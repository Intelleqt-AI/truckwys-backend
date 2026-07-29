from rest_framework import serializers
from .models import BillingTransaction, Company, DeliveryFeeCharge


class DeliveryFeeChargeSerializer(serializers.ModelSerializer):
    """The 0.25% take-rate charge on one invoice — nested onto InvoiceSerializer
    so a user can see, on the invoice itself, whether/when the platform fee
    was taken (see also BillingHistoryView, which lists these company-wide)."""
    class Meta:
        model = DeliveryFeeCharge
        fields = ['rate_pct', 'amount', 'status', 'charged_at', 'failure_reason']
        read_only_fields = fields


class BillingTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingTransaction
        fields = ['id', 'amount', 'payment_id', 'gateway_transaction_id', 'status', 'plan', 'created_at']
        read_only_fields = ['id', 'created_at']


class BillingStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = [
            'subscription_plan',
            'subscription_status',
            'subscription_start',
            'subscription_end',
            'next_billing_date',
        ]
        read_only_fields = fields


class SubscribeSerializer(serializers.Serializer):
    # No 'plan' field: there's only one flat plan — a client-sent plan or
    # amount must never influence the charge.
    return_url = serializers.URLField(required=False, default='')


class ConfirmPaymentSerializer(serializers.Serializer):
    reference = serializers.CharField()
