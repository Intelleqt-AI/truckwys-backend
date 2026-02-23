"""Partner API views for funding partners to review and approve advances."""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import BasePermission
from django.db.models import Q
from typing import Optional

from core.models import AdvanceRequest, Company
from core.serializers_capital import (
    PartnerAdvanceSerializer,
    RiskScoreSerializer,
    ApproveAdvanceSerializer,
    RejectAdvanceSerializer,
    DisburseAdvanceSerializer,
)


class PartnerAPIKeyAuthentication:
    """
    Simple API key authentication for partners.

    Checks for X-Partner-API-Key header and validates against company settings.
    """

    def authenticate(self, request):
        """
        Authenticate using X-Partner-API-Key header.

        Returns:
            tuple: (user, auth) or None
        """
        api_key = request.META.get('HTTP_X_PARTNER_API_KEY')

        if not api_key:
            return None

        # For now, we'll use a simple check against company settings
        # In production, you'd want a proper PartnerAPIKey model
        # For this implementation, we'll check if the key matches "partner-{company_id}"
        # or use a more secure approach with hashed keys

        # Simple validation: partner-key-{company_id}
        if api_key.startswith('partner-key-'):
            try:
                company_id = int(api_key.split('partner-key-')[1])
                company = Company.objects.get(id=company_id)

                # Create a fake user object for permission checking
                class PartnerUser:
                    is_authenticated = True
                    is_staff = True  # Partners have staff-like permissions
                    company = company
                    username = f'partner-{company.company_name}'

                return (PartnerUser(), api_key)
            except (ValueError, Company.DoesNotExist):
                return None

        return None

    def authenticate_header(self, request):
        """Return authentication header for 401 responses."""
        return 'X-Partner-API-Key'


class IsPartnerAuthenticated(BasePermission):
    """
    Permission class for partner API key authentication.

    Checks for valid X-Partner-API-Key header.
    """

    def has_permission(self, request, view):
        """Check if request has valid partner API key."""
        api_key = request.META.get('HTTP_X_PARTNER_API_KEY')

        if not api_key:
            return False

        # Validate API key
        auth = PartnerAPIKeyAuthentication()
        result = auth.authenticate(request)

        if result:
            user, _ = result
            request.user = user
            return True

        return False


class PartnerAdvanceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Partner API for reviewing and managing advances.

    list: GET /api/v1/partner/advances/ - List pending/approved advances
    retrieve: GET /api/v1/partner/advances/{id}/ - Get advance detail with full breakdown
    approve: POST /api/v1/partner/advances/{id}/approve/ - Approve advance
    reject: POST /api/v1/partner/advances/{id}/reject/ - Reject advance
    disburse: POST /api/v1/partner/advances/{id}/disburse/ - Confirm disbursement
    """

    serializer_class = PartnerAdvanceSerializer
    permission_classes = [IsPartnerAuthenticated]

    def get_queryset(self):
        """
        Get advances for review.

        Filter to pending/approved advances unless 'all' parameter is set.
        """
        # Get all advances (partners can see all statuses)
        queryset = AdvanceRequest.objects.all()

        # Filter by status if requested
        status_filter = self.request.query_params.get('status', None)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        else:
            # By default, show advances needing review
            queryset = queryset.filter(
                status__in=['REQUESTED', 'SCORING', 'APPROVED']
            )

        return queryset.order_by('-created_at')

    @action(detail=True, methods=['post'], url_path='approve')
    def approve(self, request, pk=None):
        """Partner approves advance request."""
        advance = self.get_object()

        serializer = ApproveAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            advance.approve()
            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            response_serializer = PartnerAdvanceSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, pk=None):
        """Partner rejects advance request."""
        advance = self.get_object()

        serializer = RejectAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            reason = serializer.validated_data['reason']
            advance.deny(reason)

            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            response_serializer = PartnerAdvanceSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'], url_path='disburse')
    def disburse(self, request, pk=None):
        """Partner confirms disbursement."""
        advance = self.get_object()

        serializer = DisburseAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            advance.disburse()

            if serializer.validated_data.get('notes'):
                advance.notes = serializer.validated_data['notes']
                advance.save()

            response_serializer = PartnerAdvanceSerializer(advance)
            return Response(response_serializer.data)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class PartnerOperatorViewSet(viewsets.ViewSet):
    """
    Partner API for viewing fleet operator profiles.

    retrieve: GET /api/v1/partner/operators/{company_id}/ - Get operator profile
    """

    permission_classes = [IsPartnerAuthenticated]

    def retrieve(self, request, pk=None):
        """
        Get fleet operator profile.

        Returns:
        - Company details
        - Facility information
        - Payment history
        - Advance history
        - Risk metrics
        """
        try:
            company = Company.objects.get(id=pk)
        except Company.DoesNotExist:
            return Response(
                {'error': f'Company with ID {pk} not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Get facility
        facility = company.facilities.filter(status='ACTIVE').first()

        facility_data = None
        if facility:
            facility_data = {
                'id': facility.id,
                'limit': facility.limit,
                'outstanding': facility.outstanding,
                'available': facility.available,
                'utilization_percent': facility.utilization_percent,
                'status': facility.status,
            }

        # Get advance statistics
        advances = AdvanceRequest.objects.filter(facility__company=company)

        advance_stats = {
            'total_count': advances.count(),
            'approved_count': advances.filter(status='APPROVED').count(),
            'disbursed_count': advances.filter(status='DISBURSED').count(),
            'settled_count': advances.filter(status='SETTLED').count(),
            'denied_count': advances.filter(status='DENIED').count(),
            'total_advanced': sum(
                [a.amount for a in advances.filter(status__in=['DISBURSED', 'SETTLED'])]
            ),
            'total_fees_earned': sum(
                [a.fee_amount for a in advances.filter(status='SETTLED')]
            ),
        }

        # Get risk score distribution
        risk_scores = company.risk_scores.all()
        risk_distribution = {}
        for tier_choice in ['EXCELLENT', 'GOOD', 'FAIR', 'ELEVATED', 'INELIGIBLE']:
            count = risk_scores.filter(tier=tier_choice).count()
            if count > 0:
                risk_distribution[tier_choice] = count

        # Get recent advances
        recent_advances = advances.order_by('-created_at')[:10]
        recent_advances_data = PartnerAdvanceSerializer(recent_advances, many=True).data

        return Response({
            'company': {
                'id': company.id,
                'name': company.company_name,
                'registration_number': company.registration_number,
                'industry': company.industry,
            },
            'facility': facility_data,
            'advance_statistics': advance_stats,
            'risk_distribution': risk_distribution,
            'recent_advances': recent_advances_data,
        })


class PartnerRiskScoreViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Partner API for viewing risk score details.

    retrieve: GET /api/v1/partner/risk/{score_id}/ - Get full risk score with all factors
    """

    serializer_class = RiskScoreSerializer
    permission_classes = [IsPartnerAuthenticated]

    def get_queryset(self):
        """Partners can view all risk scores."""
        from core.models import RiskScore
        return RiskScore.objects.all()

    def retrieve(self, request, pk=None):
        """Get full risk score with detailed breakdown."""
        risk_score = self.get_object()

        serializer = RiskScoreSerializer(risk_score)
        data = serializer.data

        # Add full breakdown
        data['full_breakdown'] = risk_score.factors_breakdown

        return Response(data)
