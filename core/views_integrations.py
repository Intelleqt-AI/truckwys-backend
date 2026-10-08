# TENANCY AUDIT: 2026-03-15 — Integration views audited
# - Xero: replaced by core.accounting (provider-neutral, per-company connection).
# - FleetImportTripsView, CreditLookupView: Authenticated, operate on specific IDs ✓
# - FleetTripSyncView, FleetTripBulkSyncView, TripSyncView (re-audited 2026-10):
#   the old "specific entities" tick was wrong (any tenant's load by number,
#   Customer.objects.first(), and real keys were rejected). Now every lookup,
#   create and customer is scoped to the API key's company (resolve_fleet_key,
#   core.services.tms_sync); demo keys only in DEBUG for FLEET_DEMO_COMPANY_ID.
# - DashboardInsightsView: Uses Company.objects.first() - needs user company ⚠️
# - CashFlowForecastView: Aggregates all data - needs company filter ⚠️

"""
Integration Views
Handles API endpoints for third-party integrations (fleet software, credit bureaus).
Accounting (Xero, QuickBooks Online) lives in core.accounting.views.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.shortcuts import redirect
from django.conf import settings
from core.models import Company, Invoice, Load, Driver, Vehicle, Customer
from core.models.integration_api_key import IntegrationAPIKey
from core.permissions import IsIntegrationAdmin
from core.integrations.credit_bureau import CreditBureauService
from core.integrations.fleet import ManualFleetIntegration
from core.services.intelligence import IntelligenceService
from core.services.cashflow import CashFlowForecastService
from datetime import datetime, date, timedelta
import csv
import io
import uuid
from decimal import Decimal
from datetime import datetime, date
from django.utils import timezone


from django.core import signing
import logging

logger = logging.getLogger(__name__)

def _user_company(request):
    """The logged-in user's own company (multi-tenant safe)."""
    from core.views import resolve_user_company
    return resolve_user_company(request.user)


class CartrackStatusView(APIView):
    """
    Get Cartrack connection status for the current user's company.
    GET /api/v1/integrations/cartrack/status/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _user_company(request)
        return Response({
            'configured': bool(company.cartrack_username and company.cartrack_password),
            'connected': bool(company.cartrack_connected_at),
            'base_url': company.cartrack_base_url,
            'connected_at': company.cartrack_connected_at,
            'last_status_sync': company.cartrack_last_status_sync,
        }, status=status.HTTP_200_OK)


class CartrackConnectView(APIView):
    """
    Save and validate this company's Cartrack Fleet API credentials.
    POST /api/v1/integrations/cartrack/connect/
    Body: {username, password, base_url}
    """
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        from core.utils.crypto import encrypt_secret
        from core.integrations.cartrack import CartrackClient, CartrackAPIError

        username = (request.data.get('username') or '').strip()
        password = request.data.get('password') or ''
        base_url = (request.data.get('base_url') or '').strip().rstrip('/')

        if not username or not password or not base_url:
            return Response(
                {'error': 'username, password and base_url are all required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            CartrackClient(username, password, base_url).get_vehicles()
        except CartrackAPIError as exc:
            return Response(
                {'error': f'Could not connect to Cartrack with these credentials: {exc}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company = _user_company(request)
        company.cartrack_username = username
        company.cartrack_password = encrypt_secret(password)
        company.cartrack_base_url = base_url
        company.cartrack_connected_at = timezone.now()
        company.save(update_fields=[
            'cartrack_username', 'cartrack_password', 'cartrack_base_url', 'cartrack_connected_at',
        ])

        return Response({'success': True, 'message': 'Cartrack connected successfully'},
                        status=status.HTTP_200_OK)


class CtrlFleetStatusView(APIView):
    """
    Get CtrlFleet connection status for the current user's company.
    GET /api/v1/integrations/ctrlfleet/status/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _user_company(request)
        matched_vehicles = Vehicle.objects.filter(company=company).exclude(
            ctrlfleet_vehicle_code__isnull=True
        ).exclude(ctrlfleet_vehicle_code='').count()

        return Response({
            'configured': bool(company.ctrlfleet_api_key),
            'connected': bool(company.ctrlfleet_connected_at),
            'connected_at': company.ctrlfleet_connected_at,
            'last_vehicle_sync': company.ctrlfleet_last_vehicle_sync,
            'matched_vehicles': matched_vehicles,
        }, status=status.HTTP_200_OK)


