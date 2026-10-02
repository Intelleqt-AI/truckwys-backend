# TENANCY (capital-safety 2026-10, audit §6 #2): the 2026-03-15 note that this
# API was "intentionally multi-company" meant any valid key saw and could raise
# advances on EVERY tenant's invoices. Rules now:
# - a lender key is an IntegrationAPIKey(key_type='LENDER') bound to the
#   transporters it funds via allowed_companies; every query is filtered to
#   request.user.company_ids
# - no bound companies => sees nothing (fail closed). Legacy LENDER_API_KEYS env
#   keys still authenticate (health check) but are bound to nothing
# - advances only on collectable invoices (SENT/VIEWED/OVERDUE/PARTIALLY_PAID)
#   with a load + POD, early_pay_eligible, no active advance, and an amount in
#   (0, balance]

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
# API Key registry — loaded from the environment, NEVER hardcoded in source.
# ---------------------------------------------------------------------------
import os
import json
from django.conf import settings


def _load_lender_api_keys() -> dict:
    """Lender API keys come from the LENDER_API_KEYS env var.

    Accepted formats:
      - JSON object: {"KEY1": "Lender One", "KEY2": "Lender Two"}
      - CSV pairs:   "KEY1:Lender One,KEY2:Lender Two"

    In production (DEBUG=False) an unset/empty var means the lender API is
    effectively closed — no keys, no access. A single throwaway demo key is
    provided ONLY in local development (DEBUG=True) so the sandbox is testable.
    """
    raw = os.environ.get('LENDER_API_KEYS', '').strip()
    keys: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                keys = {str(k): str(v) for k, v in parsed.items()}
        except (ValueError, TypeError):
            for pair in raw.split(','):
                if ':' in pair:
                    k, name = pair.split(':', 1)
                    if k.strip():
                        keys[k.strip()] = name.strip() or 'Lender'
    if not keys and getattr(settings, 'DEBUG', False):
        # Local-dev sandbox key only. Not present when DEBUG=False.
        keys = {'LENDER-KEY-DEV-SANDBOX': 'Sandbox Lender (dev only)'}
    return keys


# Resolved once at import. Real keys are supplied via env per deployment.
DEMO_API_KEYS = _load_lender_api_keys()


class LenderUser:
    """Pseudo-user for API key authenticated lender requests."""
    is_authenticated = True
    is_active = True
    is_staff = False
    is_superuser = False
    pk = None
    id = None

    def __init__(self, api_key, lender_name, company_ids=frozenset(), key_obj=None):
        self.api_key = api_key
        self.lender = lender_name
        self.username = lender_name
        # The transporters this key may see. Empty => nothing (fail closed).
        self.company_ids = frozenset(company_ids)
        self.key_obj = key_obj

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

        from core.models.integration_api_key import IntegrationAPIKey
        key_obj = IntegrationAPIKey.objects.filter(
            key=key, active=True, key_type='LENDER',
        ).first()
        if key_obj is not None:
            if not key_obj.is_ip_allowed(_client_ip(request)):
                raise AuthenticationFailed('API key not permitted from this IP.')
            company_ids = set(key_obj.allowed_companies.values_list('id', flat=True))
            return (LenderUser(api_key=key, lender_name=key_obj.name,
                               company_ids=company_ids, key_obj=key_obj), key)

        lender_name = DEMO_API_KEYS.get(key)
        if not lender_name:
            raise AuthenticationFailed('Invalid API key.')
        # Env keys carry no tenant binding, so they see no tenant data.
        return (LenderUser(api_key=key, lender_name=lender_name), key)

    def authenticate_header(self, request):
        return 'X-API-Key'


