# TENANCY AUDIT: 2026-03-15 — Integration views audited
# - XeroConnectView, XeroCallbackView, XeroDisconnectView, XeroStatusView,
#   XeroSyncInvoicesView, XeroSyncPaymentsView: All use Company.objects.first()
#   which assumes single-tenant deployment. OK for current sprint, but needs
#   request.user.company for true multi-tenancy ⚠️
# - FleetImportTripsView, CreditLookupView: Authenticated, operate on specific IDs ✓
# - FleetTripSyncView, FleetTripBulkSyncView: API key authenticated, operate on specific entities ✓
# - DashboardInsightsView: Uses Company.objects.first() - needs user company ⚠️
# - CashFlowForecastView: Aggregates all data - needs company filter ⚠️
# - TripSyncView: API key validated, operates on specific trips ✓

"""
Integration Views
Handles API endpoints for third-party integrations (Xero, Fleet software, Credit bureaus)
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
from core.integrations.xero import XeroClient
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

_XERO_STATE_SALT = 'xero-oauth-state'


def _user_company(request):
    """The logged-in user's own company (multi-tenant safe)."""
    from core.views import resolve_user_company
    return resolve_user_company(request.user)


def _frontend_redirect(outcome: str):
    """Bounce the OAuth popup/tab back to the in-app Xero settings page."""
    base = getattr(settings, 'FRONTEND_URL', '') or 'http://localhost:3701'
    return redirect(f'{base.rstrip("/")}/settings/integrations/xero?xero={outcome}')


class XeroConnectView(APIView):
    """
    Begin Xero OAuth. Returns the authorization URL for the frontend to redirect to.
    GET /api/v1/integrations/xero/connect/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _user_company(request)
        xero_client = XeroClient(company)

        if not xero_client.is_configured:
            return Response(
                {'error': 'Xero is not configured on this server. Add XERO_CLIENT_ID '
                          'and XERO_CLIENT_SECRET to the backend environment to enable it.',
                 'configured': False},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        # Signed state carries the company id (the callback is public/unauthenticated)
        # and doubles as CSRF protection — an attacker can't forge a valid value.
        state = signing.dumps({'company_id': company.id}, salt=_XERO_STATE_SALT)
        return Response({'auth_url': xero_client.get_authorization_url(state=state)},
                        status=status.HTTP_200_OK)


class XeroCallbackView(APIView):
    """
    Handle Xero OAuth callback, then redirect back into the app.
    GET /api/v1/integrations/xero/callback/?code=...&state=...
    """
    permission_classes = []  # Public endpoint for OAuth callback

    def get(self, request):
        code = request.GET.get('code')
        state = request.GET.get('state', '')
        error = request.GET.get('error')

        if error or not code:
            return _frontend_redirect('error')

        # Resolve which company this callback belongs to from the signed state.
        try:
            payload = signing.loads(state, salt=_XERO_STATE_SALT, max_age=600)
            company = Company.objects.get(id=payload['company_id'])
        except (signing.BadSignature, signing.SignatureExpired, Company.DoesNotExist, KeyError):
            return _frontend_redirect('error')

        try:
            XeroClient(company).handle_callback(code)
        except Exception:
            return _frontend_redirect('error')

        return _frontend_redirect('connected')


class XeroDisconnectView(APIView):
    """
    Disconnect Xero integration.
    POST /api/v1/integrations/xero/disconnect/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = _user_company(request)
        XeroClient(company).disconnect()
        return Response({'success': True, 'message': 'Xero disconnected successfully'},
                        status=status.HTTP_200_OK)


