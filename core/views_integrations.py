"""
Integration Views
Handles API endpoints for third-party integrations (Xero, Fleet software, Credit bureaus)
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import redirect
from django.conf import settings
from core.models import Company, Invoice
from core.integrations.xero import XeroClient
from core.integrations.credit_bureau import CreditBureauService
from core.integrations.fleet import ManualFleetIntegration
from core.services.intelligence import IntelligenceService
from core.services.cashflow import CashFlowForecastService
import csv
import io
from decimal import Decimal
from datetime import datetime


class XeroConnectView(APIView):
    """
    Initiate Xero OAuth connection.
    GET /api/v1/integrations/xero/connect/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Get company (assuming single company per deployment)
        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found. Please create company profile first.'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Initialize Xero client
        xero_client = XeroClient(company)

        # Generate authorization URL
        auth_url = xero_client.get_authorization_url()

        # Redirect to Xero OAuth
        return redirect(auth_url)


class XeroCallbackView(APIView):
    """
    Handle Xero OAuth callback.
    GET /api/v1/integrations/xero/callback/?code=...
    """
    permission_classes = []  # Public endpoint for OAuth callback

    def get(self, request):
        code = request.GET.get('code')
        error = request.GET.get('error')

        if error:
            return Response(
                {'error': f'Xero authorization failed: {error}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not code:
            return Response(
                {'error': 'No authorization code provided'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get company
        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Initialize Xero client and exchange code for tokens
        xero_client = XeroClient(company)

        try:
            token_data = xero_client.handle_callback(code)

            return Response({
                'success': True,
                'message': 'Xero connected successfully',
                'connected_at': company.xero_connected_at,
                'tenant_id': company.xero_tenant_id,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'Failed to connect Xero: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class XeroDisconnectView(APIView):
    """
    Disconnect Xero integration.
    POST /api/v1/integrations/xero/disconnect/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        xero_client = XeroClient(company)
        xero_client.disconnect()

        return Response({
            'success': True,
            'message': 'Xero disconnected successfully',
        }, status=status.HTTP_200_OK)


class XeroStatusView(APIView):
    """
    Get Xero connection status.
    GET /api/v1/integrations/xero/status/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        xero_client = XeroClient(company)

        return Response({
            'connected': xero_client.is_connected,
            'connected_at': company.xero_connected_at,
            'tenant_id': company.xero_tenant_id,
            'token_expires_at': company.xero_token_expires_at,
        }, status=status.HTTP_200_OK)


class XeroSyncInvoicesView(APIView):
    """
    Push pending invoices to Xero.
    POST /api/v1/integrations/xero/sync-invoices/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        xero_client = XeroClient(company)

        if not xero_client.is_connected:
            return Response(
                {'error': 'Xero not connected. Please connect first.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get invoices to sync (SENT or VIEWED status, not cancelled)
        invoices_to_sync = Invoice.objects.filter(
            status__in=['SENT', 'VIEWED', 'OVERDUE']
        ).exclude(status='CANCELLED')

        results = {
            'total': invoices_to_sync.count(),
            'success': 0,
            'failed': 0,
            'errors': [],
        }

        for invoice in invoices_to_sync:
            try:
                xero_client.push_invoice(invoice)
                results['success'] += 1
            except Exception as e:
                results['failed'] += 1
                results['errors'].append({
                    'invoice': invoice.invoice_number,
                    'error': str(e),
                })

        return Response(results, status=status.HTTP_200_OK)


class XeroSyncPaymentsView(APIView):
    """
    Pull payments from Xero.
    POST /api/v1/integrations/xero/sync-payments/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        xero_client = XeroClient(company)

        if not xero_client.is_connected:
            return Response(
                {'error': 'Xero not connected. Please connect first.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            synced_payments = xero_client.sync_payments()

            return Response({
                'success': True,
                'payments': synced_payments,
                'total': len(synced_payments),
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': f'Failed to sync payments: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


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
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = Company.objects.first()
        if not company:
            return Response(
                {'error': 'Company not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Generate intelligence recommendations
        intelligence_service = IntelligenceService(company)
        recommendations = intelligence_service.generate_recommendations()

        # Optionally create notifications
        create_notifications = request.query_params.get('create_notifications', 'false').lower() == 'true'

        if create_notifications and recommendations:
            intelligence_service.create_notifications(request.user, recommendations)

        return Response({
            'recommendations': recommendations,
            'total': len(recommendations),
            'by_type': self._group_by_type(recommendations),
            'by_severity': self._group_by_severity(recommendations),
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
    GET /api/v1/dashboard/cashflow/?days=90
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Get forecast period from query params
        days = int(request.query_params.get('days', 90))

        # Validate days
        if days < 1 or days > 365:
            return Response(
                {'error': 'Days must be between 1 and 365'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Generate forecast
        cashflow_service = CashFlowForecastService()
        forecast = cashflow_service.forecast_cashflow(days=days)
        summary = cashflow_service.get_summary_stats(forecast)

        return Response({
            'forecast': forecast,
            'summary': summary,
            'period_days': days,
        }, status=status.HTTP_200_OK)
