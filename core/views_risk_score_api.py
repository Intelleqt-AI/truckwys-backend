"""Productised, metered Risk-Scoring API ("Underwriting-as-a-Service").

POST /api/v1/risk/underwrite/  — a STATELESS endpoint: a partner submits an
invoice + debtor + operator snapshot and gets back the full 7-pillar risk score,
tier, fee, advance %, eligibility and top drivers — without any of it being our
own data. Authenticated by an IntegrationAPIKey (X-API-Key) with per-key metering
and monthly quotas, or by a logged-in operator (Token) for in-app testing.

The score is produced by the SAME core.services.risk_engine used internally, so
the API and the in-app Capital product can never drift apart.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from rest_framework import status, authentication, exceptions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from core.models import Company, Customer, Invoice, Facility
from core.models.integration_api_key import IntegrationAPIKey
from core.services.risk_engine import RiskEngine


class IntegrationKeyUser:
    """Pseudo-user for IntegrationAPIKey-authenticated requests."""
    is_authenticated = True
    is_active = True
    is_staff = False
    is_superuser = False
    pk = None
    id = None

    def __init__(self, api_key: IntegrationAPIKey):
        self.api_key = api_key
        self.username = f"key:{api_key.name}"

    def __str__(self):
        return self.username


class IntegrationKeyAuthentication(authentication.BaseAuthentication):
    """Authenticate via an IntegrationAPIKey in the X-API-Key header."""

    def authenticate(self, request):
        key = request.META.get('HTTP_X_API_KEY')
        if not key:
            return None  # fall through to other auth (e.g. Token)
        try:
            api_key = IntegrationAPIKey.objects.get(key=key, active=True)
        except IntegrationAPIKey.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid API key')
        return (IntegrationKeyUser(api_key), api_key)

    def authenticate_header(self, request):
        return 'X-API-Key'


def _dec(v, default='0'):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _parse_date(v, default=None):
    if not v:
        return default
    if isinstance(v, (date, datetime)):
        return v.date() if isinstance(v, datetime) else v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except ValueError:
        return default


class RiskUnderwriteView(APIView):
    """POST /api/v1/risk/underwrite/ — stateless underwriting score for a submitted invoice."""

    authentication_classes = [IntegrationKeyAuthentication, authentication.TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        api_key = request.auth if isinstance(request.auth, IntegrationAPIKey) else None

        # Quota enforcement for metered keys
        if api_key is not None and api_key.is_over_quota():
            return Response(
                {'error': 'Monthly quota exceeded for this API key',
                 'quota': api_key.monthly_quota, 'used': api_key.quota_used},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        data = request.data or {}
        inv = data.get('invoice') or {}
        deb = data.get('debtor') or data.get('customer') or {}
        op = data.get('operator') or data.get('company') or {}
        fac = data.get('facility') or {}

        if inv.get('amount') in (None, '') and inv.get('total_amount') in (None, ''):
            return Response({'error': 'invoice.amount is required'}, status=status.HTTP_400_BAD_REQUEST)

        amount = _dec(inv.get('amount', inv.get('total_amount')))
        if amount <= 0:
            return Response({'error': 'invoice.amount must be > 0'}, status=status.HTTP_400_BAD_REQUEST)

        today = date.today()
        issue_date = _parse_date(inv.get('issue_date'), today - timedelta(days=int(inv.get('age_days', 0) or 0)))
        due_date = _parse_date(inv.get('due_date'), issue_date + timedelta(days=int(inv.get('terms_days', 30) or 30)))

        from django.utils import timezone
        now = timezone.now()

        # Build UNSAVED model instances from the caller's snapshot — nothing persisted.
        company = Company(
            company_name=op.get('name', 'External Operator'),
            cipc_age_years=int(op.get('company_age_years', op.get('cipc_age_years', 0)) or 0),
            fleet_size=int(op.get('fleet_size', 0) or 0),
            turnover_trend=op.get('turnover_trend', 'stable'),
            annual_turnover=_dec(op.get('annual_turnover', 0)),
        )
        company.created_at = now

        # relationship_months is a read-only property derived from created_at,
        # so translate the caller's value into a created_at timestamp.
        rel_months = int(deb.get('relationship_months', 12) or 12)
        customer = Customer(
            company=company,
            name=deb.get('name', 'External Debtor'),
            credit_score=(int(deb['credit_score']) if str(deb.get('credit_score', '')).strip() not in ('', 'None') else None),
            is_active=bool(deb.get('is_active', True)),
            avg_days_to_pay=int(float(deb.get('avg_days_to_pay', 30) or 30)),
        )
        customer.created_at = now - timedelta(days=rel_months * 30)

        invoice = Invoice(
            company=company,
            customer=customer,
            total_amount=amount,
            subtotal=_dec(inv.get('subtotal', amount)),
            vat_amount=_dec(inv.get('vat_amount', 0)),
            issue_date=issue_date,
            due_date=due_date,
            status=inv.get('status', 'SENT'),
        )
        invoice.created_at = now

        # Facility: caller may pass limits; default to comfortably cover the invoice
        # so an external score isn't gated by a facility the caller doesn't have.
        limit = _dec(fac.get('limit', amount * 10))
        available = _dec(fac.get('available', limit))
        facility = Facility(company=company, limit=limit, outstanding=limit - available, status='ACTIVE')

        try:
            result = RiskEngine(invoice=invoice, facility=facility).calculate_risk_score()
        except Exception as exc:
            return Response({'error': f'Scoring failed: {exc}'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Meter the call (only for API-key callers)
        if api_key is not None:
            try:
                api_key.record_call()
            except Exception:
                pass

        payload = {
            'eligible': result.is_eligible,
            'risk_tier': result.risk_tier,
            'score': result.final_score,
            'fee_percent': float(result.final_fee_percent),
            'max_advance_percent': result.max_advance_percent,
            'estimated_turnaround': result.estimated_turnaround,
            'invoice_amount': float(result.invoice_amount),
            'fee_amount': float(result.fee_amount),
            'net_advance': float(result.net_advance),
            'confidence': result.confidence_level,
            'top_risk_drivers': result.top_risk_drivers,
            'top_strengths': result.top_strengths,
            'ineligibility_reasons': [r.description for r in result.ineligibility_reasons],
            'pillars': [
                {'pillar': p.pillar, 'weight': p.weight, 'raw_score': p.raw_score,
                 'weighted_score': p.weighted_score}
                for p in result.pillar_breakdown
            ],
            'scored_at': result.calculated_at,
            'valid_until': result.valid_until,
        }
        if api_key is not None:
            payload['_meta'] = {
                'key': api_key.name,
                'usage_count': api_key.usage_count,
                'monthly_quota': api_key.monthly_quota,
                'quota_used': api_key.quota_used,
            }
        return Response(payload, status=status.HTTP_200_OK)
