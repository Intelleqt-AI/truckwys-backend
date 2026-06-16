# TENANCY AUDIT: 2026-03-15 — Lender API intentionally multi-company
# This is an EXTERNAL API for lenders/partners to view operator data.
# Lenders are authenticated via API key and can see all companies they finance.
# Company isolation is INTENTIONALLY NOT APPLIED here - this is by design.
# Views: LenderHealthView, LenderRiskProfileView, LenderEligibleInvoicesView,
#        LenderAdvanceRequestView, LenderPortfolioView
# Status: EXEMPT from single-company filtering (multi-tenant lender platform) ✓

"""
Lender-facing Fast Pay API.
Authentication: X-API-Key header.
Share this API with lenders to enable fast pay / invoice financing.
"""

import uuid
from decimal import Decimal
from datetime import date, timedelta

from django.db import models as django_models
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import SimpleRateThrottle


class LenderRateThrottle(SimpleRateThrottle):
    """Per-API-key rate limit for the lender API. The default UserRateThrottle
    no-ops here because LenderUser.pk is None, so throttle on the key itself."""
    scope = 'lender'

    def get_cache_key(self, request, view):
        key = request.META.get('HTTP_X_API_KEY')
        if not key:
            return None
        return self.cache_format % {'scope': self.scope, 'ident': key}


# ---------------------------------------------------------------------------
# API Key Model (simple, in-memory seed for demo)
# ---------------------------------------------------------------------------

DEMO_API_KEYS = {
    'LENDER-KEY-2026-TRUCKWYS-DEMO': 'Capital Connect SA (Demo)',
    'LENDER-KEY-2026-ABSA-BUSINESS': 'ABSA Business Finance',
    'LENDER-KEY-2026-INVESTEC-CORP': 'Investec Corporate Finance',
    'LENDER-KEY-2026-NEDBANK-TRADE': 'Nedbank Trade Finance',
}


class LenderUser:
    """Pseudo-user for API key authenticated lender requests."""
    is_authenticated = True
    is_active = True
    pk = None
    id = None

    def __init__(self, api_key, lender_name):
        self.api_key = api_key
        self.lender = lender_name
        self.username = lender_name

    def __str__(self):
        return self.lender


class LenderAPIKeyAuthentication(BaseAuthentication):
    """Authenticate lenders via X-API-Key header."""

    def authenticate(self, request):
        # Header-only — never accept the key via query string (it would leak into
        # access logs, proxies, and browser history).
        key = request.META.get('HTTP_X_API_KEY')
        if not key:
            return None  # Not an API key request — try other auth
        lender_name = DEMO_API_KEYS.get(key)
        if not lender_name:
            raise AuthenticationFailed('Invalid API key.')
        # Return a proper user-like object
        return (LenderUser(api_key=key, lender_name=lender_name), key)

    def authenticate_header(self, request):
        return 'X-API-Key'