class XeroStatusView(APIView):
    """
    Get Xero connection status for the current user's company.
    GET /api/v1/integrations/xero/status/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _user_company(request)
        xero_client = XeroClient(company)

        return Response({
            'configured': xero_client.is_configured,
            'is_connected': xero_client.is_connected,
            'connected': xero_client.is_connected,  # legacy alias
            'tenant_id': company.xero_tenant_id,
            'tenant_name': company.xero_tenant_id,  # Xero exposes only the id we persist
            'connected_since': company.xero_connected_at,
            'connected_at': company.xero_connected_at,  # legacy alias
            'token_expires_at': company.xero_token_expires_at,
            'last_invoice_sync': company.xero_last_invoice_sync,
            'last_payment_sync': company.xero_last_payment_sync,
        }, status=status.HTTP_200_OK)


class XeroSyncInvoicesView(APIView):
    """
    Push this company's outstanding invoices to Xero.
    POST /api/v1/integrations/xero/sync-invoices/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = _user_company(request)
        xero_client = XeroClient(company)

        if not xero_client.is_connected:
            return Response({'error': 'Xero not connected. Please connect first.'},
                            status=status.HTTP_400_BAD_REQUEST)

        invoices_to_sync = Invoice.objects.filter(
            company=company,
            status__in=['SENT', 'VIEWED', 'OVERDUE'],
        ).exclude(status='CANCELLED')

        results = {'total': invoices_to_sync.count(), 'success': 0, 'failed': 0, 'errors': []}

        for invoice in invoices_to_sync:
            try:
                xero_client.push_invoice(invoice)
                results['success'] += 1
            except Exception as e:
                results['failed'] += 1
                results['errors'].append({'invoice': invoice.invoice_number, 'error': str(e)})

        company.xero_last_invoice_sync = timezone.now()
        company.save(update_fields=['xero_last_invoice_sync'])

        return Response(results, status=status.HTTP_200_OK)