class CtrlFleetConnectView(APIView):
    """
    Save and validate this company's CtrlFleet API key, then run the
    one-time vehicle-roster sync (matches CtrlFleet vehicles to ours by
    licence plate).
    POST /api/v1/integrations/ctrlfleet/connect/
    Body: {api_key}
    """
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        from core.utils.crypto import encrypt_secret
        from core.integrations.ctrlfleet import CtrlFleetClient, CtrlFleetAPIError
        from core.services.ctrlfleet_sync import sync_ctrlfleet_vehicles

        api_key = (request.data.get('api_key') or '').strip()
        if not api_key:
            return Response({'error': 'api_key is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            vehicles = CtrlFleetClient(api_key).list_vehicles()
        except CtrlFleetAPIError as exc:
            return Response(
                {'error': f'Could not connect to CtrlFleet with this key: {exc}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company = _user_company(request)
        company.ctrlfleet_api_key = encrypt_secret(api_key)
        company.ctrlfleet_connected_at = timezone.now()
        company.save(update_fields=['ctrlfleet_api_key', 'ctrlfleet_connected_at'])

        sync_result = sync_ctrlfleet_vehicles(company, vehicles=vehicles)

        return Response({
            'success': True,
            'message': 'CtrlFleet connected successfully',
            'sync': sync_result,
        }, status=status.HTTP_200_OK)


class CtrlFleetDisconnectView(APIView):
    """
    Disconnect CtrlFleet integration — clears the stored key and unlinks
    every vehicle matched to a CtrlFleet vehicleCode.
    POST /api/v1/integrations/ctrlfleet/disconnect/
    """
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        company = _user_company(request)
        company.ctrlfleet_api_key = None
        company.ctrlfleet_connected_at = None
        company.save(update_fields=['ctrlfleet_api_key', 'ctrlfleet_connected_at'])
        Vehicle.objects.filter(company=company).update(ctrlfleet_vehicle_code=None)

        return Response({'success': True, 'message': 'CtrlFleet disconnected'}, status=status.HTTP_200_OK)


class CtrlFleetVehiclesView(APIView):
    """
    CtrlFleet's fleet roster, annotated with whatever TruckWys vehicle (if any)
    each one is already linked to, plus this company's own vehicle list so the
    frontend can offer a manual-link dropdown for unmatched entries.
    GET /api/v1/integrations/ctrlfleet/vehicles/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.integrations.ctrlfleet import CtrlFleetClient, CtrlFleetAPIError

        company = _user_company(request)
        if not company.ctrlfleet_api_key:
            return Response(
                {'error': 'CtrlFleet not connected. Please connect first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cf_vehicles = CtrlFleetClient.for_company(company).list_vehicles()
        except CtrlFleetAPIError as exc:
            return Response({'error': f'Could not fetch vehicles: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)

        local_vehicles = list(Vehicle.objects.filter(company=company))
        local_by_code = {v.ctrlfleet_vehicle_code: v for v in local_vehicles if v.ctrlfleet_vehicle_code}

        annotated = []
        for cf in cf_vehicles:
            code = cf.get('vehicleCode')
            matched = local_by_code.get(code)
            annotated.append({
                'licence_number': cf.get('licenceNumber'),
                'vehicle_code': code,
                'type': cf.get('type'),
                'device_name': cf.get('deviceName'),
                'matched_vehicle_id': matched.id if matched else None,
                'matched_vehicle_plate': matched.plate if matched else None,
            })

        return Response({
            'ctrlfleet_vehicles': annotated,
            'truckwys_vehicles': [
                {
                    'id': v.id,
                    'plate': v.plate,
                    'make': v.make,
                    'model': v.model,
                    'ctrlfleet_vehicle_code': v.ctrlfleet_vehicle_code,
                }
                for v in local_vehicles
            ],
        }, status=status.HTTP_200_OK)


class CtrlFleetLinkVehicleView(APIView):
    """
    Manually link (or unlink) a TruckWys vehicle to a CtrlFleet vehicleCode —
    for cases the automatic plate match misses (typo, different format, or a
    CtrlFleet entry that isn't a real plate at all).
    POST /api/v1/integrations/ctrlfleet/link-vehicle/
    Body: {vehicle_id, ctrlfleet_vehicle_code}  # falsy code unlinks
    """
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        company = _user_company(request)
        vehicle_id = request.data.get('vehicle_id')
        vehicle_code = (request.data.get('ctrlfleet_vehicle_code') or '').strip() or None

        if not vehicle_id:
            return Response({'error': 'vehicle_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            vehicle = Vehicle.objects.get(id=vehicle_id, company=company)
        except Vehicle.DoesNotExist:
            return Response({'error': 'Vehicle not found'}, status=status.HTTP_404_NOT_FOUND)

        vehicle.ctrlfleet_vehicle_code = vehicle_code
        vehicle.save(update_fields=['ctrlfleet_vehicle_code'])

        return Response({
            'success': True,
            'vehicle_id': vehicle.id,
            'ctrlfleet_vehicle_code': vehicle_code,
        }, status=status.HTTP_200_OK)


class CtrlFleetSyncPositionsView(APIView):
    """
    Poll CtrlFleet for live positions of vehicles already linked to this
    company, and update their latitude/longitude/heading/speed/last_location_at.
    Runs on a schedule too (core.tasks.poll_ctrlfleet_positions); this is the
    on-demand trigger for immediate feedback.
    POST /api/v1/integrations/ctrlfleet/sync-positions/
    Body (optional): {vehicle_id} — sync just that one vehicle (e.g. from an
    order's "Sync Location" button) instead of the whole fleet.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from core.integrations.ctrlfleet import CtrlFleetAPIError
        from core.services.ctrlfleet_sync import sync_ctrlfleet_positions

        vehicle_id = request.data.get('vehicle_id')
        vehicle_ids = [vehicle_id] if vehicle_id else None
        # Refreshing one vehicle's location is the order page's "Sync
        # Location" button, used by dispatchers; a fleet-wide sync is an
        # integration action and needs a company admin.
        if vehicle_ids is None and not IsIntegrationAdmin().has_permission(request, self):
            return Response({'error': IsIntegrationAdmin.message}, status=status.HTTP_403_FORBIDDEN)

        company = _user_company(request)
        if not company.ctrlfleet_api_key:
            return Response(
                {'error': 'CtrlFleet not connected. Please connect first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            sync_result = sync_ctrlfleet_positions(company, vehicle_ids=vehicle_ids)
        except CtrlFleetAPIError as exc:
            return Response({'error': f'Position sync failed: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'success': True, 'sync': sync_result}, status=status.HTTP_200_OK)


class CtrlFleetSyncVehiclesView(APIView):
    """
    Re-run the CtrlFleet vehicle-matching sync on demand (e.g. after adding
    a new truck to the fleet).
    POST /api/v1/integrations/ctrlfleet/sync-vehicles/
    """
    permission_classes = [IsIntegrationAdmin]

    def post(self, request):
        from core.integrations.ctrlfleet import CtrlFleetAPIError
        from core.services.ctrlfleet_sync import sync_ctrlfleet_vehicles

        company = _user_company(request)
        if not company.ctrlfleet_api_key:
            return Response(
                {'error': 'CtrlFleet not connected. Please connect first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            sync_result = sync_ctrlfleet_vehicles(company)
        except CtrlFleetAPIError as exc:
            return Response({'error': f'Sync failed: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'success': True, 'sync': sync_result}, status=status.HTTP_200_OK)


class FleetImportTripsView(APIView):
    """
    Import trip data from CSV/Excel.
    POST /api/v1/integrations/fleet/import-trips/

    Expected CSV columns:
    origin, destination, distance_km, vehicle_reg, driver_name, start_date, end_date, fuel_litres, toll_cost
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Get uploaded file
        csv_file = request.FILES.get('file')

        if not csv_file:
            return Response(
                {'error': 'No file provided. Please upload a CSV file.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate file type
        if not csv_file.name.endswith('.csv'):
            return Response(
                {'error': 'Invalid file type. Please upload a CSV file.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Read CSV
            decoded_file = csv_file.read().decode('utf-8')
            io_string = io.StringIO(decoded_file)
            reader = csv.DictReader(io_string)

            # Use manual fleet integration
            fleet_integration = ManualFleetIntegration()

            results = {
                'total': 0,
                'success': 0,
                'failed': 0,
                'errors': [],
            }

            for row in reader:
                results['total'] += 1

                try:
                    trip_data = fleet_integration.parse_trip_row(row)
                    # TODO: Create Trip record in database
                    results['success'] += 1
                except Exception as e:
                    results['failed'] += 1
                    results['errors'].append({
                        'row': results['total'],
                        'error': str(e),
                    })

            return Response(results, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'Failed to import trips: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CreditLookupView(APIView):
    """
    Lookup credit score for a customer.
    POST /api/v1/integrations/credit/lookup/
    Body: {"customer_id": 123}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from core.models import Customer

        customer_id = request.data.get('customer_id')

        if not customer_id:
            return Response(
                {'error': 'customer_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Tenant isolation (2026-09): only the caller's own customers can be
        # sent to the bureau. Company-less accounts fail closed; a foreign
        # customer is indistinguishable from a missing one (404).
        company = getattr(request.user, 'company', None)
        if not company:
            return Response(
                {'error': 'No company associated with this account'},
                status=status.HTTP_403_FORBIDDEN
            )
        try:
            customer = Customer.objects.get(id=customer_id, company=company)
        except (Customer.DoesNotExist, ValueError, TypeError):
            return Response(
                {'error': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Use credit bureau service
        credit_service = CreditBureauService()

        try:
            score_data = credit_service.get_score(customer)

            return Response(score_data, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'Failed to lookup credit score: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DashboardInsightsView(APIView):
    """
    Get dashboard insights and recommendations.
    GET /api/v1/dashboard/insights/
    Query params:
    - from: YYYY-MM-DD (optional, defaults to 30 days ago)
    - to: YYYY-MM-DD (optional, defaults to today)
    - create_notifications: 'true' to create notifications (default false)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Parse date range from query params
        from_date_str = request.query_params.get('from')
        to_date_str = request.query_params.get('to')

        today = date.today()

        # Default: last 30 days
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid from date format. Use YYYY-MM-DD'}, status=400)
        else:
            from_date = today - timedelta(days=30)

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid to date format. Use YYYY-MM-DD'}, status=400)
        else:
            to_date = today

        # Tenant isolation (2026-09): was Company.objects.first(), which served
        # the first tenant's invoices/debtors to every caller. Company-less
        # accounts fail closed.
        company = getattr(request.user, 'company', None)
        if not company:
            return Response(
                {'error': 'No company associated with this account'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Generate intelligence recommendations
        intelligence_service = IntelligenceService(company)
        try:
            recommendations = intelligence_service.generate_recommendations()
        except Exception:
            # Was: 200 with an empty list, indistinguishable from "nothing to
            # flag" (audit #44). Now an explicit error the UI can show as one.
            logger.exception('DashboardInsightsView: generate_recommendations failed')
            return Response(
                {'error': 'Recommendations are unavailable right now. Please try again.',
                 'data_status': 'error'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Optionally create notifications
        create_notifications = request.query_params.get('create_notifications', 'false').lower() == 'true'

        if create_notifications and recommendations:
            intelligence_service.create_notifications(request.user, recommendations)

        return Response({
            'recommendations': recommendations,
            'total': len(recommendations),
            'by_type': self._group_by_type(recommendations),
            'by_severity': self._group_by_severity(recommendations),
            'from_date': from_date.isoformat(),
            'to_date': to_date.isoformat(),
        }, status=status.HTTP_200_OK)

    def _group_by_type(self, recommendations):
        """Group recommendations by type."""
        grouped = {}
        for rec in recommendations:
            rec_type = rec.get('type', 'UNKNOWN')
            if rec_type not in grouped:
                grouped[rec_type] = []
            grouped[rec_type].append(rec)
        return grouped

    def _group_by_severity(self, recommendations):
        """Group recommendations by severity."""
        grouped = {'HIGH': [], 'MEDIUM': [], 'LOW': []}
        for rec in recommendations:
            severity = rec.get('severity', 'LOW')
            if severity in grouped:
                grouped[severity].append(rec)
        return grouped


class CashFlowForecastView(APIView):
    """
    Get cash flow forecast.
    GET /api/v1/dashboard/cashflow/
    Query params:
    - days: forecast period in days (default 90, max 365)
    - from: YYYY-MM-DD (optional start date for historical analysis)
    - to: YYYY-MM-DD (optional end date for historical analysis)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Get forecast period from query params
        days = int(request.query_params.get('days', 90))
        from_date_str = request.query_params.get('from')
        to_date_str = request.query_params.get('to')

        # Validate days
        if days < 1 or days > 365:
            return Response(
                {'error': 'Days must be between 1 and 365'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Parse historical date range if provided
        from_date = None
        to_date = None
        if from_date_str:
            try:
                from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid from date format. Use YYYY-MM-DD'}, status=400)

        if to_date_str:
            try:
                to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid to date format. Use YYYY-MM-DD'}, status=400)

        # Tenant isolation (2026-09): the forecast used to aggregate every
        # tenant's invoices/expenses. Company-less accounts fail closed.
        company = getattr(request.user, 'company', None)
        if not company:
            return Response(
                {'error': 'No company associated with this account'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Generate forecast
        cashflow_service = CashFlowForecastService(company)
        try:
            forecast = cashflow_service.forecast_cashflow(days=days)
            summary = cashflow_service.get_summary_stats(forecast)
        except Exception:
            # Was: 200 with a zero summary whose keys differed from the success
            # shape, so a UI read R 0 either way (audit #45). Now an explicit
            # error with no figures in it.
            logger.exception('CashFlowForecastView: forecast failed')
            return Response(
                {'error': 'The cash flow forecast is unavailable right now. Please try again.',
                 'data_status': 'error', 'period_days': days},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        response_data = {
            'forecast': forecast,
            'summary': summary,
            'period_days': days,
        }

        if from_date:
            response_data['from_date'] = from_date.isoformat()
        if to_date:
            response_data['to_date'] = to_date.isoformat()

        return Response(response_data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Fleet TMS Integration - Inbound Trip Sync
# ---------------------------------------------------------------------------

# Temporary demo keys (will be replaced with IntegrationAPIKey model in Sprint C3)
FLEET_DEMO_API_KEYS = {
    'fleet_demo_key_123': 'Demo Fleet TMS',
    'tms_integration_test': 'Test Fleet System',
}


class FleetAPIKeyAuthentication(BaseAuthentication):
    """Authenticate fleet TMS systems via X-API-Key header.

    Resolves real IntegrationAPIKey records (metered, quota-enforced, operator-scoped).
    The hardcoded demo keys remain only as a DEBUG convenience for local testing.
    """

    def authenticate(self, request):
        # Header-only — never accept the key via query string.
        key = request.META.get('HTTP_X_API_KEY')
        if not key:
            return None  # Not an API key request — try other auth
        key_obj = resolve_fleet_key(request, key)
        return (key_obj.operator, key_obj)

    def authenticate_header(self, request):
        return 'X-API-Key'


class _DemoFleetOperator:
    """Pseudo operator for a DEBUG demo key: bound to ONE company
    (settings.FLEET_DEMO_COMPANY_ID), never to "whatever company sorts first"."""
    is_authenticated = True
    is_active = True
    is_staff = False
    is_superuser = False
    pk = id = 'demo-fleet'

    def __init__(self, name, company):
        self.username = f'demo-fleet:{name}'
        self.company = company
        self.company_id = getattr(company, 'id', None)


class _DemoFleetKey:
    key_type = 'FLEET_TMS'

    def __init__(self, key, name, company):
        self.key = key
        self.name = name
        self.operator = _DemoFleetOperator(name, company)
        self.pk = self.id = None


class IntegrationQuotaExceeded(AuthenticationFailed):
    status_code = 429
    default_detail = 'Monthly API quota exceeded.'


def resolve_fleet_key(request, key):
    """The IntegrationAPIKey (or, in DEBUG only, a demo key) for a fleet / TMS
    call. Raises AuthenticationFailed (401) for an unknown, inactive, lender,
    IP-blocked or company-less key, IntegrationQuotaExceeded (429) over quota.

    Every fleet / TMS endpoint works ONLY on the key's company
    (key.operator.company): lookups, creates and customers alike."""
    key_obj = IntegrationAPIKey.objects.filter(
        key=key, active=True
    ).select_related('operator', 'operator__company').first()
    if key_obj is not None:
        if key_obj.key_type == 'LENDER':
            # A lender key reads the funding book; it never writes loads.
            raise AuthenticationFailed('This API key cannot sync trips.')
        operator = key_obj.operator
        if operator is None or not getattr(operator, 'is_active', False):
            raise AuthenticationFailed('Invalid API key.')
        if getattr(operator, 'company_id', None) is None:
            raise AuthenticationFailed('This API key is not linked to a company.')
        ip = request.META.get('REMOTE_ADDR', '')
        if not key_obj.is_ip_allowed(ip):
            raise AuthenticationFailed('Calls from this address are not allowed for this API key.')
        if key_obj.is_over_quota():
            raise IntegrationQuotaExceeded()
        key_obj.record_call()
        request.integration_key = key_obj
        return key_obj

    # DEBUG-only demo keys, bound to settings.FLEET_DEMO_COMPANY_ID. Never in
    # production, and never without a company.
    if getattr(settings, 'DEBUG', False):
        fleet_name = FLEET_DEMO_API_KEYS.get(key)
        company_id = getattr(settings, 'FLEET_DEMO_COMPANY_ID', None)
        if fleet_name and company_id:
            company = Company.objects.filter(pk=company_id).first()
            if company is not None:
                demo = _DemoFleetKey(key, fleet_name, company)
                request.integration_key = demo
                return demo

    raise AuthenticationFailed('Invalid API key.')


def integration_company(request):
    """The company an authenticated fleet / TMS request acts for, or None
    when the request did not come in on an integration key (e.g. a forced /
    session user): only a key names the company a TMS acts for."""
    if getattr(request, 'integration_key', None) is None:
        return None
    return getattr(request.user, 'company', None)


def _no_key_response():
    return Response({'error': 'API key required. Pass X-API-Key header.'},
                    status=status.HTTP_401_UNAUTHORIZED)


class FleetTripSyncView(APIView):
    """
    POST /api/v1/integrations/fleet/sync/

    Receive trip updates from external Fleet/TMS systems.

    Body:
    {
      "action": "status_update" | "create" | "complete",
      "load_number": "LD-20260227-0196",
      "status": "IN_TRANSIT",
      "driver_id": 3,
      "vehicle_plate": "NW 123 BCD",
      "notes": "Departed depot 08:45",
      "pickup_location": "Johannesburg",
      "delivery_location": "Cape Town"
    }
    """
    authentication_classes = [FleetAPIKeyAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.db import transaction
        from core.serializers import LoadSerializer
        from core.services.tms_sync import SyncError, apply_fleet_trip
        company = integration_company(request)
        if company is None:
            return _no_key_response()
        try:
            with transaction.atomic():
                load, created = apply_fleet_trip(company, request.data)
        except SyncError as e:
            return Response({'error': e.message}, status=e.http_status)
        return Response(LoadSerializer(load).data, status=status.HTTP_200_OK)


class FleetTripBulkSyncView(APIView):
    """
    POST /api/v1/integrations/fleet/sync/bulk/

    Bulk trip sync for external Fleet/TMS systems.

    Body:
    {
      "trips": [
        {
          "action": "status_update",
          "load_number": "LD-001",
          "status": "IN_TRANSIT",
          ...
        },
        {
          "action": "create",
          "load_number": "LD-002",
          ...
        }
      ]
    }

    Returns:
    {
      "processed": 10,
      "created": 2,
      "updated": 8,
      "errors": []
    }
    """
    authentication_classes = [FleetAPIKeyAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.db import transaction
        from core.services.tms_sync import SyncError, apply_fleet_trip
        company = integration_company(request)
        if company is None:
            return _no_key_response()
        trips = request.data.get('trips', []) if isinstance(request.data, dict) else None

        if not trips or not isinstance(trips, list):
            return Response(
                {'error': 'trips array is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if len(trips) > 500:
            return Response({'error': 'Max 500 trips per request'}, status=status.HTTP_400_BAD_REQUEST)

        results = {
            'processed': 0,
            'created': 0,
            'updated': 0,
            'errors': [],
        }

        for idx, trip_data in enumerate(trips):
            results['processed'] += 1
            if not isinstance(trip_data, dict):
                results['errors'].append({'index': idx, 'load_number': None, 'error': 'Not an object'})
                continue
            try:
                with transaction.atomic():
                    load, created = apply_fleet_trip(company, trip_data)
                results['created' if created else 'updated'] += 1
            except SyncError as e:
                results['errors'].append({
                    'index': idx, 'load_number': trip_data.get('load_number'), 'error': e.message,
                })
            except Exception:
                logger.exception('fleet bulk sync item %s failed', idx)
                results['errors'].append({
                    'index': idx, 'load_number': trip_data.get('load_number'),
                    'error': 'Could not process this trip',
                })

        return Response(results, status=status.HTTP_200_OK)


class TripSyncView(APIView):
    """
    Inbound trip sync endpoint for external TMS systems.

    POST /api/v1/integrations/trips/sync/
    Headers: X-API-Key: <api_key>
    Body: [
        {
            "external_id": "TMS-12345",
            "origin": "Johannesburg",
            "destination": "Cape Town",
            "cargo_description": "Palletized goods",
            "weight": 15000,
            "distance": 1400
        }
    ]
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        from django.db import transaction
        from core.services.tms_sync import SyncError, sync_trip_record
        # Validate + meter the API key (company-scoped, see resolve_fleet_key).
        api_key = request.headers.get('X-API-Key', '')
        if not api_key:
            return Response({'error': 'Invalid or missing API key'}, status=401)
        try:
            key_obj = resolve_fleet_key(request, api_key)
        except IntegrationQuotaExceeded:
            return Response({'error': 'Monthly API quota exceeded'}, status=429)
        except AuthenticationFailed as e:
            return Response({'error': str(e.detail)}, status=401)
        company = key_obj.operator.company

        records = request.data if isinstance(request.data, list) else request.data.get('trips', [])
        if not isinstance(records, list):
            return Response({'error': 'trips array is required'}, status=400)
        if len(records) > 500:
            return Response({'error': 'Max 500 records per request'}, status=400)

        created, skipped, errors, created_ids = 0, 0, [], []
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                errors.append({'index': i, 'error': 'Not an object'})
                continue
            try:
                with transaction.atomic():
                    outcome, load = sync_trip_record(company, rec)
                if outcome == 'created':
                    created += 1
                    created_ids.append(load.id)
                else:
                    skipped += 1
            except SyncError as e:
                errors.append({'index': i, 'error': e.message})
            except Exception:
                logger.exception('trip sync record %s failed', i)
                errors.append({'index': i, 'error': 'Could not process this record'})

        return Response({
            'created': created, 'skipped': skipped, 'errors': errors,
            'total': len(records), 'load_ids': created_ids,
        })