def _client_ip(request) -> str:
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


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

    def _company_ids(self, request):
        """Transporters this key is bound to (empty set => sees nothing)."""
        if isinstance(request.user, LenderUser):
            return request.user.company_ids
        return frozenset()

    def _funder_matches(self, request, invoice) -> bool:
        """A key bound to a funder acts only on that funder's lines. Keys from
        before the funder model (no funder) keep their company binding only."""
        key = getattr(request.user, 'key_obj', None)
        funder_id = getattr(key, 'funder_id', None)
        if not funder_id:
            return True
        from core.models import Facility
        return Facility.objects.filter(company_id=invoice.company_id, status='ACTIVE',
                                       funder_id=funder_id).exists()


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

        # Platform-wide counts/timestamps would disclose other tenants' volume.
        ids = self._company_ids(request)
        loads = Load.objects.filter(company_id__in=ids)
        invoices = Invoice.objects.filter(company_id__in=ids)
        risks = RiskScore.objects.filter(company_id__in=ids)
        latest_load = loads.order_by('-updated_at').first()
        latest_invoice = invoices.order_by('-updated_at').first()
        latest_risk = risks.order_by('-calculated_at').first()

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
                'total_loads': loads.count(),
                'total_invoices': invoices.count(),
                'risk_scores': risks.count(),
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

        # Was Company.objects.first(): every key got the first tenant's profile
        # (and platform-wide metrics). Now the caller names a bound company,
        # or gets its only one.
        ids = self._company_ids(request)
        raw_id = request.query_params.get('company_id')
        if raw_id:
            try:
                company_id = int(raw_id)
            except (TypeError, ValueError):
                return Response({'error': 'company_id must be an integer'}, status=400)
            if company_id not in ids:
                return Response({'error': 'Company not found'}, status=404)
        elif len(ids) == 1:
            company_id = next(iter(ids))
        elif not ids:
            return Response({'error': 'No company data available'}, status=404)
        else:
            return Response({'error': 'company_id required (key is bound to several companies)'}, status=400)
        company = Company.objects.filter(id=company_id).first()
        if not company:
            return Response({'error': 'No company data available'}, status=404)

        # Invoice metrics
        all_invoices = Invoice.objects.filter(company=company)
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
            company=company,
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
        risk_scores = RiskScore.objects.filter(company=company)
        avg_score = 0
        if risk_scores.exists():
            avg_score = round(sum(rs.total_score for rs in risk_scores) / risk_scores.count())

        # Tier distribution
        tier_dist = {}
        for rs in risk_scores:
            tier_dist[rs.tier] = tier_dist.get(rs.tier, 0) + 1

        # Outstanding = paid out and not yet settled. ACTIVE/FUNDED were never
        # AdvanceRequest statuses.
        company_advances = AdvanceRequest.objects.filter(facility__company=company)
        advances = company_advances.filter(status='DISBURSED')
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
                'vehicles': Vehicle.objects.filter(company=company).count(),
                'active_vehicles': Vehicle.objects.filter(company=company, status__in=['AVAILABLE', 'IN_USE']).count(),
                'drivers': Driver.objects.filter(company=company).count(),
                'active_drivers': Driver.objects.filter(company=company, status='ACTIVE').count(),
                'active_loads': Load.objects.filter(company=company, status='IN_TRANSIT').count(),
                'delivered_90d': recent_loads.count(),
            },
            'advance_history': [
                {
                    'id': str(a.id),
                    'amount': float(a.amount or 0),
                    'status': a.status,
                    'created': a.created_at.isoformat(),
                }
                for a in company_advances.order_by('-created_at')[:5]
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

        from core.services.capital_guard import FUNDABLE_INVOICE_STATUSES, load_has_pod_evidence
        from core.services.facility_ledger import ACTIVE_STATUSES

        # Bound transporters only; collectable, offered (early_pay_eligible),
        # backed by a load, and not already advanced. The old fallback listed
        # every SENT/DRAFT invoice when none were early_pay_eligible.
        invoices = Invoice.objects.filter(
            company_id__in=self._company_ids(request),
            status__in=FUNDABLE_INVOICE_STATUSES,
            early_pay_eligible=True,
            load__isnull=False,
        ).exclude(
            advance_requests__status__in=ACTIVE_STATUSES,
        ).select_related('customer', 'load')

        # Fast Pay (2026-10): every row comes from core.capital.engine, the
        # one decision path. The old per-tier fee map and the default score 55
        # for unscored invoices are gone.
        from core.capital import engine as fp_engine
        result = []
        for inv in invoices:
            if not load_has_pod_evidence(inv.load):
                continue
            if not self._funder_matches(request, inv):
                continue
            try:
                ev = fp_engine.evaluate(inv)
            except Exception:
                continue
            if not ev.eligible or ev.decision == 'DECLINE':
                continue
            amount = float(inv.total_amount)
            result.append({
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer': inv.customer.name,
                'customer_id': inv.customer.id,
                'debtor_registration_number': getattr(ev.debtor, 'registration_number', None),
                'amount_zar': amount,
                'subtotal_zar': float(inv.subtotal),
                'vat_zar': float(inv.vat_amount),
                'issue_date': inv.issue_date.isoformat(),
                'due_date': inv.due_date.isoformat(),
                'age_days': (date.today() - inv.issue_date).days,
                'decision': ev.decision,
                'invoice_grade': ev.invoice_grade,
                'risk_tier': ev.invoice_grade,
                'debtor_grade': getattr(ev.debtor_score, 'grade', None),
                'transporter_grade': getattr(ev.transporter_score, 'grade', None),
                'expected_loss_pct': float(ev.el_pct),
                'advance_rate_pct': float(ev.advance_rate_pct),
                'fundable_amount_zar': float(ev.fundable_amount),
                'fee_rate_pct': float(ev.fee_pct),
                'fee_amount_zar': float(ev.fee_amount),
                'fee_vat_zar': float(ev.fee_vat_amount),
                'net_payout_zar': float(ev.net_payout),
                'expected_payment_date': ev.expected_payment_date.isoformat() if ev.expected_payment_date else None,
                'verification_tier': ev.verification_tier,
                'reason_codes': [r['code'] for r in ev.reasons],
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

        from decimal import InvalidOperation
        from core.models.invoice import Invoice
        from core.models.facility import Facility
        from core.services.capital_guard import financing_block_reason
        from core.services.facility_ledger import open_advance, CapacityError

        invoice_id = request.data.get('invoice_id')
        requested_amount = request.data.get('requested_amount')
        lender_name = self._get_lender_name(request)

        if not invoice_id:
            return Response({'error': 'invoice_id required'}, status=400)

        # Only invoices of transporters this key is bound to; anything else is
        # indistinguishable from a missing invoice.
        try:
            invoice = Invoice.objects.select_related('load', 'customer').get(
                id=invoice_id, company_id__in=self._company_ids(request))
        except (Invoice.DoesNotExist, ValueError, TypeError):
            return Response({'error': f'Invoice {invoice_id} not found'}, status=404)

        block = financing_block_reason(invoice)
        if block:
            return Response({'error': block}, status=400)
        if not invoice.early_pay_eligible:
            return Response({'error': 'Invoice has not been offered for early payment'}, status=400)

        balance = Decimal(str(invoice.balance if invoice.balance is not None else invoice.total_amount))
        if requested_amount in (None, ''):
            amount = balance
        else:
            try:
                amount = Decimal(str(requested_amount)).quantize(Decimal('0.01'))
            except (InvalidOperation, ValueError, TypeError):
                return Response({'error': 'requested_amount must be a number'}, status=400)
        if amount <= 0:
            return Response({'error': 'requested_amount must be greater than zero'}, status=400)
        if amount > balance:
            return Response({'error': f'requested_amount exceeds the invoice balance (R{balance})'}, status=400)

        if not self._funder_matches(request, invoice):
            return Response({'error': f'Invoice {invoice_id} not found'}, status=404)
        facility = Facility.objects.filter(
            company_id=invoice.company_id, status='ACTIVE'
        ).first() if invoice.company_id else None
        if not facility:
            return Response({'error': 'No active facility found'}, status=400)

        # Fast Pay (2026-10): the same decision path as an in-app request:
        # evaluate under the funder lock, record the decision, open the advance.
        from core.capital import engine as fp_engine
        if not fp_engine.can_request(invoice.company):
            return Response({'code': 'not_launched', 'error': 'Fast Pay is not live yet.'}, status=403)
        try:
            advance, assessment, ev, created = fp_engine.request(
                invoice, actor=request.user, actor_label=f'funder API: {lender_name}', purpose='LENDER',
                requested_amount=amount)
        except CapacityError as exc:
            return Response({'error': f'Requested amount exceeds available capacity: {exc}'}, status=400)
        if advance is None:
            return Response({
                'error': 'Invoice is not fundable',
                'decision': ev.decision,
                'reason_codes': [r['code'] for r in ev.reasons],
                'reasons': [r['text'] for r in ev.reasons if r['direction'] in ('!', '-')],
                'assessment_id': assessment.pk,
            }, status=400)
        if not created:
            return Response({
                'error': 'Invoice already has an active advance',
                'advance_id': advance.id,
                'reference': f'ADV-{advance.id:06d}',
            }, status=409)

        return Response({
            'advance_id': advance.id,
            'reference': f'ADV-{advance.id:06d}',
            'invoice_number': invoice.invoice_number,
            'customer': invoice.customer.name,
            'requested_amount_zar': float(amount),
            'decision': ev.decision,
            'advance_amount_zar': float(advance.amount),
            'fee_rate_pct': float(ev.fee_pct),
            'fee_amount_zar': float(ev.fee_amount),
            'net_payout_zar': float(ev.net_payout),
            'queued_amount_zar': float(ev.queued_amount),
            'assessment_id': assessment.pk,
            'status': advance.status,
            'lender': lender_name,
            'submitted_at': timezone.now().isoformat(),
            'message': ('Decision recorded. The advance needs the funder\'s approval (Mode A) before it is '
                        'paid out.' if advance.status in ('REQUESTED', 'SCORING') else
                        'Decision recorded. The request is queued until capacity frees.'),
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

        from core.services.facility_ledger import ACTIVE_STATUSES

        advances = AdvanceRequest.objects.filter(
            facility__company_id__in=self._company_ids(request),
        ).select_related('invoice__customer', 'facility').order_by('-created_at')

        # ACTIVE/FUNDED/PENDING/REPAID were never AdvanceRequest statuses, so
        # the book always showed (almost) nothing outstanding.
        active = advances.filter(status__in=ACTIVE_STATUSES)
        disbursed = advances.filter(status='DISBURSED')
        completed = advances.filter(status='SETTLED')

        return Response({
            'summary': {
                'active_advances': active.count(),
                'total_outstanding_zar': round(sum(float(a.amount or 0) for a in disbursed), 2),
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
