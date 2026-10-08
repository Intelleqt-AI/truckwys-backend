"""Fleet settings: measured fuel use (fleet tracker) vs configured burn.

GET  /api/v1/fleet/fuel-actuals/                                   any company user
POST /api/v1/fleet/fuel-actuals/vehicle-types/<id>/burn-mode/       admin  {mode}
POST /api/v1/fleet/fuel-actuals/refresh/                            admin

Reads stored rows only; "refresh" queues the Celery job (never calls the
tracker in the request).
"""
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsIntegrationAdmin

REFRESH_LOCK_SECONDS = 15 * 60


def _company(request):
    from core.views import resolve_user_company
    return resolve_user_company(request.user)


def _run_out(run):
    if run is None:
        return None
    return {'status': run.status, 'provider': run.provider or None, 'message': run.message,
            'started_at': timezone.localtime(run.started_at).isoformat() if run.started_at else None,
            'finished_at': timezone.localtime(run.finished_at).isoformat() if run.finished_at else None,
            'summary': run.summary}


def vehicle_type_row(company, vt, now=None):
    from core.services import quote_costing as qc
    burn = qc.resolve_rated_burn(company, vt, now=now)
    return {
        'id': vt.id, 'name': vt.name, 'shared_default': vt.company_id is None,
        'capacity_t': qc.capacity_tonnes(vt.capacity),
        'configured_l_per_100km': burn['configured'],
        'configured_source': 'standard' if vt.company_id is None else 'configured',
        'burn_mode': burn['mode'],
        'in_use': {'value': burn['value'], 'source': burn['source'], 'label': burn['label']},
        'measured': burn['measured'],
        'can_use_measured': bool(burn['measured'] and burn['measured']['usable']),
    }


class FleetFuelActualsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import FleetFuelMeasurement as M, FleetFuelSyncRun, Vehicle
        from core.services import fleet_fuel_actuals as ffa
        from core.services.vehicle_types import visible_vehicle_types_queryset
        company = _company(request)
        now = timezone.now()
        provider, reason = ffa.connection_status(company)
        vehicle_rows = {r.vehicle_id: r for r in M.objects.filter(company=company, scope=M.SCOPE_VEHICLE)}
        vehicles = [{
            'id': v.id, 'plate': v.plate, 'vehicle_type_id': v.vehicle_type_id,
            'vehicle_type': v.vehicle_type.name if v.vehicle_type_id else None,
            'configured_override_l_per_100km': (float(v.fuel_consumption_l_per_100km)
                                                if v.fuel_consumption_l_per_100km is not None else None),
            'measured': ffa.measurement_out(vehicle_rows.get(v.id), company, now),
        } for v in Vehicle.objects.filter(company=company).select_related('vehicle_type').order_by('plate')]
        return Response({
            'connection': {'provider': provider, 'reason': reason, 'can_measure': provider is not None},
            'last_run': _run_out(FleetFuelSyncRun.objects.filter(company=company).first()),
            'refresh_queued': bool(cache.get(f'fleet-fuel-refresh:{company.id}')),
            'vehicle_types': [vehicle_type_row(company, vt, now) for vt in visible_vehicle_types_queryset(company)],
            'vehicles': vehicles,
            'rules': {
                'period_days': ffa.PERIOD_DAYS, 'min_distance_km': ffa.MIN_DISTANCE_KM,
                'assumed_load_ratio': ffa.ASSUMED_LOAD_RATIO, 'max_age_days': ffa.MAX_AGE_DAYS,
                'refresh': 'Weekly, Monday 02:30',
            },
        })


class FleetFuelBurnModeView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request, pk):
        from core.models import FleetFuelMeasurement as M
        from core.services import fleet_fuel_actuals as ffa
        from core.services.vehicle_types import visible_vehicle_types_queryset
        company = _company(request)
        vt = visible_vehicle_types_queryset(company).filter(id=pk).first()
        if vt is None:
            return Response({'error': 'Vehicle type not found.'}, status=status.HTTP_404_NOT_FOUND)
        mode = str(request.data.get('mode') or '').upper()
        if mode not in (M.MODE_AUTO, M.MODE_MEASURED, M.MODE_CONFIGURED):
            return Response({'error': 'Choose "Use measured figure", "Use my figure" or automatic.'},
                            status=status.HTTP_400_BAD_REQUEST)
        row = ffa.type_measurement(company, vt)
        if mode == M.MODE_MEASURED:
            ok, why = ffa.usable(row, company)
            if not ok:
                msg = {'not_measured': 'There is no measured figure for this truck type yet.',
                       'tracker_disconnected': 'Reconnect your fleet tracker to use its figure.',
                       'stale': 'The measured figure is out of date; refresh it first.',
                       'rejected': 'The measured figure failed the quality checks.'}.get(
                    why, f'Not enough tracker data yet (needs {int(ffa.MIN_DISTANCE_KM)} km in '
                         f'{ffa.PERIOD_DAYS} days).')
                return Response({'error': msg, 'code': why}, status=status.HTTP_400_BAD_REQUEST)
        if mode == M.MODE_CONFIGURED and vt.fuel_consumption_l_per_100km is None:
            return Response({'error': 'Set litres per 100 km for this truck type first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if row is None:
            row = M(company=company, scope=M.SCOPE_VEHICLE_TYPE, vehicle_type=vt, confidence='insufficient',
                    note='Not measured yet.')
        row.burn_mode, row.burn_mode_set_at, row.burn_mode_set_by = mode, timezone.now(), request.user
        row.save()
        return Response(vehicle_type_row(company, vt))


class FleetFuelRefreshView(APIView):
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        from core.services import fleet_fuel_actuals as ffa
        from core.tasks import refresh_fleet_fuel_actuals
        company = _company(request)
        provider, reason = ffa.connection_status(company)
        if provider is None:
            return Response({'error': reason, 'queued': False}, status=status.HTTP_400_BAD_REQUEST)
        if not cache.add(f'fleet-fuel-refresh:{company.id}', 1, REFRESH_LOCK_SECONDS):
            return Response({'queued': True, 'message': 'A refresh is already on its way.'},
                            status=status.HTTP_202_ACCEPTED)
        refresh_fleet_fuel_actuals.delay(company_id=company.id)
        return Response({'queued': True, 'message': 'Refreshing from your tracker; this takes a few minutes.'},
                        status=status.HTTP_202_ACCEPTED)
