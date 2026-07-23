from rest_framework import serializers
from .models import BillingTransaction, Company


class BillingTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingTransaction
        fields = ['id', 'amount', 'payment_id', 'payfast_payment_id', 'status', 'plan', 'created_at']
        read_only_fields = ['id', 'created_at']


class BillingStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = [
            'subscription_plan',
            'subscription_status',
            'subscription_start',
            'subscription_end',
            'payfast_token',
        ]
        read_only_fields = fields


class SubscribeSerializer(serializers.Serializer):
    # No 'plan' field: the tier is derived server-side from the company's
    # vehicle count — a client-sent plan must never influence the charge.
    return_url = serializers.URLField(required=False, default='')
    cancel_url = serializers.URLField(required=False, default='')
