# TENANCY (2026-09 tenant-isolation fix, docs/backend-changes/2026-09-tenant-isolation.md):
# the 2026-03-15 "all querysets properly filter by company ✓" note that used to
# sit here was wrong — company-less users saw every tenant, and risk-score
# calculate / advance create looked invoices up across tenants. Rules now:
# - non-staff users are scoped to request.user.company; no company => nothing
# - is_staff users (TruckWys capital desk) keep deliberate cross-tenant access
#   by id; their LIST views are scoped to their own company when they have one
#   (see _capital_scope)

"""Capital module views for facilities, risk scoring, and advance requests."""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView
from django.db.models import Q, Count, Sum
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from typing import Dict, Any
from datetime import date

from core.models import (
    Facility,
    RiskScore,
    AdvanceRequest,
    Invoice,
    Company,
)
from core.serializers_capital import (
    FacilitySerializer,
    RiskScoreSerializer,
    AdvanceRequestSerializer,
    RiskScoreRequestSerializer,
    AdvanceRequestCreateSerializer,
    ApproveAdvanceSerializer,
    RejectAdvanceSerializer,
    DisburseAdvanceSerializer,
    SettleAdvanceSerializer,
)
from core.services.risk_engine import RiskEngine
from core.formatting import format_zar


def _capital_scope(view, qs, company_lookup):
    """Tenant scoping for the capital viewsets (2026-09).

    * unauthenticated / company-less non-staff -> nothing (was .all(): fail open)
    * non-staff with a company                 -> their company only
    * staff with a company, LIST               -> their company only — the app
      pages (Capital renders facilities[0], Overview, RiskScoreView) used to mix
      other tenants' rows in for a staff member of a tenant
    * staff, detail/actions by id, or staff with no company -> cross-tenant
      (deliberate: the capital desk approves/disburses any advance by id)
    """
    user = view.request.user
    if not getattr(user, 'is_authenticated', False):
        return qs.none()
    company = getattr(user, 'company', None)
    if getattr(user, 'is_staff', False):
        if company is not None and getattr(view, 'action', None) == 'list':
            return qs.filter(**{company_lookup: company})
        return qs
    return qs.filter(**{company_lookup: company}) if company else qs.none()


def _invoice_scope(user):
    """Invoices this user may act on for capital purposes.

    Staff (TruckWys capital desk) keep deliberate cross-tenant access; everyone
    else is limited to their own company's invoices, and a company-less account
    gets none (fail closed).
    """
    if getattr(user, 'is_staff', False):
        return Invoice.objects.all()
    company = getattr(user, 'company', None)
    return Invoice.objects.filter(company=company) if company else Invoice.objects.none()


def _facility_for_invoice(invoice):
    """The active facility of the transporter that issued the invoice.

    Staff used to get the first ACTIVE facility of any tenant: an arbitrary
    tenant's facility, so the capital desk scored and funded one
    transporter's invoice against another's limit (audit §6 #7). The
    facility always follows invoice.company now, for staff and tenants alike.
    """
    if invoice is None or not invoice.company_id:
        return None
    return Facility.objects.filter(company_id=invoice.company_id, status='ACTIVE').first()


def _inv_no(advance):
    """Invoice number for an advance, defensively."""
    inv = getattr(advance, 'invoice', None)
    return getattr(inv, 'invoice_number', None) or f'Advance #{advance.id}'


def _notify_advance(advance, ntype, title, message, exclude_user_id=None):
    """Persist + live-push a notification for an advance lifecycle change."""
    try:
        from core.services.notify import notify_company
        company_id = getattr(getattr(advance, 'facility', None), 'company_id', None)
        notify_company(company_id, ntype, title, message,
                       link=f'/capital/advances/{advance.id}', event='advance.status',
                       exclude_user_id=exclude_user_id)
    except Exception:
        pass


class FacilityViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Facility management.

    list: Get all facilities for the user's company
    retrieve: Get facility detail with utilization
    create: Create new facility (admin only)
    update/partial_update: Update facility limit
    """

    serializer_class = FacilitySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter facilities by user's company."""
        return _capital_scope(self, Facility.objects.all(), 'company')

    def check_permissions(self, request):
        # A facility limit is credit the funder extends; a tenant could PATCH
        # its own limit up (only create was guarded, and with a PermissionError
        # that surfaced as a 500). Writes are staff-only; reads stay scoped.
        super().check_permissions(request)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.user.is_staff:
            raise PermissionDenied('Only the capital desk can change facilities')


class RiskScoreViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for RiskScore viewing and calculation.

    list: Get all risk scores for user's company
    retrieve: Get risk score detail
    calculate: POST /api/v1/risk/score/ - Calculate new risk score for an invoice
    breakdown: GET /api/v1/risk/score/{id}/breakdown/ - Get detailed factor breakdown
    """

    serializer_class = RiskScoreSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter risk scores by user's company."""
        return _capital_scope(self, RiskScore.objects.all(), 'company')

    @action(detail=False, methods=['post'], url_path='calculate')
    def calculate(self, request):
        """
        Calculate risk score for an invoice.

        Body: { "invoice_id": 1 }
        Returns: full score breakdown + fee calculation
        """
        serializer = RiskScoreRequestSerializer(
            data=request.data, context={'invoices': _invoice_scope(request.user)})
        serializer.is_valid(raise_exception=True)

        invoice_id = serializer.validated_data['invoice_id']

        try:
            # Tenant isolation: non-staff can only score their own invoices.
            invoice = _invoice_scope(request.user).get(id=invoice_id)
        except Invoice.DoesNotExist:
            return Response(
                {'error': f'Invoice with ID {invoice_id} not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Score against the invoice's own transporter facility (non-staff are
        # already limited to their own invoices by _invoice_scope).
        facility = _facility_for_invoice(invoice)

        if not facility:
            return Response(
                {'error': 'No active facility found for this company'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Calculate risk score
        engine = RiskEngine(invoice=invoice, facility=facility)
        result = engine.calculate_risk_score()

        # Create risk score record
        risk_score = engine.create_risk_score_record(result)

        # Serialize and return
        response_serializer = RiskScoreSerializer(risk_score)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='breakdown')
    def breakdown(self, request, pk=None):
        """
        Get detailed factor breakdown for a risk score.

        Returns the full factors_breakdown JSON with explanations.
        """
        risk_score = self.get_object()
        return Response({
            'id': risk_score.id,
            'invoice_number': risk_score.invoice.invoice_number,
            'total_score': risk_score.total_score,
            'tier': risk_score.tier,
            'is_eligible': risk_score.is_eligible,
            'ineligibility_reason': risk_score.ineligibility_reason,
            'fee_percent': risk_score.fee_percent,
            'fee_amount': risk_score.fee_amount,
            'factors_breakdown': risk_score.factors_breakdown,
        })


class AdvanceRequestViewSet(viewsets.ModelViewSet):
    """
    ViewSet for AdvanceRequest management.

    list: Get all advance requests
    retrieve: Get advance detail
    create: Create new advance request
    approve: POST /api/v1/advances/{id}/approve/
    reject: POST /api/v1/advances/{id}/reject/
    disburse: POST /api/v1/advances/{id}/disburse/
    settle: POST /api/v1/advances/{id}/settle/
    """

    serializer_class = AdvanceRequestSerializer
    permission_classes = [IsAuthenticated]
    # No PUT/PATCH/DELETE: status, amount and facility used to be writable by
    # a plain PATCH (e.g. status=SETTLED), bypassing every lifecycle check and
    # the facility ledger. State changes go through the actions below only.
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        """Filter advance requests by user's company."""
        return _capital_scope(self, AdvanceRequest.objects.all(), 'facility__company')

    def create(self, request, *args, **kwargs):
        """
        Create advance request.

        Body: { "invoice_id": 1 }
        Process: check eligibility → calculate risk score → create advance request
        """
        # IDEMPOTENCY (pre-validation): a retry/double-click for an invoice that
        # already has an active advance returns that advance (200) instead of a
        # 400 — so callers can safely retry. Runs before the serializer, which
        # would otherwise reject the duplicate outright.
        # Tenant isolation: both the pre-check and the invoice lookup only see
        # the caller's own invoices (staff: all) — the pre-check used to hand
        # back ANY tenant's advance for a guessed invoice_id.
        invoices = _invoice_scope(request.user)
        raw_invoice_id = request.data.get('invoice_id')
        if raw_invoice_id:
            try:
                existing = AdvanceRequest.objects.filter(
                    invoice__in=invoices,
                    invoice_id=raw_invoice_id,
                    status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED'],
                ).order_by('-requested_at').first()
            except (ValueError, TypeError):
                existing = None
            if existing:
                return Response(AdvanceRequestSerializer(existing).data, status=status.HTTP_200_OK)

        serializer = AdvanceRequestCreateSerializer(data=request.data, context={'invoices': invoices})
        serializer.is_valid(raise_exception=True)

        invoice_id = serializer.validated_data['invoice_id']
        invoice = invoices.filter(id=invoice_id).first()
        if not invoice:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)

        # Fast Pay (2026-10): one decision path. The old flow (customer-risk
        # gate, 7-pillar RiskEngine, proportional deduction, own fee) is gone;
        # core.capital.engine evaluates, records the decision and opens the
        # advance under the funder lock.
        from core.capital import engine as fp_engine
        from core.capital import present as fp_present
        if not request.user.is_staff:
            if not fp_engine.can_request(invoice.company):
                return Response({'code': 'not_launched', 'error': 'Fast Pay is not live yet.'},
                                status=status.HTTP_403_FORBIDDEN)
            if getattr(invoice.company, 'is_demo', False):
                return Response({'code': 'demo', 'error': 'This is a demo account, so no money is advanced.'},
                                status=status.HTTP_403_FORBIDDEN)
        if _facility_for_invoice(invoice) is None:
            return Response(
                {'error': 'No active facility found for this company'},
                status=status.HTTP_400_BAD_REQUEST
            )
        from core.services.facility_ledger import CapacityError
        try:
            advance_request, assessment, ev, created = fp_engine.request(
                invoice, actor=request.user, actor_label=request.user.username)
        except CapacityError:
            return Response({'code': 'capacity', 'error': 'Fast Pay capacity changed while we were checking. '
                                                          'Please try again.'}, status=status.HTTP_409_CONFLICT)
        if advance_request is None:
            # Transporter wording only: desk text can name other tenants'
            # invoices or loads (duplicates) and the desk's hold notes.
            from core.capital.reasons import for_transporter
            safe = for_transporter(ev.reasons)
            hard = [r['text'] for r in safe if r['direction'] == '!'] or \
                   [r['text'] for r in safe if r['direction'] == '-'] or ['Not fundable']
            return Response(
                {
                    'error': 'Invoice is not eligible for advance',
                    'reason': hard[0],
                    'reasons': hard,
                    'decision': ev.decision,
                    'offer': fp_present.offer(ev, persisted=assessment),
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        if not created:
            return Response(AdvanceRequestSerializer(advance_request).data, status=status.HTTP_200_OK)

        try:
            from core.services.notify import notify_company
            notify_company(
                getattr(advance_request.facility, 'company_id', None),
                'SUCCESS',
                'Fast Pay requested',
                f'{invoice.invoice_number}: {fp_present.STATUS_LABELS.get(advance_request.status)}',
                link=f'/capital/advances/{advance_request.id}',
                event='advance.created',
                exclude_user_id=request.user.id,
            )
        except Exception:
            pass

        response_serializer = AdvanceRequestSerializer(advance_request)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='approve')
    def approve(self, request, pk=None):
        """Approve advance request (for partner/admin)."""
        advance = self.get_object()

        # Check permissions (staff or partner - will implement partner auth later)
        if not request.user.is_staff:
            return Response(
                {'error': 'Only administrators can approve advances'},
                status=status.HTTP_403_FORBIDDEN
            )
        # Mode A: the funder approves every advance; the capital desk only with
        # a written delegation (Funder.staff_may_approve; the sandbox has it).
        from core.capital.access import check_advance_action
        check_advance_action(request.user, advance, 'approve')

        serializer = ApproveAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            # Read by the AdvanceRequest post_save signal: this view sends its
            # own "Advance approved" notification below (excluding the actor),
            # so tell the signal not to send its own copy too — and exclude the
            # approving user from that copy in case _notify_handled ever isn't
            # set (defence in depth, matches the same pattern used for quotes).
            advance._notify_handled = True
            advance._notify_actor_id = request.user.id
            from core.services.facility_ledger import approve_advance
            approve_advance(advance, actor=request.user, actor_label=request.user.username)
            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            _notify_advance(advance, 'SUCCESS', 'Advance approved',
                            f'{_inv_no(advance)} approved — {format_zar(advance.net_amount, 0)} to be disbursed',
                            exclude_user_id=request.user.id)
            response_serializer = AdvanceRequestSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, pk=None):
        """Reject advance request with reason."""
        advance = self.get_object()

        # Check permissions
        if not request.user.is_staff:
            return Response(
                {'error': 'Only administrators can reject advances'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = RejectAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            reason = serializer.validated_data['reason']
            from core.capital.access import check_advance_action
            from core.capital.queue import capacity_freed
            from core.services.facility_ledger import deny_advance
            check_advance_action(request.user, advance, 'decline')
            deny_advance(advance, reason, actor=request.user)
            capacity_freed(advance.funder or advance.facility.funder)

            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            _notify_advance(advance, 'WARNING', 'Advance declined',
                            f'{_inv_no(advance)} declined: {reason}',
                            exclude_user_id=request.user.id)
            response_serializer = AdvanceRequestSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'], url_path='disburse')
    def disburse(self, request, pk=None):
        """Mark advance as disbursed."""
        advance = self.get_object()

        # Check permissions
        if not request.user.is_staff:
            return Response(
                {'error': 'Only administrators can disburse advances'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = DisburseAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            # See the same guard in approve() above — the signal would
            # otherwise also send "Funds disbursed" for this transition.
            advance._notify_handled = True
            advance._notify_actor_id = request.user.id
            from core.capital.access import check_advance_action
            check_advance_action(request.user, advance, 'disburse')
            from core.services.facility_ledger import disburse_advance
            disburse_advance(advance, actor=request.user,
                             reference=serializer.validated_data.get('notes', '') or '')

            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            _notify_advance(advance, 'SUCCESS', 'Advance disbursed',
                            f'{_inv_no(advance)} — {format_zar(advance.net_amount, 0)} paid out',
                            exclude_user_id=request.user.id)
            response_serializer = AdvanceRequestSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'], url_path='settle')
    def settle(self, request, pk=None):
        """Settle a disbursed advance against debtor-payment evidence.

        Staff (capital desk) only. The transporter used to be able to settle
        its own advance with one click, releasing facility capacity while the
        debt was still unpaid, and then draw again (audit §6 #1).
        """
        user = request.user
        if not user.is_staff:
            return Response(
                {'error': 'Only the capital desk can settle advances. Settlement is '
                          'recorded when the debtor payment is received.'},
                status=status.HTTP_403_FORBIDDEN
            )
        advance = self.get_object()

        serializer = SettleAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payment = None
        payment_id = serializer.validated_data.get('payment_id')
        if payment_id is not None:
            from core.models import Payment
            payment = Payment.objects.filter(pk=payment_id, invoice_id=advance.invoice_id).first()
            if payment is None:
                return Response(
                    {'error': 'payment_id must be a payment recorded on the advanced invoice'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        try:
            advance.settle(
                payment_reference=serializer.validated_data['payment_reference'],
                settled_by=user,
                payment=payment,
            )

            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            # Flywheel: record the realised outcome for ML retraining.
            try:
                from core.services.outcome_capture import capture_settlement_outcome
                capture_settlement_outcome(advance)
            except Exception:
                pass
            _notify_advance(advance, 'SUCCESS', 'Advance settled',
                            f'{_inv_no(advance)} settled',
                            exclude_user_id=user.id)

            response_serializer = AdvanceRequestSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class CapitalDashboardViewSet(viewsets.ViewSet):
    """
    ViewSet for capital dashboard data.

    dashboard: GET /api/v1/dashboard/capital/
    """

    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'], url_path='capital')
    def capital(self, request):
        """
        Get capital dashboard data for new Capital Pay page.

        Returns:
        - facility: {id, limit, outstanding, available, utilization_percent}
        - eligible_invoices: [invoice objects with customer data]
        - advances: [recent advances with full details]
        - stats: {total_advances_this_month, total_fees_this_month, average_settlement_days}
        """
        from datetime import timedelta
        from django.utils.timezone import now

        user = request.user

        # Get company. Staff/admin may target any company via ?company_id=,
        # otherwise fall back to their own company association.
        if user.is_staff:
            company_id = request.GET.get('company_id')
            if company_id:
                company = Company.objects.filter(id=company_id).first()
                if not company:
                    return Response(
                        {'error': f'Company {company_id} not found'},
                        status=status.HTTP_404_NOT_FOUND
                    )
            else:
                company = getattr(user, 'company', None)
                if not company:
                    company = Company.objects.order_by('id').first()
                if not company:
                    return Response(
                        {'error': 'No company found; specify a company_id parameter'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
        else:
            company = user.company

        # Get facility data
        facility = Facility.objects.filter(company=company, status='ACTIVE').first()

        if not facility:
            # Return empty state
            return Response(
                {
                    'facility': {
                        'id': None,
                        'limit': 0,
                        'outstanding': 0,
                        'available': 0,
                        'utilization_percent': 0,
                    },
                    'eligible_invoices': [],
                    'advances': [],
                    'stats': {
                        'total_advances_this_month': 0,
                        'total_fees_this_month': 0,
                        'average_settlement_days': 0,
                    }
                },
                status=status.HTTP_200_OK
            )

        # Facility object
        facility_data = {
            'id': facility.id,
            'limit': float(facility.limit),
            'outstanding': float(facility.outstanding),
            'reserved': float(facility.reserved),
            'available': float(facility.available),
            'utilization_percent': float(facility.utilization_percent),
        }

        # Eligible invoices (SENT status, with POD, without active advances)
        from core.serializers import InvoiceSerializer

        eligible_invoices_qs = Invoice.objects.filter(
            company=company,
            status='SENT'
        ).exclude(
            advance_requests__status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        ).select_related('customer', 'trip').order_by('-created_at')[:50]

        eligible_invoices = []
        for invoice in eligible_invoices_qs:
            eligible_invoices.append({
                'id': invoice.id,
                'invoice_number': invoice.invoice_number,
                'customer': {
                    'id': invoice.customer.id,
                    'name': invoice.customer.name,
                },
                'total_amount': float(invoice.total_amount),
                'status': invoice.status,
                'created_at': invoice.created_at.isoformat(),
                'due_date': invoice.due_date.isoformat() if invoice.due_date else None,
                'trip': {
                    'id': invoice.trip.id,
                    'pod_status': invoice.trip.pod_status,
                } if invoice.trip else None,
            })

        # Advances (all advances for this facility, ordered by recency)
        advances_qs = AdvanceRequest.objects.filter(
            facility=facility
        ).select_related('invoice', 'invoice__customer').order_by('-created_at')[:30]

        advances = []
        for adv in advances_qs:
            advances.append({
                'id': adv.id,
                'invoice_number': adv.invoice.invoice_number if adv.invoice else 'N/A',
                'customer_name': adv.invoice.customer.name if (adv.invoice and adv.invoice.customer) else 'Unknown',
                'gross_amount': float(adv.amount),
                'fee_percent': float(adv.fee_percent),
                'fee_amount': float(adv.fee_amount),
                'net_amount': float(adv.net_amount),
                'status': adv.status,
                'created_at': adv.created_at.isoformat(),
                'disbursed_at': adv.disbursed_at.isoformat() if adv.disbursed_at else None,
                'settled_at': adv.settled_at.isoformat() if adv.settled_at else None,
            })

        # Stats for last 30 days
        thirty_days_ago = now() - timedelta(days=30)

        month_advances = AdvanceRequest.objects.filter(
            facility=facility,
            created_at__gte=thirty_days_ago
        )

        total_advances_this_month = month_advances.count()
        total_fees_this_month = month_advances.aggregate(
            total=Sum('fee_amount')
        )['total'] or Decimal('0.00')

        # Average settlement time (settled advances only)
        settled_advances = AdvanceRequest.objects.filter(
            facility=facility,
            status='SETTLED',
            settled_at__isnull=False,
            created_at__isnull=False
        )

        if settled_advances.exists():
            settlement_days = []
            for adv in settled_advances:
                if adv.settled_at and adv.created_at:
                    days = (adv.settled_at - adv.created_at).days
                    settlement_days.append(days)
            average_settlement_days = sum(settlement_days) / len(settlement_days) if settlement_days else 0
        else:
            average_settlement_days = 0

        stats_data = {
            'total_advances_this_month': total_advances_this_month,
            'total_fees_this_month': float(total_fees_this_month),
            'average_settlement_days': round(average_settlement_days, 1),
        }

        return Response({
            'facility': facility_data,
            'eligible_invoices': eligible_invoices,
            'advances': advances,
            'stats': stats_data,
        })


class CapitalEligibleInvoicesView(APIView):
    """
    GET /api/v1/capital/eligible/

    Returns eligible invoices for fast-pay/advance for the authenticated operator.
    Similar logic to lender eligible-invoices but with Token authentication.
    """
    permission_classes = [IsAuthenticated]  # Using Token auth in practice

    def get(self, request):
        user = request.user

        # Each invoice is scored against its own transporter's facility (never
        # an arbitrary "first active" one). Without a facility, no invoice is
        # advanceable — return an empty, honest list.
        company = getattr(user, 'company', None)
        if company is None and not user.is_staff:
            # Company-less non-staff used to fall through to every tenant's
            # invoices here; fail closed like the rest of the capital views.
            return Response({
                'eligible_count': 0, 'total_face_value_zar': 0.0, 'total_net_payout_zar': 0.0,
                'invoices': [], 'ineligible_count': 0, 'ineligible_invoices': [],
            })

        # Fast Pay (2026-10): rows come from core.capital.engine, the same
        # decision path as a request, so this list shows exactly what a request
        # would get. Field names kept for the existing app pages.
        from core.capital import engine as fp_engine
        from core.capital import book as fp_book
        from core.capital.reasons import for_transporter
        candidates = Invoice.objects.filter(
            status__in=list(fp_engine.FUNDABLE_INVOICE_STATUSES),
        ).select_related('customer', 'customer__debtor_identity', 'load', 'company').exclude(
            advance_requests__status__in=list(fp_engine.LIVE_STATUSES)
        )
        if company is not None:
            candidates = candidates.filter(company=company)
        candidates = candidates.order_by('-issue_date')[:50]

        # The customer-risk badge stays informational on the page; it no longer
        # gates or sizes an advance (the debtor score does).
        from core.services.customer_risk import compute_customer_risk_bulk
        customer_risk = compute_customer_risk_bulk(
            company, [inv.customer_id for inv in candidates if inv.customer_id]
        ) if company is not None else {}

        states = {}
        result, ineligible_result = [], []
        total_face_value = Decimal('0.00')
        total_net_payout = Decimal('0.00')
        for inv in candidates:
            line = fp_engine.line_for(inv.company)
            if line is not None and line.funder_id and line.funder_id not in states:
                states[line.funder_id] = fp_book.load_state(line.funder)
            try:
                ev = fp_engine.evaluate(inv, state=states.get(getattr(line, 'funder_id', None)))
            except Exception:
                continue
            visible = for_transporter(ev.reasons)
            if not ev.eligible or ev.decision == 'DECLINE':
                blockers = [r['text'] for r in visible if r['direction'] == '!'] or ['Not eligible']
                ineligible_result.append({
                    'id': inv.id,
                    'invoice_number': inv.invoice_number,
                    'customer': inv.customer.name,
                    'amount': float(inv.total_amount),
                    'reason': blockers[0],
                    'rule': next((r['code'] for r in visible if r['direction'] == '!'), 'NOT_ELIGIBLE'),
                    'all_reasons': blockers,
                })
                continue
            amount = Decimal(str(inv.total_amount))
            total_face_value += amount
            total_net_payout += ev.net_payout
            crisk = customer_risk.get(inv.customer_id, {'risk_pct': None, 'band': None, 'blocked': False})
            result.append({
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer': inv.customer.name,
                'customer_id': inv.customer.id,
                'customer_risk_pct': crisk['risk_pct'],
                'customer_risk_band': crisk['band'],
                'risk_blocked': False,
                'decision': ev.decision,
                'fundable_amount_zar': float(ev.fundable_amount),
                'queued_amount_zar': float(ev.queued_amount),
                'amount_zar': float(amount),
                'amount': float(amount),  # Frontend compatibility
                'total_amount': float(amount),  # Frontend compatibility
                'subtotal_zar': float(inv.subtotal),
                'vat_zar': float(inv.vat_amount),
                'issue_date': inv.issue_date.isoformat() if inv.issue_date else None,
                'due_date': inv.due_date.isoformat() if inv.due_date else None,
                'age_days': (date.today() - inv.issue_date).days if inv.issue_date else 0,
                'risk_tier': ev.invoice_grade,
                'tier': ev.invoice_grade.lower(),  # Frontend compatibility
                'fee_rate_pct': float(ev.fee_pct),
                'fee_amount_zar': float(ev.fee_amount),
                'fee_vat_zar': float(ev.fee_vat_amount),
                'net_payout_zar': float(ev.net_payout),
                'holdback_zar': float(ev.holdback_amount),
                'max_advance_percent': float(ev.advance_rate_pct),
                'expected_payment_date': ev.expected_payment_date.isoformat() if ev.expected_payment_date else None,
                'reasons': visible,
                'load_reference': inv.load.load_number if inv.load else None,
                'route': f'{inv.load.pickup_city} → {inv.load.delivery_city}' if inv.load else None,
            })

        return Response({
            'eligible_count': len(result),
            'total_face_value_zar': float(total_face_value),
            'total_net_payout_zar': float(total_net_payout),
            'invoices': result,
            'ineligible_count': len(ineligible_result),
            'ineligible_invoices': ineligible_result,
        })


class CustomerRiskProfileView(APIView):
    """
    GET /api/v1/customers/<id>/risk-profile/

    The AI customer risk profile behind the Capital page's risk badge:
    overdue-behavior score + components, payment-behavior rows for the table
    and charts, and a live LLM-written summary (deterministic fallback).
    Company-scoped: another company's customer id is a 404.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from core.models import Customer
        from core.services.customer_risk import compute_customer_risk, ai_risk_summary

        company = getattr(request.user, 'company', None)
        qs = Customer.objects.all()
        if company is not None and not request.user.is_superuser:
            qs = qs.filter(company=company)
        customer = qs.filter(pk=pk).first()
        if customer is None:
            return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)

        profile = compute_customer_risk(customer, company or customer.company)
        profile['ai_summary'] = ai_risk_summary(profile)
        return Response(profile)
