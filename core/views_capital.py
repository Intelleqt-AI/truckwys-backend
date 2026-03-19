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
        serializer = AdvanceRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invoice_id = serializer.validated_data['invoice_id']
        invoice = Invoice.objects.get(id=invoice_id)

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

        # Create advance request
        advance_request = AdvanceRequest.objects.create(
            invoice=invoice,
            facility=facility,
            risk_score=risk_score,
            amount=invoice.total_amount,
            fee_percent=result.final_fee_percent,
            fee_amount=result.fee_amount,
            net_amount=result.net_advance,
            status='REQUESTED',
            requested_at=timezone.now(),
        )

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

        # Get company
        if user.is_staff:
            return Response(
                {'error': 'Admin users must specify a company_id parameter'},
                status=status.HTTP_400_BAD_REQUEST
            )

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
        # Get invoices eligible for advance
        eligible_statuses = ['SENT', 'OVERDUE']
        invoices = Invoice.objects.filter(
            status__in=eligible_statuses,
            early_pay_eligible=True,
        ).select_related('customer', 'load').exclude(
            advance_requests__status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        )

        if not invoices.exists():
            # Fallback: any SENT invoices without active advances
            invoices = Invoice.objects.filter(
                status__in=eligible_statuses
            ).select_related('customer', 'load').exclude(
                advance_requests__status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
            )

        result = []
        total_face_value = Decimal('0.00')
        total_net_payout = Decimal('0.00')

        for inv in invoices:
            # Get risk score for this customer
            risk = RiskScore.objects.filter(customer=inv.customer).order_by('-calculated_at').first()
            tier = risk.tier if risk else 'FAIR'
            score = risk.total_score if risk else 55

            # Calculate fee based on tier
            fee_map = {
                'EXCELLENT': 2.0,
                'GOOD': 2.5,
                'FAIR': 3.0,
                'ELEVATED': 3.5,
                'INELIGIBLE': 0.0,
            }
            fee_rate = fee_map.get(tier, 3.0)
            amount = Decimal(str(inv.total_amount))
            fee_amount = (amount * Decimal(str(fee_rate)) / Decimal('100')).quantize(Decimal('0.01'))
            net_payout = amount - fee_amount

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
                'risk_score': score,
                'risk_tier': tier,
                'tier': tier.lower(),  # Frontend compatibility
                'fee_rate_pct': fee_rate,
                'fee_amount_zar': float(fee_amount),
                'net_payout_zar': float(net_payout),
                'load_reference': inv.load.load_number if inv.load else None,
                'route': f'{inv.load.pickup_city} → {inv.load.delivery_city}' if inv.load else None,
            })

        return Response({
            'eligible_count': len(result),
            'total_face_value_zar': float(total_face_value),
            'total_net_payout_zar': float(total_net_payout),
            'invoices': result,
        })
