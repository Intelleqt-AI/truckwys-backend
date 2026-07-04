"""Serializers for capital module (facilities, risk scores, advance requests)."""

from rest_framework import serializers
from decimal import Decimal
from core.models import Facility, RiskScore, AdvanceRequest, Invoice, Customer, Company


class FacilitySerializer(serializers.ModelSerializer):
    """Serializer for Facility model with computed utilization."""

    available = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        read_only=True,
        help_text='Available facility amount (limit - outstanding)'
    )
    utilization_percent = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        read_only=True,
        help_text='Facility utilization percentage'
    )
    company_name = serializers.CharField(source='company.company_name', read_only=True)

    class Meta:
        model = Facility
        fields = [
            'id',
            'company',
            'company_name',
            'limit',
            'outstanding',
            'available',
            'utilization_percent',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'outstanding', 'created_at', 'updated_at']


class RiskScoreSerializer(serializers.ModelSerializer):
    """Serializer for RiskScore model with full factor breakdown."""

    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    is_valid = serializers.BooleanField(read_only=True)
    days_until_expiry = serializers.IntegerField(read_only=True)

    class Meta:
        model = RiskScore
        fields = [
            'id',
            'invoice',
            'invoice_number',
            'customer',
            'customer_name',
            'company',
            'total_score',
            'tier',
            'fee_percent',
            'fee_amount',
            'factor_payment_history',
            'factor_invoice_age',
            'factor_pod_quality',
            'factor_credit_score',
            'factor_relationship_length',
            'factor_facility_ratio',
            'factors_breakdown',
            'is_eligible',
            'ineligibility_reason',
            'calculated_at',
            'expires_at',
            'is_expired',
            'is_valid',
            'days_until_expiry',
            'created_at',
        ]
        read_only_fields = [
            'id',
            'total_score',
            'tier',
            'fee_percent',
            'fee_amount',
            'factor_payment_history',
            'factor_invoice_age',
            'factor_pod_quality',
            'factor_credit_score',
            'factor_relationship_length',
            'factor_facility_ratio',
            'factors_breakdown',
            'is_eligible',
            'ineligibility_reason',
            'calculated_at',
            'expires_at',
            'created_at',
        ]


