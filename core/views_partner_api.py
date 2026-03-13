"""Partner/Capital API endpoints for external lender and partner integration."""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import viewsets
from rest_framework.decorators import action
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiExample
from drf_spectacular.types import OpenApiTypes
from django.db.models import Count, Avg, Sum, Q
from django.utils import timezone
from decimal import Decimal

from core.models import Invoice, RiskScore, Customer, WebhookSubscription
from core.auth import APIKeyAuthentication


class PartnerRiskAssessmentView(APIView):
    """Full risk assessment for a specific invoice."""

    authentication_classes = [APIKeyAuthentication]
    permission_classes = []

    @extend_schema(
        tags=['Partner API'],
        summary='Get risk assessment for invoice',
        description='Returns full risk assessment including score, tier, confidence, feature breakdown, and top risk drivers',
        parameters=[
            OpenApiParameter(
                name='X-API-Key',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='Partner API key',
                required=True
            )
        ],
        responses={
            200: {
                'description': 'Risk assessment data',
                'content': {
                    'application/json': {
                        'example': {
                            'invoice_id': 123,
                            'invoice_number': 'INV-2024-001',
                            'total_score': 85,
                            'tier': 'PRIME',
                            'confidence': 0.92,
                            'is_eligible': True,
                            'fee_percent': 2.5,
                            'fee_amount': 1250.00,
                            'features': {
                                'client_identity': 90,
                                'client_financial': 85,
                                'debtor_credit': 88,
                                'invoice_chars': 82,
                                'pod_docs': 80,
                                'operational': 85,
                                'macro_market': 75
                            },
                            'top_risk_drivers': [
                                {'factor': 'payment_history', 'impact': 'positive', 'score': 95},
                                {'factor': 'debtor_credit', 'impact': 'positive', 'score': 88}
                            ],
                            'calculated_at': '2024-01-15T10:30:00Z',
                            'expires_at': '2024-01-22T10:30:00Z'
                        }
                    }
                }
            },
            404: {'description': 'Invoice not found'}
        }
    )
    def get(self, request, invoice_id):
        """Get full risk assessment for invoice."""
        try:
            invoice = Invoice.objects.get(id=invoice_id)
        except Invoice.DoesNotExist:
            return Response(
                {'error': 'Invoice not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Get latest risk score
        try:
            risk_score = RiskScore.objects.filter(
                invoice=invoice,
                is_expired=False
            ).latest('calculated_at')
        except RiskScore.DoesNotExist:
            return Response(
                {'error': 'No risk assessment available for this invoice'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Build feature breakdown
        features = {
            'client_identity': risk_score.factor_client_identity,
            'client_financial': risk_score.factor_client_financial,
            'debtor_credit': risk_score.factor_debtor_credit,
            'invoice_chars': risk_score.factor_invoice_chars,
            'pod_docs': risk_score.factor_pod_docs,
            'operational': risk_score.factor_operational,
            'macro_market': risk_score.factor_macro_market,
        }

        # Calculate top risk drivers
        top_drivers = []
        for factor, score in features.items():
            if score >= 80:
                top_drivers.append({
                    'factor': factor,
                    'impact': 'positive',
                    'score': score
                })
            elif score <= 50:
                top_drivers.append({
                    'factor': factor,
                    'impact': 'negative',
                    'score': score
                })

        # Sort by absolute distance from 70 (neutral)
        top_drivers.sort(key=lambda x: abs(x['score'] - 70), reverse=True)
        top_drivers = top_drivers[:5]  # Top 5 drivers

        return Response({
            'invoice_id': invoice.id,
            'invoice_number': invoice.invoice_number,
            'total_score': risk_score.total_score,
            'tier': risk_score.tier,
            'confidence': 0.92,  # TODO: Calculate actual confidence
            'is_eligible': risk_score.is_eligible,
            'fee_percent': float(risk_score.fee_percent),
            'fee_amount': float(risk_score.fee_amount),
            'features': features,
            'top_risk_drivers': top_drivers,
            'calculated_at': risk_score.calculated_at.isoformat(),
            'expires_at': risk_score.expires_at.isoformat(),
        })


class PartnerPortfolioSummaryView(APIView):
    """Portfolio metrics for partner."""

    authentication_classes = [APIKeyAuthentication]
    permission_classes = []

    @extend_schema(
        tags=['Partner API'],
        summary='Get portfolio summary',
        description='Returns portfolio metrics including total exposure, avg risk score, default rate, concentration by customer',
        parameters=[
            OpenApiParameter(
                name='X-API-Key',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='Partner API key',
                required=True
            )
        ],
        responses={
            200: {
                'description': 'Portfolio summary',
                'content': {
                    'application/json': {
                        'example': {
                            'total_exposure': 1500000.00,
                            'invoice_count': 150,
                            'avg_risk_score': 78.5,
                            'default_rate': 0.02,
                            'tier_distribution': {
                                'PRIME': 45,
                                'STANDARD': 60,
                                'ELEVATED': 30,
                                'HIGH': 15
                            },
                            'concentration': [
                                {'customer_name': 'ABC Corp', 'exposure': 250000.00, 'percentage': 16.7},
                                {'customer_name': 'XYZ Ltd', 'exposure': 180000.00, 'percentage': 12.0}
                            ]
                        }
                    }
                }
            }
        }
    )
    def get(self, request):
        """Get portfolio summary for partner."""
        # Get all eligible invoices with risk scores
        invoices = Invoice.objects.filter(
            early_pay_eligible=True,
            status__in=['SENT', 'VIEWED', 'OVERDUE']
        )

        # Calculate total exposure
        total_exposure = invoices.aggregate(
            total=Sum('total_amount')
        )['total'] or Decimal('0.00')

        # Get average risk score
        risk_scores = RiskScore.objects.filter(
            invoice__in=invoices,
            is_eligible=True
        )
        avg_score = risk_scores.aggregate(
            avg=Avg('total_score')
        )['avg'] or 0

        # Tier distribution
        tier_dist = risk_scores.values('tier').annotate(
            count=Count('id')
        )
        tier_distribution = {item['tier']: item['count'] for item in tier_dist}

        # Customer concentration (top 10)
        concentration = invoices.values('customer__name').annotate(
            exposure=Sum('total_amount')
        ).order_by('-exposure')[:10]

        concentration_list = []
        for item in concentration:
            percentage = (item['exposure'] / total_exposure * 100) if total_exposure > 0 else 0
            concentration_list.append({
                'customer_name': item['customer__name'],
                'exposure': float(item['exposure']),
                'percentage': round(float(percentage), 2)
            })

        return Response({
            'total_exposure': float(total_exposure),
            'invoice_count': invoices.count(),
            'avg_risk_score': round(float(avg_score), 2),
            'default_rate': 0.02,  # TODO: Calculate actual default rate
            'tier_distribution': tier_distribution,
            'concentration': concentration_list
        })


class PartnerEligibleInvoicesView(APIView):
    """List eligible invoices with pre-computed scores."""

    authentication_classes = [APIKeyAuthentication]
    permission_classes = []

    @extend_schema(
        tags=['Partner API'],
        summary='List eligible invoices',
        description='Returns list of eligible invoices with pre-computed risk scores',
        parameters=[
            OpenApiParameter(
                name='X-API-Key',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='Partner API key',
                required=True
            ),
            OpenApiParameter(
                name='min_score',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Minimum risk score filter',
                required=False
            ),
            OpenApiParameter(
                name='tier',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description='Risk tier filter (PRIME, STANDARD, ELEVATED, HIGH)',
                required=False
            )
        ],
        responses={
            200: {
                'description': 'List of eligible invoices',
                'content': {
                    'application/json': {
                        'example': {
                            'invoices': [
                                {
                                    'invoice_id': 123,
                                    'invoice_number': 'INV-2024-001',
                                    'customer_name': 'ABC Corp',
                                    'amount': 50000.00,
                                    'issue_date': '2024-01-15',
                                    'due_date': '2024-02-14',
                                    'risk_score': 85,
                                    'tier': 'PRIME',
                                    'fee_percent': 2.5,
                                    'fee_amount': 1250.00
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    def get(self, request):
        """Get list of eligible invoices with scores."""
        # Filter parameters
        min_score = request.query_params.get('min_score')
        tier = request.query_params.get('tier')

        # Base query
        risk_scores = RiskScore.objects.filter(
            is_eligible=True,
            invoice__early_pay_eligible=True,
            invoice__status__in=['SENT', 'VIEWED', 'OVERDUE']
        ).select_related('invoice', 'customer')

        # Apply filters
        if min_score:
            try:
                risk_scores = risk_scores.filter(total_score__gte=int(min_score))
            except ValueError:
                pass

        if tier:
            risk_scores = risk_scores.filter(tier=tier.upper())

        # Build response
        invoices = []
        for rs in risk_scores[:100]:  # Limit to 100
            invoices.append({
                'invoice_id': rs.invoice.id,
                'invoice_number': rs.invoice.invoice_number,
                'customer_name': rs.customer.name,
                'amount': float(rs.invoice.total_amount),
                'issue_date': rs.invoice.issue_date.isoformat(),
                'due_date': rs.invoice.due_date.isoformat(),
                'risk_score': rs.total_score,
                'tier': rs.tier,
                'fee_percent': float(rs.fee_percent),
                'fee_amount': float(rs.fee_amount),
            })

        return Response({'invoices': invoices})


class PartnerWebhookSubscriptionViewSet(viewsets.ViewSet):
    """Manage webhook subscriptions for partners."""

    authentication_classes = [APIKeyAuthentication]
    permission_classes = []

    @extend_schema(
        tags=['Partner API'],
        summary='Subscribe to webhook events',
        description='Create a new webhook subscription for event notifications',
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'webhook_url': {'type': 'string', 'format': 'uri'},
                    'events': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'example': ['invoice.created', 'invoice.paid', 'risk_score.updated']
                    }
                }
            }
        },
        responses={
            201: {
                'description': 'Subscription created',
                'content': {
                    'application/json': {
                        'example': {
                            'subscription_id': 1,
                            'webhook_url': 'https://partner.example.com/webhooks',
                            'events': ['invoice.created', 'invoice.paid'],
                            'secret': 'whsec_abc123...',
                            'created_at': '2024-01-15T10:30:00Z'
                        }
                    }
                }
            }
        }
    )
    def create(self, request):
        """Create webhook subscription."""
        webhook_url = request.data.get('webhook_url')
        events = request.data.get('events', [])

        if not webhook_url:
            return Response(
                {'error': 'webhook_url is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get partner name from authenticated user
        partner_name = request.user.partner_name if hasattr(request.user, 'partner_name') else 'Unknown Partner'

        # Create subscription
        subscription = WebhookSubscription.objects.create(
            partner_name=partner_name,
            webhook_url=webhook_url,
            events=events,
            is_active=True
        )

        return Response({
            'subscription_id': subscription.id,
            'webhook_url': subscription.webhook_url,
            'events': subscription.events,
            'secret': subscription.secret,
            'created_at': subscription.created_at.isoformat()
        }, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=['Partner API'],
        summary='Unsubscribe from webhook',
        description='Delete a webhook subscription',
        responses={
            204: {'description': 'Subscription deleted'},
            404: {'description': 'Subscription not found'}
        }
    )
    def destroy(self, request, pk=None):
        """Delete webhook subscription."""
        try:
            subscription = WebhookSubscription.objects.get(id=pk)
            subscription.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except WebhookSubscription.DoesNotExist:
            return Response(
                {'error': 'Subscription not found'},
                status=status.HTTP_404_NOT_FOUND
            )

    @extend_schema(
        tags=['Partner API'],
        summary='List webhook subscriptions',
        description='Get all webhook subscriptions for the authenticated partner',
        responses={
            200: {
                'description': 'List of subscriptions',
                'content': {
                    'application/json': {
                        'example': {
                            'subscriptions': [
                                {
                                    'subscription_id': 1,
                                    'webhook_url': 'https://partner.example.com/webhooks',
                                    'events': ['invoice.created'],
                                    'is_active': True,
                                    'created_at': '2024-01-15T10:30:00Z'
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    def list(self, request):
        """List all webhook subscriptions."""
        # Get subscriptions for this API key
        if hasattr(request, 'auth') and request.auth:
            subscriptions = WebhookSubscription.objects.filter(
                api_key=request.auth.api_key
            )
        else:
            subscriptions = WebhookSubscription.objects.all()

        data = []
        for sub in subscriptions:
            data.append({
                'subscription_id': sub.id,
                'webhook_url': sub.webhook_url,
                'events': sub.events,
                'is_active': sub.is_active,
                'created_at': sub.created_at.isoformat(),
                'last_delivery_at': sub.last_delivery_at.isoformat() if sub.last_delivery_at else None,
                'failure_count': sub.failure_count
            })

        return Response({'subscriptions': data})