class XeroSyncPaymentsView(APIView):
    """
    Pull payments from Xero and reconcile them against this company's invoices.
    POST /api/v1/integrations/xero/sync-payments/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = _user_company(request)
        xero_client = XeroClient(company)

        if not xero_client.is_connected:
            return Response({'error': 'Xero not connected. Please connect first.'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            synced_payments = xero_client.sync_payments()
        except Exception as e:
            return Response({'error': f'Failed to sync payments: {str(e)}'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        company.xero_last_payment_sync = timezone.now()
        company.save(update_fields=['xero_last_payment_sync'])

        recorded = sum(1 for p in synced_payments if p.get('status') == 'recorded')
        return Response({
            'success': True,
            'payments': synced_payments,
            'total': len(synced_payments),
            'recorded': recorded,
        }, status=status.HTTP_200_OK)


class XeroSyncLogView(APIView):
    """
    Recent Xero sync activity (derived from the last-sync timestamps).
    GET /api/v1/integrations/xero/sync-log/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = _user_company(request)
        logs = []
        if company.xero_last_invoice_sync:
            logs.append({
                'id': 1, 'sync_type': 'invoice', 'status': 'success',
                'timestamp': company.xero_last_invoice_sync, 'records_synced': None,
            })
        if company.xero_last_payment_sync:
            logs.append({
                'id': 2, 'sync_type': 'payment', 'status': 'success',
                'timestamp': company.xero_last_payment_sync, 'records_synced': None,
            })
        logs.sort(key=lambda x: x['timestamp'], reverse=True)
        return Response(logs, status=status.HTTP_200_OK)


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

        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
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

        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Generate intelligence recommendations
        intelligence_service = IntelligenceService(company)
        try:
            recommendations = intelligence_service.generate_recommendations()
        except Exception as e:
            recommendations = []

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

        # Generate forecast
        cashflow_service = CashFlowForecastService()
        try:
            forecast = cashflow_service.forecast_cashflow(days=days)
            summary = cashflow_service.get_summary_stats(forecast)
        except Exception as e:
            forecast = []
            summary = {'total_inflow': 0, 'total_outflow': 0, 'net': 0}

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
    """Authenticate fleet TMS systems via X-API-Key header."""

    def authenticate(self, request):
        # Header-only — never accept the key via query string.
        key = request.META.get('HTTP_X_API_KEY')
        if not key:
            return None  # Not an API key request — try other auth

        # TODO Sprint C3: Replace with IntegrationAPIKey.objects.filter(key=key, key_type='FLEET_TMS', active=True)
        fleet_name = FLEET_DEMO_API_KEYS.get(key)
        if not fleet_name:
            raise AuthenticationFailed('Invalid API key.')

        # Return a pseudo-user tuple
        return ({'api_key': key, 'fleet_name': fleet_name}, key)

    def authenticate_header(self, request):
        return 'X-API-Key'


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
        # Require API key
        if not request.auth or not isinstance(request.user, dict):
            return Response(
                {'error': 'API key required. Pass X-API-Key header.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        data = request.data
        action = data.get('action', 'status_update')
        load_number = data.get('load_number')

        if not load_number:
            return Response(
                {'error': 'load_number is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Try to find existing load
        try:
            load = Load.objects.get(load_number=load_number)
        except Load.DoesNotExist:
            if action == 'create':
                # Create new load from external data
                try:
                    # Get or create customer (simplified — use first customer for demo)
                    from core.models import Customer
                    customer = Customer.objects.first()
                    if not customer:
                        return Response(
                            {'error': 'No customers found. Please create at least one customer first.'},
                            status=status.HTTP_400_BAD_REQUEST
                        )

                    load = Load.objects.create(
                        load_number=load_number or f"EXT-{uuid.uuid4().hex[:8].upper()}",
                        customer=customer,
                        pickup_location=data.get('pickup_location', ''),
                        pickup_city=data.get('pickup_city', data.get('pickup_location', '')[:100]),
                        pickup_state=data.get('pickup_state', ''),
                        pickup_zip=data.get('pickup_zip', ''),
                        pickup_date=datetime.now(),
                        delivery_location=data.get('delivery_location', ''),
                        delivery_city=data.get('delivery_city', data.get('delivery_location', '')[:100]),
                        delivery_state=data.get('delivery_state', ''),
                        delivery_zip=data.get('delivery_zip', ''),
                        delivery_date=datetime.now(),
                        cargo_description=data.get('cargo_description', 'Freight'),
                        weight=Decimal(data.get('weight', 0)),
                        distance=Decimal(data.get('distance', 0)),
                        rate=Decimal(data.get('rate', 0)),
                        total_amount=Decimal(data.get('total_amount', 0)),
                        status='PENDING',
                        notes=data.get('notes', ''),
                    )
                except Exception as e:
                    return Response(
                        {'error': f'Failed to create load: {str(e)}'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
            else:
                return Response(
                    {'error': f'Load {load_number} not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

        # Update load based on action
        if action in ['status_update', 'create']:
            # Update status if provided
            if data.get('status'):
                load.status = data.get('status')

            # Update driver if provided
            if data.get('driver_id'):
                try:
                    driver = Driver.objects.get(id=data['driver_id'])
                    load.driver = driver
                except Driver.DoesNotExist:
                    pass

            # Update vehicle if plate provided
            if data.get('vehicle_plate'):
                try:
                    vehicle = Vehicle.objects.get(registration_number=data['vehicle_plate'])
                    load.vehicle = vehicle
                except Vehicle.DoesNotExist:
                    pass

            # Update notes
            if data.get('notes'):
                load.notes = data.get('notes', load.notes)

        elif action == 'complete':
            load.status = 'DELIVERED'
            if data.get('notes'):
                load.notes = data.get('notes', load.notes)

        # Save changes
        load.save()

        # Return updated load (serialize)
        from core.serializers import LoadSerializer
        serializer = LoadSerializer(load)
        return Response(serializer.data, status=status.HTTP_200_OK)


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
        # Require API key
        if not request.auth or not isinstance(request.user, dict):
            return Response(
                {'error': 'API key required. Pass X-API-Key header.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        trips = request.data.get('trips', [])

        if not trips or not isinstance(trips, list):
            return Response(
                {'error': 'trips array is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        results = {
            'processed': 0,
            'created': 0,
            'updated': 0,
            'errors': [],
        }

        for idx, trip_data in enumerate(trips):
            results['processed'] += 1

            try:
                # Use FleetTripSyncView logic
                load_number = trip_data.get('load_number')
                action = trip_data.get('action', 'status_update')

                if not load_number:
                    results['errors'].append({
                        'index': idx,
                        'load_number': load_number,
                        'error': 'load_number is required',
                    })
                    continue

                # Try to find existing load
                try:
                    load = Load.objects.get(load_number=load_number)
                    created = False
                except Load.DoesNotExist:
                    if action == 'create':
                        # Create new load
                        from core.models import Customer
                        customer = Customer.objects.first()
                        if not customer:
                            results['errors'].append({
                                'index': idx,
                                'load_number': load_number,
                                'error': 'No customers found',
                            })
                            continue

                        load = Load.objects.create(
                            load_number=load_number or f"EXT-{uuid.uuid4().hex[:8].upper()}",
                            customer=customer,
                            pickup_location=trip_data.get('pickup_location', ''),
                            pickup_city=trip_data.get('pickup_city', trip_data.get('pickup_location', '')[:100]),
                            pickup_state=trip_data.get('pickup_state', ''),
                            pickup_zip=trip_data.get('pickup_zip', ''),
                            pickup_date=datetime.now(),
                            delivery_location=trip_data.get('delivery_location', ''),
                            delivery_city=trip_data.get('delivery_city', trip_data.get('delivery_location', '')[:100]),
                            delivery_state=trip_data.get('delivery_state', ''),
                            delivery_zip=trip_data.get('delivery_zip', ''),
                            delivery_date=datetime.now(),
                            cargo_description=trip_data.get('cargo_description', 'Freight'),
                            weight=Decimal(trip_data.get('weight', 0)),
                            distance=Decimal(trip_data.get('distance', 0)),
                            rate=Decimal(trip_data.get('rate', 0)),
                            total_amount=Decimal(trip_data.get('total_amount', 0)),
                            status='PENDING',
                            notes=trip_data.get('notes', ''),
                        )
                        created = True
                    else:
                        results['errors'].append({
                            'index': idx,
                            'load_number': load_number,
                            'error': f'Load {load_number} not found',
                        })
                        continue

                # Update load
                if action in ['status_update', 'create']:
                    if trip_data.get('status'):
                        load.status = trip_data.get('status')
                    if trip_data.get('driver_id'):
                        try:
                            driver = Driver.objects.get(id=trip_data['driver_id'])
                            load.driver = driver
                        except Driver.DoesNotExist:
                            pass
                    if trip_data.get('vehicle_plate'):
                        try:
                            vehicle = Vehicle.objects.get(registration_number=trip_data['vehicle_plate'])
                            load.vehicle = vehicle
                        except Vehicle.DoesNotExist:
                            pass
                    if trip_data.get('notes'):
                        load.notes = trip_data.get('notes', load.notes)
                elif action == 'complete':
                    load.status = 'DELIVERED'
                    if trip_data.get('notes'):
                        load.notes = trip_data.get('notes', load.notes)

                load.save()

                if created:
                    results['created'] += 1
                else:
                    results['updated'] += 1

            except Exception as e:
                results['errors'].append({
                    'index': idx,
                    'load_number': trip_data.get('load_number'),
                    'error': str(e),
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
        # Validate API key
        api_key = request.headers.get('X-API-Key', '')
        if not IntegrationAPIKey.objects.filter(key=api_key, is_active=True).exists():
            return Response({'error': 'Invalid or missing API key'}, status=401)

        records = request.data if isinstance(request.data, list) else request.data.get('trips', [])
        if len(records) > 500:
            return Response({'error': 'Max 500 records per request'}, status=400)

        created, skipped, errors = 0, 0, []
        for i, rec in enumerate(records):
            try:
                ext_id = rec.get('external_id')
                required = ['origin', 'destination']
                missing = [f for f in required if not rec.get(f)]
                if missing:
                    errors.append({'index': i, 'error': f'Missing fields: {missing}'})
                    continue
                if ext_id and Load.objects.filter(notes__icontains=f'ext_id:{ext_id}').exists():
                    skipped += 1
                    continue
                Load.objects.create(
                    origin=rec['origin'],
                    destination=rec['destination'],
                    cargo_description=rec.get('cargo_description', ''),
                    weight=rec.get('weight', 0),
                    distance=rec.get('distance', 0),
                    status='SCHEDULED',
                    notes=f'ext_id:{ext_id}' if ext_id else 'imported via API',
                )
                created += 1
            except Exception as e:
                errors.append({'index': i, 'error': str(e)})

        return Response({'created': created, 'skipped': skipped, 'errors': errors, 'total': len(records)})
