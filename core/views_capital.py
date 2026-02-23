"""Capital module views for facilities, risk scoring, and advance requests."""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count, Sum
from django.utils import timezone
from decimal import Decimal
from typing import Dict, Any

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
        return Facility.objects.filter(company=user.company)

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
        return RiskScore.objects.filter(company=user.company)

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

        # Check permissions
        user = request.user
        if not user.is_staff and invoice.company != user.company:
            return Response(
                {'error': 'You do not have permission to score this invoice'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Get or create facility for company
        facility = Facility.objects.filter(
            company=invoice.company,
            status='ACTIVE'
        ).first()

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
        if user.is_staff:
            return AdvanceRequest.objects.all()
        return AdvanceRequest.objects.filter(facility__company=user.company)

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

        # Check permissions
        user = request.user
        if not user.is_staff and invoice.company != user.company:
            return Response(
                {'error': 'You do not have permission to request advance on this invoice'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Get active facility
        facility = Facility.objects.filter(
            company=invoice.company,
            status='ACTIVE'
        ).first()

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
            return Response(
                {
                    'error': 'Invoice is not eligible for advance',
                    'reason': result.ineligibility_reason,
                    'total_score': result.total_score,
                    'tier': result.tier,
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
            fee_percent=result.fee_percent,
            fee_amount=result.fee_amount,
            net_amount=result.net_amount,
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
        Get capital dashboard data.

        Returns:
        - facility_limit, facility_outstanding, facility_available, utilization_percent
        - eligible_invoices_count, eligible_invoices_total
        - active_advances (count, total)
        - risk_distribution (count per tier)
        - advance_history (last 20)
        - total_fees_earned (from settled advances)
        """
        user = request.user

        # Get company
        if user.is_staff:
            # For admin, aggregate all companies (or require company filter)
            return Response(
                {'error': 'Admin users must specify a company_id parameter'},
                status=status.HTTP_400_BAD_REQUEST
            )

        company = user.company

        # Get facility data
        facility = Facility.objects.filter(company=company, status='ACTIVE').first()

        if not facility:
            return Response(
                {
                    'facility': None,
                    'eligible_invoices_count': 0,
                    'eligible_invoices_total': Decimal('0.00'),
                    'active_advances': {'count': 0, 'total': Decimal('0.00')},
                    'risk_distribution': {},
                    'advance_history': [],
                    'total_fees_earned': Decimal('0.00'),
                },
                status=status.HTTP_200_OK
            )

        # Facility metrics
        facility_data = {
            'facility_limit': facility.limit,
            'facility_outstanding': facility.outstanding,
            'facility_available': facility.available,
            'utilization_percent': facility.utilization_percent,
        }

        # Eligible invoices (unpaid invoices without active advances)
        eligible_invoices = Invoice.objects.filter(
            company=company,
            status='SENT'
        ).exclude(
            advance_requests__status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        )

        eligible_invoices_data = {
            'count': eligible_invoices.count(),
            'total': eligible_invoices.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00'),
        }

        # Active advances
        active_advances = AdvanceRequest.objects.filter(
            facility=facility,
            status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        )

        active_advances_data = {
            'count': active_advances.count(),
            'total': active_advances.aggregate(total=Sum('amount'))['total'] or Decimal('0.00'),
        }

        # Risk distribution
        risk_distribution = RiskScore.objects.filter(
            company=company
        ).values('tier').annotate(count=Count('id'))

        risk_distribution_data = {item['tier']: item['count'] for item in risk_distribution}

        # Advance history (last 20)
        advance_history = AdvanceRequest.objects.filter(
            facility=facility
        ).order_by('-created_at')[:20]

        advance_history_data = AdvanceRequestSerializer(advance_history, many=True).data

        # Total fees earned from settled advances
        settled_advances = AdvanceRequest.objects.filter(
            facility=facility,
            status='SETTLED'
        )

        total_fees_earned = settled_advances.aggregate(
            total=Sum('fee_amount')
        )['total'] or Decimal('0.00')

        return Response({
            'facility': facility_data,
            'eligible_invoices': eligible_invoices_data,
            'active_advances': active_advances_data,
            'risk_distribution': risk_distribution_data,
            'advance_history': advance_history_data,
            'total_fees_earned': total_fees_earned,
        })