class AdvanceRequestSerializer(serializers.ModelSerializer):
    """Serializer for AdvanceRequest model with nested relationships."""

    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    invoice_total = serializers.DecimalField(
        source='invoice.total_amount',
        max_digits=10,
        decimal_places=2,
        read_only=True
    )
    customer_name = serializers.CharField(source='invoice.customer.name', read_only=True)
    invoice_due_date = serializers.DateField(source='invoice.due_date', read_only=True)
    facility_limit = serializers.DecimalField(
        source='facility.limit',
        max_digits=12,
        decimal_places=2,
        read_only=True
    )
    risk_score_detail = RiskScoreSerializer(source='risk_score', read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    is_settled = serializers.BooleanField(read_only=True)
    days_to_settlement = serializers.IntegerField(read_only=True)

    class Meta:
        model = AdvanceRequest
        fields = [
            'id',
            'invoice',
            'invoice_number',
            'invoice_total',
            'invoice_due_date',
            'customer_name',
            'facility',
            'facility_limit',
            'risk_score',
            'risk_score_detail',
            'amount',
            'fee_amount',
            'fee_percent',
            'net_amount',
            'status',
            'requested_at',
            'approved_at',
            'disbursed_at',
            'settled_at',
            'denial_reason',
            'notes',
            'is_active',
            'is_settled',
            'days_to_settlement',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'requested_at',
            'approved_at',
            'disbursed_at',
            'settled_at',
            'created_at',
            'updated_at',
        ]


class PartnerAdvanceSerializer(serializers.ModelSerializer):
    """
    Extended serializer for partner API with full invoice and customer details.

    Includes all information needed for funding partners to make decisions.
    """

    # Invoice details
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    invoice_total = serializers.DecimalField(
        source='invoice.total_amount',
        max_digits=10,
        decimal_places=2,
        read_only=True
    )
    invoice_status = serializers.CharField(source='invoice.status', read_only=True)
    invoice_issue_date = serializers.DateField(source='invoice.issue_date', read_only=True)
    invoice_due_date = serializers.DateField(source='invoice.due_date', read_only=True)
    invoice_age_days = serializers.IntegerField(source='invoice.age_days', read_only=True)

    # Customer details
    customer_name = serializers.CharField(source='invoice.customer.name', read_only=True)
    customer_email = serializers.EmailField(source='invoice.customer.email', read_only=True)
    customer_credit_score = serializers.IntegerField(source='invoice.customer.credit_score', read_only=True)
    customer_relationship_months = serializers.IntegerField(
        source='invoice.customer.relationship_months',
        read_only=True
    )

    # Company details
    company_name = serializers.CharField(source='facility.company.company_name', read_only=True)

    # POD details
    has_pod = serializers.SerializerMethodField()
    pod_type = serializers.SerializerMethodField()
    pod_verified = serializers.SerializerMethodField()

    # Risk score
    risk_score_detail = RiskScoreSerializer(source='risk_score', read_only=True)

    # Facility details
    facility_limit = serializers.DecimalField(
        source='facility.limit',
        max_digits=12,
        decimal_places=2,
        read_only=True
    )
    facility_utilization = serializers.DecimalField(
        source='facility.utilization_percent',
        max_digits=5,
        decimal_places=2,
        read_only=True
    )

    class Meta:
        model = AdvanceRequest
        fields = [
            'id',
            'invoice',
            'invoice_number',
            'invoice_total',
            'invoice_status',
            'invoice_issue_date',
            'invoice_due_date',
            'invoice_age_days',
            'customer_name',
            'customer_email',
            'customer_credit_score',
            'customer_relationship_months',
            'company_name',
            'has_pod',
            'pod_type',
            'pod_verified',
            'facility',
            'facility_limit',
            'facility_utilization',
            'risk_score',
            'risk_score_detail',
            'amount',
            'fee_amount',
            'fee_percent',
            'net_amount',
            'status',
            'requested_at',
            'approved_at',
            'disbursed_at',
            'settled_at',
            'denial_reason',
            'notes',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'requested_at',
            'approved_at',
            'disbursed_at',
            'settled_at',
            'created_at',
            'updated_at',
        ]

    def get_has_pod(self, obj: AdvanceRequest) -> bool:
        """Check if invoice has POD."""
        if hasattr(obj.invoice, 'trip') and obj.invoice.trip:
            return obj.invoice.trip.has_pod
        return False

    def get_pod_type(self, obj: AdvanceRequest) -> str:
        """Get POD type."""
        if hasattr(obj.invoice, 'trip') and obj.invoice.trip:
            return obj.invoice.trip.pod_type
        return 'NONE'

    def get_pod_verified(self, obj: AdvanceRequest) -> bool:
        """Check if POD is verified."""
        if hasattr(obj.invoice, 'trip') and obj.invoice.trip:
            return obj.invoice.trip.pod_verified
        return False


class RiskScoreRequestSerializer(serializers.Serializer):
    """Serializer for risk score calculation request."""

    invoice_id = serializers.IntegerField(required=True, help_text='Invoice ID to score')

    def validate_invoice_id(self, value: int) -> int:
        """Validate that invoice exists."""
        try:
            Invoice.objects.get(id=value)
        except Invoice.DoesNotExist:
            raise serializers.ValidationError(f"Invoice with ID {value} does not exist")
        return value


class AdvanceRequestCreateSerializer(serializers.Serializer):
    """Serializer for creating advance request."""

    invoice_id = serializers.IntegerField(required=True, help_text='Invoice ID to advance')

    def validate_invoice_id(self, value: int) -> int:
        """Validate that invoice exists and is eligible."""
        try:
            invoice = Invoice.objects.get(id=value)
        except Invoice.DoesNotExist:
            raise serializers.ValidationError(f"Invoice with ID {value} does not exist")

        # Check if invoice already has an active advance
        active_advance = AdvanceRequest.objects.filter(
            invoice=invoice,
            status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        ).first()

        if active_advance:
            raise serializers.ValidationError(
                f"Invoice already has an active advance request (ID: {active_advance.id})"
            )

        return value


class ApproveAdvanceSerializer(serializers.Serializer):
    """Serializer for approving advance request."""

    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='Optional notes about the approval'
    )


class RejectAdvanceSerializer(serializers.Serializer):
    """Serializer for rejecting advance request."""

    reason = serializers.CharField(
        required=True,
        help_text='Reason for rejection'
    )
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='Optional additional notes'
    )


class DisburseAdvanceSerializer(serializers.Serializer):
    """Serializer for disbursing advance request."""

    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='Optional notes about the disbursement'
    )


class SettleAdvanceSerializer(serializers.Serializer):
    """Serializer for settling advance request."""

    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='Optional notes about the settlement'
    )
