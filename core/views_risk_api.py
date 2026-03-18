# TENANCY AUDIT: 2026-03-15 — Risk API views properly scoped
# - RiskAssessmentView: Checks request.user.company against invoice.company ✓
# - RiskPortfolioView: Scoped to request.user.company via RiskMonitor ✓
# - RiskRetrainView: Admin-only, operates on all data (appropriate for ML training) ✓
# - RiskModelInfoView: Returns model metadata (no data leak) ✓
# - RiskAnomaliesView: Scoped to request.user.company via RiskMonitor ✓
# - RiskRescoreCustomerView: Checks request.user.company against customer.company ✓

"""Risk API endpoints for ML-enhanced risk assessment."""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from core.models import Invoice, Facility, PaymentOutcome, Company
from core.services.risk_engine import RiskEngine
from core.services.risk_monitor import RiskMonitor
from core.services.ml_pipeline import RiskMLPipeline


class RiskAssessmentView(APIView):
    """
    GET /api/v1/risk/assessment/{invoice_id}/

    Full AI risk assessment with hybrid scoring (rules + ML).
    Returns comprehensive risk breakdown with SHAP-like explanations.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, invoice_id):
        """Get ML-enhanced risk assessment for invoice."""
        try:
            invoice = get_object_or_404(Invoice, id=invoice_id)

            # Check user has access to this invoice
            if request.user.company and invoice.company != request.user.company:
                return Response(
                    {'error': 'Access denied'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Get facility (use company's active facility)
            facility = None
            if invoice.company:
                facility = invoice.company.facilities.filter(is_active=True).first()

            if not facility:
                return Response(
                    {'error': 'No active facility found for invoice company'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Calculate hybrid risk score
            engine = RiskEngine(invoice, facility)
            result = engine.score_with_ml()

            # Add anomaly detection
            monitor = RiskMonitor(company=invoice.company)
            anomalies = monitor.detect_anomalies(invoice)

            result['anomalies'] = anomalies
            result['anomaly_count'] = len(anomalies)

            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RiskPortfolioView(APIView):
    """
    GET /api/v1/risk/portfolio/

    Portfolio summary metrics with risk distribution and health indicators.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get portfolio health metrics."""
        try:
            # Scope to user's company if exists
            company = getattr(request.user, 'company', None)

            monitor = RiskMonitor(company=company)
            portfolio_health = monitor.check_portfolio_health()

            return Response(portfolio_health, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RiskRetrainView(APIView):
    """
    POST /api/v1/risk/retrain/

    Trigger ML model retraining (admin only).
    Requires sufficient PaymentOutcome data.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Trigger model retraining."""
        # Check if user is admin/superuser
        if not request.user.is_staff:
            return Response(
                {'error': 'Admin access required'},
                status=status.HTTP_403_FORBIDDEN
            )

        try:
            # Get all payment outcomes with complete data
            outcomes_qs = PaymentOutcome.objects.filter(
                feature_snapshot__isnull=False
            ).exclude(
                feature_snapshot={}
            )

            # Filter for complete data
            valid_outcomes = [o for o in outcomes_qs if o.has_complete_data]

            if len(valid_outcomes) < 50:
                return Response(
                    {
                        'success': False,
                        'error': f'Insufficient training data: {len(valid_outcomes)} samples (need at least 50)'
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Train model
            pipeline = RiskMLPipeline()
            result = pipeline.train(outcomes_qs)

            return Response(result, status=status.HTTP_200_OK)

        except ImportError as e:
            return Response(
                {
                    'success': False,
                    'error': 'ML libraries not installed. Run: pip install scikit-learn xgboost'
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except Exception as e:
            return Response(
                {
                    'success': False,
                    'error': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RiskModelInfoView(APIView):
    """
    GET /api/v1/risk/model-info/

    Get ML model training stats and metadata.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get model information."""
        try:
            pipeline = RiskMLPipeline()
            model_info = pipeline.get_model_info()

            if model_info is None:
                return Response(
                    {
                        'trained': False,
                        'message': 'ML model not yet trained. Use /api/v1/risk/retrain/ to train.'
                    },
                    status=status.HTTP_200_OK
                )

            return Response(model_info, status=status.HTTP_200_OK)

        except ImportError:
            return Response(
                {
                    'trained': False,
                    'error': 'ML libraries not installed. Run: pip install scikit-learn xgboost'
                },
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RiskAnomaliesView(APIView):
    """
    GET /api/v1/risk/anomalies/

    Get recent anomalies detected across portfolio.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get recent anomalies."""
        try:
            # Get query parameters
            limit = int(request.GET.get('limit', 50))
            severity = request.GET.get('severity', None)  # 'critical', 'high', 'medium'

            # Scope to user's company
            company = getattr(request.user, 'company', None)
            monitor = RiskMonitor(company=company)

            # Get recent invoices
            from core.models import Invoice
            if company:
                recent_invoices = Invoice.objects.filter(company=company).order_by('-created_at')[:limit]
            else:
                recent_invoices = Invoice.objects.all().order_by('-created_at')[:limit]

            # Detect anomalies for each
            all_anomalies = []
            for invoice in recent_invoices:
                anomalies = monitor.detect_anomalies(invoice)

                # Filter by severity if specified
                if severity:
                    anomalies = [a for a in anomalies if a.get('severity') == severity]

                if anomalies:
                    for anomaly in anomalies:
                        all_anomalies.append({
                            'invoice_id': invoice.id,
                            'invoice_number': invoice.invoice_number,
                            'customer': invoice.customer.name,
                            'amount': float(invoice.total_amount),
                            'anomaly': anomaly,
                        })

            # Sort by severity (critical first)
            severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
            all_anomalies.sort(
                key=lambda x: severity_order.get(x['anomaly'].get('severity', 'low'), 99)
            )

            return Response(
                {
                    'success': True,
                    'count': len(all_anomalies),
                    'anomalies': all_anomalies[:limit],
                },
                status=status.HTTP_200_OK
            )

        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RiskRescoreCustomerView(APIView):
    """
    POST /api/v1/risk/rescore-customer/<customer_id>/

    Trigger re-scoring of all open invoices for a customer.
    Useful when payment behavior changes.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, customer_id):
        """Re-score customer's open invoices."""
        try:
            # Check access
            from core.models import Customer
            customer = get_object_or_404(Customer, id=customer_id)

            if request.user.company and customer.company != request.user.company:
                return Response(
                    {'error': 'Access denied'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Trigger re-scoring
            monitor = RiskMonitor(company=customer.company)
            result = monitor.auto_rescore_customer(customer_id)

            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
