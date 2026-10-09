"""Fleet settings: measured fuel use (fleet tracker) vs configured burn.

GET  /api/v1/fleet/fuel-actuals/                                   any company user
POST /api/v1/fleet/fuel-actuals/vehicle-types/<id>/burn-mode/       admin  {mode}
POST /api/v1/fleet/fuel-actuals/refresh/                            admin

Reads stored rows only; "refresh" queues the Celery job (never calls the
tracker in the request).
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsIntegrationAdmin

# One manual refresh per company per 15 minutes, counted from its start;
# the lock (holding the start time) lives for the whole cooldown.
REFRESH_LOCK_SECONDS = 15 * 60


def _lock_key(company):
    return f'fleet-fuel-refresh:{company.id}'


def _claim_lock(company, now):
    key = _lock_key(company)
    if cache.add(key, now.isoformat(), REFRESH_LOCK_SECONDS):
        return True
    stale = cache.get(key)
    if stale is None or refresh_state(company, now)['next_at'] is not None:
        return False
    if not cache.add(f'{key}:takeover:{stale}', 1, 60):
        return False
    cache.set(key, now.isoformat(), REFRESH_LOCK_SECONDS)
    return True


def refresh_state(company, now=None):
    """{queued, next_at, next_at_dt}: queued = started and no run has finished
    since; next_at = when "Refresh now" can run again (SAST ISO) or None."""
    from datetime import datetime, timedelta
    from core.models import FleetFuelSyncRun
    now = now or timezone.now()
    raw = cache.get(_lock_key(company))
    try:
        started = datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        started = None
    if started is None:
        return {'queued': False, 'next_at': None, 'next_at_dt': None}
    next_at = started + timedelta(seconds=REFRESH_LOCK_SECONDS)
    if next_at <= now:
        return {'queued': False, 'next_at': None, 'next_at_dt': None}
    done = FleetFuelSyncRun.objects.filter(company=company, finished_at__gte=started).exists()
    return {'queued': not done, 'next_at': timezone.localtime(next_at).isoformat(), 'next_at_dt': next_at}


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
        refresh = refresh_state(company, now)
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
            'refresh_queued': refresh['queued'],
            # "Refresh now" can run again from this time (SAST), else null.
            'refresh_next_at': refresh['next_at'],
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
        body = request.data if isinstance(request.data, dict) else None
        if body is None:
            return Response({'error': 'Send {"mode": "MEASURED"}, {"mode": "CONFIGURED"} or {"mode": "AUTO"}.',
                             'code': 'invalid_body'}, status=status.HTTP_400_BAD_REQUEST)
        mode = str(body.get('mode') or '').upper()
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
        now = timezone.now()
        state = refresh_state(company, now)
        if state['next_at'] is None:
            # Missing or expired: claim it. Never delete-then-add (two
            # simultaneous presses could both queue): add() is atomic, and a
            # lock the cache still holds past its cooldown is taken over through
            # a one-off claim key that only one press can add.
            if _claim_lock(company, now):
                refresh_fleet_fuel_actuals.delay(company_id=company.id)
                return Response({'queued': True, 'next_at': timezone.localtime(
                    now + timedelta(seconds=REFRESH_LOCK_SECONDS)).isoformat(),
                    'message': 'Refreshing from your tracker; this takes a few minutes.'},
                    status=status.HTTP_202_ACCEPTED)
            state = refresh_state(company, now)
        at = timezone.localtime(state['next_at_dt']).strftime('%H:%M') if state['next_at_dt'] else None
        return Response({'error': f'You can refresh again at {at}.' if at else 'A refresh is already on its way.',
                         'code': 'refresh_cooldown', 'queued': state['queued'], 'next_at': state['next_at']},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
