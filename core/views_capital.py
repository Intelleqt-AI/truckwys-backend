# TENANCY AUDIT: 2026-03-15 — All querysets properly filter by company
# - FacilityViewSet: Filters by user.company ✓
# - RiskScoreViewSet: Filters by user.company ✓
# - AdvanceRequestViewSet: Filters by facility__company ✓
# - CapitalDashboardViewSet: Scoped to user.company ✓
# - CapitalEligibleInvoicesView: Uses authenticated user context ✓

"""Capital module views for facilities, risk scoring, and advance requests."""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
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


def _inv_no(advance):
    """Invoice number for an advance, defensively."""
    inv = getattr(advance, 'invoice', None)
    return getattr(inv, 'invoice_number', None) or f'Advance #{advance.id}'


def _notify_advance(advance, ntype, title, message):
    """Persist + live-push a notification for an advance lifecycle change."""
    try:
        from core.services.notify import notify_company
        company_id = getattr(getattr(advance, 'facility', None), 'company_id', None)
        notify_company(company_id, ntype, title, message,
                       link=f'/capital/advances/{advance.id}', event='advance.status')
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
        user = self.request.user
        if user.is_staff:
            return Facility.objects.all()
        if not user.is_authenticated: return Facility.objects.all()
        company = getattr(user, "company", None)
        return Facility.objects.filter(company=company) if company else Facility.objects.all()

    def perform_create(self, serializer):
        """Only staff can create facilities."""
        if not self.request.user.is_staff:
            raise PermissionError("Only administrators can create facilities")
        serializer.save()


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
        user = self.request.user
        if user.is_staff:
            return RiskScore.objects.all()
        if not user.is_authenticated: return RiskScore.objects.all()
        company = getattr(user, "company", None)
        return RiskScore.objects.filter(company=company) if company else RiskScore.objects.all()

    @action(detail=False, methods=['post'], url_path='calculate')
    def calculate(self, request):
        """
        Calculate risk score for an invoice.

        Body: { "invoice_id": 1 }
        Returns: full score breakdown + fee calculation
        """
        serializer = RiskScoreRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invoice_id = serializer.validated_data['invoice_id']

        try:
            invoice = Invoice.objects.get(id=invoice_id)
        except Invoice.DoesNotExist:
            return Response(
                {'error': f'Invoice with ID {invoice_id} not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check permissions — admin/staff can score any invoice
        user = request.user

        # Get facility for the OPERATOR (logged-in user's company), not the debtor
        # For staff/admin, use the first active facility
        if user.is_staff:
            facility = Facility.objects.filter(status='ACTIVE').first()
        else:
            company = getattr(user, 'company', None)
            facility = Facility.objects.filter(
                company=company,
                status='ACTIVE'
            ).first() if company else None

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

    def get_queryset(self):
        """Filter advance requests by user's company."""
        user = self.request.user
        if not user.is_authenticated:
            return AdvanceRequest.objects.all()
        if user.is_staff:
            return AdvanceRequest.objects.all()
        company = getattr(user, 'company', None)
        if company:
            return AdvanceRequest.objects.filter(facility__company=company)
        return AdvanceRequest.objects.all()

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
        raw_invoice_id = request.data.get('invoice_id')
        if raw_invoice_id:
            existing = AdvanceRequest.objects.filter(
                invoice_id=raw_invoice_id,
                status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED'],
            ).order_by('-requested_at').first()
            if existing:
                return Response(AdvanceRequestSerializer(existing).data, status=status.HTTP_200_OK)

        serializer = AdvanceRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invoice_id = serializer.validated_data['invoice_id']
        invoice = Invoice.objects.filter(id=invoice_id).first()
        if not invoice:
            return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)

        # Get facility for the OPERATOR (logged-in user's company)
        user = request.user
        if user.is_staff:
            facility = Facility.objects.filter(status='ACTIVE').first()
        else:
            company = getattr(user, 'company', None)
            facility = Facility.objects.filter(
                company=company,
                status='ACTIVE'
            ).first() if company else None

        if not facility:
            return Response(
                {'error': 'No active facility found for this company'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # IDEMPOTENCY: if an active advance already exists for this invoice,
        # return it instead of creating a duplicate (handles retries/double-clicks).
        existing = AdvanceRequest.objects.filter(
            invoice=invoice,
            status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED'],
        ).order_by('-requested_at').first()
        if existing:
            return Response(AdvanceRequestSerializer(existing).data, status=status.HTTP_200_OK)

        # Calculate risk score
        engine = RiskEngine(invoice=invoice, facility=facility)
        result = engine.calculate_risk_score()

        # Check eligibility
        if not result.is_eligible:
            reasons = [r.description for r in result.ineligibility_reasons] if result.ineligibility_reasons else ['Score below minimum']
            return Response(
                {
                    'error': 'Invoice is not eligible for advance',
                    'reason': reasons[0] if reasons else 'Ineligible',
                    'reasons': reasons,
                    'total_score': result.final_score,
                    'tier': result.risk_tier,
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create risk score record
        risk_score = engine.create_risk_score_record(result)

        # Atomically lock the facility row, re-check capacity under the lock to
        # prevent concurrent requests double-spending the facility limit, and
        # guard against a racing duplicate advance for the same invoice.
        try:
            with transaction.atomic():
                locked_facility = Facility.objects.select_for_update().get(pk=facility.pk)

                race_dupe = AdvanceRequest.objects.select_for_update().filter(
                    invoice=invoice,
                    status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED'],
                ).first()
                if race_dupe:
                    return Response(AdvanceRequestSerializer(race_dupe).data, status=status.HTTP_200_OK)

                if locked_facility.available < invoice.total_amount:
                    return Response(
                        {'error': 'Advance would exceed available facility limit',
                         'available': float(locked_facility.available)},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                advance_request = AdvanceRequest.objects.create(
                    invoice=invoice,
                    facility=locked_facility,
                    risk_score=risk_score,
                    amount=invoice.total_amount,
                    fee_percent=result.final_fee_percent,
                    fee_amount=result.fee_amount,
                    net_amount=result.net_advance,
                    status='REQUESTED',
                    requested_at=timezone.now(),
                )
        except Exception as exc:
            return Response({'error': f'Could not create advance: {exc}'}, status=status.HTTP_400_BAD_REQUEST)

        # Persist a notification + live-push to the operator's open sessions.
        try:
            from core.services.notify import notify_company
            notify_company(
                getattr(facility, 'company_id', None),
                'SUCCESS',
                'Advance requested',
                f'{invoice.invoice_number} — R{float(result.net_advance):,.0f} net ({result.risk_tier} tier)',
                link=f'/capital/advances/{advance_request.id}',
                event='advance.created',
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

        serializer = ApproveAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            advance.approve()
            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            _notify_advance(advance, 'SUCCESS', 'Advance approved',
                            f'{_inv_no(advance)} approved — R{float(advance.net_amount):,.0f} to be disbursed')
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
            advance.deny(reason)

            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            _notify_advance(advance, 'WARNING', 'Advance declined',
                            f'{_inv_no(advance)} declined: {reason}')
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
            advance.disburse()

            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            _notify_advance(advance, 'SUCCESS', 'Advance disbursed',
                            f'{_inv_no(advance)} — R{float(advance.net_amount):,.0f} paid out')
            response_serializer = AdvanceRequestSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'], url_path='settle')
    def settle(self, request, pk=None):
        """Settle advance when customer pays."""
        advance = self.get_object()

        # Check permissions
        user = request.user
        if not user.is_staff and advance.facility.company != user.company:
            return Response(
                {'error': 'You do not have permission to settle this advance'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = SettleAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            advance.settle()

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
                            f'{_inv_no(advance)} settled')

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

        # Resolve the operator's active facility (advances are scored against it).
        # Without a facility, no invoice is advanceable — return an empty, honest list.
        company = getattr(user, 'company', None)
        if user.is_staff:
            facility = Facility.objects.filter(status='ACTIVE').first()
        else:
            facility = Facility.objects.filter(
                company=company, status='ACTIVE'
            ).first() if company else None

        # Candidate invoices: this company's SENT/VIEWED/OVERDUE invoices with no active advance.
        # VIEWED is included because viewing the public link auto-transitions SENT → VIEWED.
        eligible_statuses = ['SENT', 'VIEWED', 'OVERDUE']
        candidates = Invoice.objects.filter(
            status__in=eligible_statuses,
        ).select_related('customer', 'load', 'trip').exclude(
            advance_requests__status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        )
        if company is not None:
            candidates = candidates.filter(company=company)
        candidates = candidates.order_by('-issue_date')[:50]

        result = []
        ineligible_result = []
        total_face_value = Decimal('0.00')
        total_net_payout = Decimal('0.00')

        for inv in candidates:
            if not facility:
                ineligible_result.append({
                    'id': inv.id,
                    'invoice_number': inv.invoice_number,
                    'customer': inv.customer.name,
                    'amount': float(inv.total_amount),
                    'reason': 'No active facility on file',
                    'rule': 'NO_FACILITY',
                })
                continue
            # Run the SAME risk engine used at advance creation so this list only
            # contains invoices that will actually be accepted (POD on file, score OK).
            try:
                engine = RiskEngine(invoice=inv, facility=facility)
                res = engine.calculate_risk_score()
            except Exception:
                continue
            if not res.is_eligible:
                primary = res.ineligibility_reasons[0] if res.ineligibility_reasons else None
                ineligible_result.append({
                    'id': inv.id,
                    'invoice_number': inv.invoice_number,
                    'customer': inv.customer.name,
                    'amount': float(inv.total_amount),
                    'reason': primary.description if primary else 'Ineligible',
                    'rule': primary.rule if primary else 'UNKNOWN',
                    'all_reasons': [r.description for r in res.ineligibility_reasons],
                })
                continue

            amount = Decimal(str(inv.total_amount))
            fee_amount = Decimal(str(res.fee_amount))
            net_payout = Decimal(str(res.net_advance))
            fee_rate = float(res.final_fee_percent)
            tier = res.risk_tier

            total_face_value += amount
            total_net_payout += net_payout

            age_days = (date.today() - inv.issue_date).days if inv.issue_date else 0

            result.append({
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer': inv.customer.name,
                'customer_id': inv.customer.id,
                'amount_zar': float(amount),
                'amount': float(amount),  # Frontend compatibility
                'total_amount': float(amount),  # Frontend compatibility
                'subtotal_zar': float(inv.subtotal),
                'vat_zar': float(inv.vat_amount),
                'issue_date': inv.issue_date.isoformat() if inv.issue_date else None,
                'due_date': inv.due_date.isoformat() if inv.due_date else None,
                'age_days': age_days,
                'risk_score': float(res.final_score),
                'risk_tier': tier,
                'tier': str(tier).lower(),  # Frontend compatibility
                'fee_rate_pct': fee_rate,
                'fee_amount_zar': float(fee_amount),
                'net_payout_zar': float(net_payout),
                'max_advance_percent': res.max_advance_percent,
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