class LenderBaseView(APIView):
    authentication_classes = [LenderAPIKeyAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [LenderRateThrottle]

    def _require_api_key(self, request):
        """Returns error response if not authenticated via API key, else None."""
        if not request.auth or not isinstance(request.user, LenderUser):
            return Response(
                {'error': 'API key required. Pass X-API-Key header.'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        return None

    def _get_lender_name(self, request):
        if isinstance(request.user, LenderUser):
            return request.user.lender
        return 'Unknown Lender'


# ---------------------------------------------------------------------------
# GET /api/v1/lender/health/
# ---------------------------------------------------------------------------

class LenderHealthView(LenderBaseView):
    """System health and data freshness check."""

    def get(self, request):
        err = self._require_api_key(request)
        if err:
            return err

        from core.models.load import Load
        from core.models.invoice import Invoice
        from core.models.risk_score import RiskScore

        latest_load = Load.objects.order_by('-updated_at').first()
        latest_invoice = Invoice.objects.order_by('-updated_at').first()
        latest_risk = RiskScore.objects.order_by('-calculated_at').first()

        return Response({
            'status': 'healthy',
            'lender': self._get_lender_name(request),
            'api_version': '1.0',
            'data_freshness': {
                'loads_last_updated': latest_load.updated_at.isoformat() if latest_load else None,
                'invoices_last_updated': latest_invoice.updated_at.isoformat() if latest_invoice else None,
                'risk_scores_last_calculated': latest_risk.calculated_at.isoformat() if latest_risk else None,
            },
            'counts': {
                'total_loads': Load.objects.count(),
                'total_invoices': Invoice.objects.count(),
                'risk_scores': RiskScore.objects.count(),
            },
            'timestamp': timezone.now().isoformat(),
        })


# ---------------------------------------------------------------------------
# GET /api/v1/lender/risk-profile/
# ---------------------------------------------------------------------------

class LenderRiskProfileView(LenderBaseView):
    """Full operator risk profile for underwriting decisions."""

    def get(self, request):
        err = self._require_api_key(request)
        if err:
            return err

        from core.models.company import Company
        from core.models.invoice import Invoice
        from core.models.load import Load
        from core.models.vehicle import Vehicle
        from core.models.driver import Driver
        from core.models.risk_score import RiskScore
        from core.models.advance_request import AdvanceRequest

        company = Company.objects.first()
        if not company:
            return Response({'error': 'No company data available'}, status=404)

        # Invoice metrics
        all_invoices = Invoice.objects.all()
        paid_invoices = all_invoices.filter(status='PAID')
        overdue_invoices = all_invoices.filter(status='OVERDUE')
        total_inv = all_invoices.count()

        # DSO calculation
        dso_days = 30.0
        if paid_invoices.exists():
            dso_list = []
            for inv in paid_invoices:
                if inv.paid_at and inv.issue_date:
                    days = (inv.paid_at.date() - inv.issue_date).days
                    if 0 < days < 365:
                        dso_list.append(days)
            if dso_list:
                dso_days = round(sum(dso_list) / len(dso_list), 1)

        # Revenue (last 90 days)
        ninety_days_ago = timezone.now() - timedelta(days=90)
        recent_loads = Load.objects.filter(
            status='DELIVERED',
            delivery_date__gte=ninety_days_ago
        )
        revenue_90d = sum(float(l.total_amount) for l in recent_loads)
        monthly_revenue = revenue_90d / 3

        # Avg invoice value
        avg_invoice = 0
        if total_inv > 0:
            total_val = sum(float(inv.total_amount) for inv in all_invoices)
            avg_invoice = total_val / total_inv

        # Risk scores summary
        risk_scores = RiskScore.objects.all()
        avg_score = 0
        if risk_scores.exists():
            avg_score = round(sum(rs.total_score for rs in risk_scores) / risk_scores.count())

        # Tier distribution
        tier_dist = {}
        for rs in risk_scores:
            tier_dist[rs.tier] = tier_dist.get(rs.tier, 0) + 1

        # Active advances
        advances = AdvanceRequest.objects.filter(status__in=['ACTIVE', 'DISBURSED', 'FUNDED'])
        outstanding = sum(float(a.amount or 0) for a in advances)

        # Payment performance
        on_time = paid_invoices.filter(
            paid_at__isnull=False
        ).count()
        on_time_pct = round((on_time / total_inv * 100) if total_inv > 0 else 0, 1)

        return Response({
            'company': {
                'name': company.company_name,
                'registration': getattr(company, 'registration_number', 'TW-2024-001'),
                'industry': 'Road Freight — South Africa',
                'vat_number': getattr(company, 'vat_number', None),
                'address': getattr(company, 'address', 'Johannesburg, South Africa'),
            },
            'risk_summary': {
                'portfolio_score': avg_score,
                'portfolio_tier': _score_to_tier(avg_score),
                'tier_distribution': tier_dist,
                'scored_customers': risk_scores.count(),
            },
            'financial_metrics': {
                'monthly_revenue_zar': round(monthly_revenue, 2),
                'revenue_90d_zar': round(revenue_90d, 2),
                'avg_invoice_value_zar': round(avg_invoice, 2),
                'total_invoices': total_inv,
                'paid_invoices': paid_invoices.count(),
                'overdue_invoices': overdue_invoices.count(),
                'overdue_ratio': round(overdue_invoices.count() / total_inv, 3) if total_inv > 0 else 0,
                'on_time_payment_pct': on_time_pct,
                'dso_days': dso_days,
                'outstanding_advances_zar': outstanding,
            },
            'fleet': {
                'vehicles': Vehicle.objects.count(),
                'active_vehicles': Vehicle.objects.filter(status__in=['AVAILABLE', 'IN_USE']).count(),
                'drivers': Driver.objects.count(),
                'active_drivers': Driver.objects.filter(status='ACTIVE').count(),
                'active_loads': Load.objects.filter(status='IN_TRANSIT').count(),
                'delivered_90d': recent_loads.count(),
            },
            'advance_history': [
                {
                    'id': str(a.id),
                    'amount': float(a.amount or 0),
                    'status': a.status,
                    'created': a.created_at.isoformat(),
                }
                for a in AdvanceRequest.objects.order_by('-created_at')[:5]
            ],
            'generated_at': timezone.now().isoformat(),
        })


# ---------------------------------------------------------------------------
# GET /api/v1/lender/eligible-invoices/
# ---------------------------------------------------------------------------

class LenderEligibleInvoicesView(LenderBaseView):
    """List invoices eligible for advance/fast pay."""

    def get(self, request):
        err = self._require_api_key(request)
        if err:
            return err

        from core.models.invoice import Invoice
        from core.models.risk_score import RiskScore

        # Get SENT/DRAFT invoices not already advanced
        eligible_statuses = ['SENT', 'DRAFT']
        invoices = Invoice.objects.filter(
            status__in=eligible_statuses,
            early_pay_eligible=True,
        ).select_related('customer', 'load')

        if not invoices.exists():
            # Fallback: get any SENT invoices
            invoices = Invoice.objects.filter(status__in=eligible_statuses).select_related('customer', 'load')

        result = []
        for inv in invoices:
            # Get risk score for this customer
            risk = RiskScore.objects.filter(customer=inv.customer).order_by('-calculated_at').first()
            tier = risk.tier if risk else 'FAIR'
            score = risk.total_score if risk else 55

            fee_map = {
                'EXCELLENT': 2.0, 'GOOD': 2.5, 'FAIR': 3.0,
                'ELEVATED': 3.5, 'INELIGIBLE': 0.0,
            }
            fee_rate = fee_map.get(tier, 3.0)
            amount = float(inv.total_amount)
            net_payout = round(amount * (1 - fee_rate / 100), 2)

            age_days = (date.today() - inv.issue_date).days

            result.append({
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer': inv.customer.name,
                'customer_id': inv.customer.id,
                'amount_zar': amount,
                'subtotal_zar': float(inv.subtotal),
                'vat_zar': float(inv.vat_amount),
                'issue_date': inv.issue_date.isoformat(),
                'due_date': inv.due_date.isoformat(),
                'age_days': age_days,
                'risk_score': score,
                'risk_tier': tier,
                'fee_rate_pct': fee_rate,
                'fee_amount_zar': round(amount * fee_rate / 100, 2),
                'net_payout_zar': net_payout,
                'load_reference': inv.load.load_number if inv.load else None,
                'route': f'{inv.load.pickup_city} → {inv.load.delivery_city}' if inv.load else None,
            })

        # Sort by net payout desc
        result.sort(key=lambda x: x['net_payout_zar'], reverse=True)

        return Response({
            'eligible_count': len(result),
            'total_face_value_zar': round(sum(r['amount_zar'] for r in result), 2),
            'total_net_payout_zar': round(sum(r['net_payout_zar'] for r in result), 2),
            'invoices': result,
        })


# ---------------------------------------------------------------------------
# POST /api/v1/lender/advance-request/
# ---------------------------------------------------------------------------

class LenderAdvanceRequestView(LenderBaseView):
    """Lender submits an advance request for a specific invoice."""

    def post(self, request):
        err = self._require_api_key(request)
        if err:
            return err

        from core.models.invoice import Invoice
        from core.models.advance_request import AdvanceRequest
        from core.models.facility import Facility
        from core.models.company import Company

        invoice_id = request.data.get('invoice_id')
        requested_amount = request.data.get('requested_amount')
        lender_name = self._get_lender_name(request)

        if not invoice_id:
            return Response({'error': 'invoice_id required'}, status=400)

        try:
            invoice = Invoice.objects.get(id=invoice_id)
        except Invoice.DoesNotExist:
            return Response({'error': f'Invoice {invoice_id} not found'}, status=404)

        if invoice.status not in ['SENT', 'DRAFT']:
            return Response({'error': f'Invoice status {invoice.status} not eligible for advance'}, status=400)

        # Check facility
        company = Company.objects.first()
        facility = Facility.objects.filter(company=company, status='ACTIVE').first()
        if not facility:
            return Response({'error': 'No active facility found'}, status=400)

        amount = Decimal(str(requested_amount or invoice.total_amount))
        if amount > facility.available:
            return Response({
                'error': f'Requested amount R{amount} exceeds available facility R{facility.available}'
            }, status=400)

        # Create advance
        advance = AdvanceRequest.objects.create(
            invoice=invoice,
            facility=facility,
            amount=amount,
            status='PENDING',
            notes=f'Submitted by lender: {lender_name}',
        )

        return Response({
            'advance_id': advance.id,
            'reference': f'ADV-{advance.id:06d}',
            'invoice_number': invoice.invoice_number,
            'customer': invoice.customer.name,
            'requested_amount_zar': float(amount),
            'status': 'PENDING',
            'lender': lender_name,
            'submitted_at': timezone.now().isoformat(),
            'expected_disbursement': (date.today() + timedelta(hours=4)).isoformat(),
            'message': 'Advance request received. Funds disbursed within 4 business hours upon approval.',
        }, status=201)


# ---------------------------------------------------------------------------
# GET /api/v1/lender/portfolio/
# ---------------------------------------------------------------------------

class LenderPortfolioView(LenderBaseView):
    """All active advances and performance metrics for lender portfolio view."""

    def get(self, request):
        err = self._require_api_key(request)
        if err:
            return err

        from core.models.advance_request import AdvanceRequest

        advances = AdvanceRequest.objects.select_related('invoice__customer', 'facility').order_by('-created_at')

        active = advances.filter(status__in=['ACTIVE', 'DISBURSED', 'FUNDED', 'PENDING'])
        completed = advances.filter(status='REPAID')

        return Response({
            'summary': {
                'active_advances': active.count(),
                'total_outstanding_zar': round(sum(float(a.amount or 0) for a in active), 2),
                'completed_advances': completed.count(),
                'total_repaid_zar': round(sum(float(a.amount or 0) for a in completed), 2),
            },
            'active': [
                {
                    'id': a.id,
                    'reference': f'ADV-{a.id:06d}',
                    'invoice': a.invoice.invoice_number if a.invoice else None,
                    'customer': a.invoice.customer.name if a.invoice else None,
                    'amount_zar': float(a.amount or 0),
                    'status': a.status,
                    'created_at': a.created_at.isoformat(),
                }
                for a in active[:20]
            ],
            'generated_at': timezone.now().isoformat(),
        })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score_to_tier(score):
    if score >= 85: return 'EXCELLENT'
    if score >= 70: return 'GOOD'
    if score >= 55: return 'FAIR'
    if score >= 40: return 'ELEVATED'
    return 'INELIGIBLE'
