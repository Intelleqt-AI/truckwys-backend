# TENANCY AUDIT: 2026-03-15 — Billing views properly scoped
# - SubscribeView: Uses request.user.company ✓
# - CancelSubscriptionView: Uses request.user.company ✓
# - BillingStatusView: Uses request.user.company ✓
# - BillingHistoryView: Filters by request.user.company ✓
# - PayFastITNView: Public webhook (AllowAny) - exempt from company filtering ✓

"""Billing views for PayFast subscription management."""
import logging
from decimal import Decimal, InvalidOperation

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from django.utils import timezone

from .models import BillingTransaction
from .serializers_billing import (
    BillingTransactionSerializer, BillingStatusSerializer, SubscribeSerializer
)
from .services.payfast import (
    build_payment_data, validate_itn, confirm_payment_with_payfast, PLAN_PRICING
)
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_notify_url(request):
    """Build the ITN notify URL from the current request."""
    return request.build_absolute_uri('/api/v1/billing/itn/')


class SubscribeView(APIView):
    """POST /api/v1/billing/subscribe/ — Initiate a PayFast subscription."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SubscribeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        plan = serializer.validated_data['plan']
        return_url = serializer.validated_data.get('return_url', '')
        cancel_url = serializer.validated_data.get('cancel_url', '')
        notify_url = _get_notify_url(request)

        company = request.user.company
        user = request.user

        payment_data = build_payment_data(
            plan=plan,
            company_id=company.id,
            user_email=user.email or '',
            first_name=user.first_name or '',
            last_name=user.last_name or '',
            notify_url=notify_url,
            return_url=return_url,
            cancel_url=cancel_url,
        )

        # Create a pending transaction
        plan_info = PLAN_PRICING[plan]
        BillingTransaction.objects.create(
            company=company,
            amount=plan_info['amount'],
            payment_id=payment_data['form_data'].get('m_payment_id', ''),
            status='pending',
            plan=plan,
        )

        return Response({
            'payfast_url': payment_data['payfast_url'],
            'payment_url': payment_data['payfast_url'],
            'form_data': payment_data['form_data'],
            'plan': plan,
            'amount': str(plan_info['amount']),
            'item_name': plan_info['item_name'],
        }, status=status.HTTP_200_OK)


class CancelSubscriptionView(APIView):
    """POST /api/v1/billing/cancel/ — Cancel the active subscription."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = request.user.company

        if company.subscription_status not in ('active', 'trialing'):
            return Response(
                {'detail': 'No active subscription to cancel.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company.subscription_status = 'cancelled'
        company.payfast_token = None
        company.save(update_fields=['subscription_status', 'payfast_token', 'updated_at'])

        return Response({'detail': 'Subscription cancelled successfully.'})


class BillingStatusView(APIView):
    """GET /api/v1/billing/status/ — Current billing status."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = request.user.company
        serializer = BillingStatusSerializer(company)
        plan_info = PLAN_PRICING.get(company.subscription_plan, {})
        return Response({
            **serializer.data,
            'amount': str(plan_info.get('amount', '0.00')),
            'item_name': plan_info.get('item_name', 'Free'),
        })


class BillingHistoryView(APIView):
    """GET /api/v1/billing/history/ — Payment history for the company."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = request.user.company
        transactions = BillingTransaction.objects.filter(company=company)
        serializer = BillingTransactionSerializer(transactions, many=True)
        return Response({'results': serializer.data, 'count': transactions.count()})


class PayFastITNView(APIView):
    """
    POST /api/v1/billing/itn/ — PayFast Instant Transaction Notification webhook.

    This endpoint is publicly accessible (AllowAny) but validates the PayFast
    signature before processing any state changes.
    """
    permission_classes = [AllowAny]
    # PayFast sends form-encoded POST data, no CSRF token
    authentication_classes = []

    def post(self, request):
        # Capture the raw body FIRST: once request.POST reads the stream,
        # request.body raises RawPostDataException. The raw body is needed to
        # verify the ITN signature against PayFast's exact bytes/order.
        try:
            raw_body = request.body.decode('utf-8', 'ignore')
        except Exception:
            raw_body = ''
        post_data = request.POST.dict()
        source_ip = request.META.get('REMOTE_ADDR', '')

        # 1. Verify the ITN signature (and source IP in production).
        if not validate_itn(post_data, source_ip, raw_body):
            return Response({'detail': 'Invalid signature.'}, status=status.HTTP_400_BAD_REQUEST)

        # 2. Server-to-server confirmation: PayFast must echo this ITN as VALID.
        #    This defeats forged/replayed payloads that happen to carry a valid signature.
        if not confirm_payment_with_payfast(post_data):
            logger.warning("PayFast ITN failed server-to-server confirmation: %s", post_data.get('m_payment_id'))
            return Response({'detail': 'Unconfirmed.'}, status=status.HTTP_400_BAD_REQUEST)

        payment_status = post_data.get('payment_status', '')
        m_payment_id = post_data.get('m_payment_id', '')
        pf_payment_id = post_data.get('pf_payment_id', '')
        token = post_data.get('token', '')
        company_id = post_data.get('custom_str1', '')
        plan = post_data.get('custom_str2', '')

        txn = BillingTransaction.objects.filter(payment_id=m_payment_id).first()

        # 3. Idempotency: a transaction we've already completed is never re-processed.
        #    Acknowledge with 200 so PayFast stops retrying.
        if txn and txn.status == 'complete':
            return Response(status=status.HTTP_200_OK)

        # 4. Amount verification: the gross paid must equal the plan's price.
        #    Without this, a tampered ITN could grant a paid plan for any amount.
        if payment_status == 'COMPLETE':
            expected_amount = PLAN_PRICING.get(plan, {}).get('amount')
            try:
                amount_gross = Decimal(str(post_data.get('amount_gross', '0')))
            except (InvalidOperation, TypeError):
                amount_gross = Decimal('0')

            if expected_amount is None or amount_gross != expected_amount:
                logger.warning(
                    "PayFast ITN amount/plan mismatch: plan=%r gross=%s expected=%s",
                    plan, amount_gross, expected_amount,
                )
                BillingTransaction.objects.filter(payment_id=m_payment_id).update(
                    payfast_payment_id=pf_payment_id,
                    status='failed',
                    payment_status=payment_status,
                    raw_itn_data=post_data,
                )
                return Response({'detail': 'Amount mismatch.'}, status=status.HTTP_400_BAD_REQUEST)

        # Update billing transaction
        BillingTransaction.objects.filter(payment_id=m_payment_id).update(
            payfast_payment_id=pf_payment_id,
            status='complete' if payment_status == 'COMPLETE' else 'failed',
            payment_status=payment_status,
            raw_itn_data=post_data,
        )

        # Update company subscription on verified successful payment
        if payment_status == 'COMPLETE' and company_id:
            from .models import Company
            try:
                company = Company.objects.get(id=company_id)
                company.subscription_plan = plan or company.subscription_plan
                company.subscription_status = 'active'
                if token:
                    company.payfast_token = token
                if not company.subscription_start:
                    company.subscription_start = timezone.now()
                company.save(update_fields=[
                    'subscription_plan', 'subscription_status',
                    'payfast_token', 'subscription_start', 'updated_at',
                ])
            except Company.DoesNotExist:
                pass

        # PayFast expects a 200 OK with no body on success
        return Response(status=status.HTTP_200_OK)


class ConfirmPaymentView(APIView):
    """
    POST /api/v1/billing/confirm/
    Called by the frontend immediately after the user returns from PayFast
    (return_url fires before ITN in most cases). Finds the most recent pending
    transaction for the company and activates the subscription.

    In sandbox mode: activates without server-to-server check (ITN can't reach
    localhost). In production: verifies with PayFast before activating.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = request.user.company
        sandbox = getattr(settings, 'PAYFAST_SANDBOX', True)

        txn = (
            BillingTransaction.objects
            .filter(company=company, status='pending')
            .order_by('-created_at')
            .first()
        )

        if not txn:
            # Already activated (ITN arrived first) or nothing to confirm
            from .serializers_billing import BillingStatusSerializer
            return Response(BillingStatusSerializer(company).data)

        if not sandbox:
            # Production: verify with PayFast before trusting
            fake_itn = {
                'm_payment_id': txn.payment_id,
                'payment_status': 'COMPLETE',
                'custom_str1': str(company.id),
                'custom_str2': txn.plan or '',
            }
            if not confirm_payment_with_payfast(fake_itn):
                return Response(
                    {'detail': 'Payment could not be confirmed with PayFast. Please wait a moment and try again.'},
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )

        # Activate
        txn.status = 'complete'
        txn.payment_status = 'COMPLETE'
        txn.save(update_fields=['status', 'payment_status'])

        company.subscription_plan = txn.plan or company.subscription_plan
        company.subscription_status = 'active'
        if not company.subscription_start:
            company.subscription_start = timezone.now()
        company.save(update_fields=['subscription_plan', 'subscription_status', 'subscription_start', 'updated_at'])

        from .serializers_billing import BillingStatusSerializer
        return Response(BillingStatusSerializer(company).data)
