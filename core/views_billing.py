# TENANCY AUDIT: 2026-03-15 — Billing views properly scoped
# - SubscribeView: Uses request.user.company ✓
# - CancelSubscriptionView: Uses request.user.company ✓
# - BillingStatusView: Uses request.user.company ✓
# - BillingHistoryView: Filters by request.user.company ✓
# - PaystackWebhookView: Public webhook (AllowAny) - exempt from company filtering ✓

"""Billing views for Paystack subscription + take-rate management.

There's no separate "cancel"/"revoke" API call on Paystack's side to make
here — we never created a Paystack Subscription object (see
core/services/subscription_billing.py for why: one charge_authorization
primitive covers both the flat fee and the take-rate, so there's nothing
gateway-side to cancel — cancelling is purely a local subscription_status
flip that stops our own cron from charging them further).
"""
import logging
from decimal import Decimal, InvalidOperation

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework import status
from django.conf import settings
from django.utils import timezone

from .models import BillingTransaction
from .serializers_billing import (
    BillingStatusSerializer, SubscribeSerializer, ConfirmPaymentSerializer,
)
from .services import paystack
from .services.paystack import MONTHLY_FEE, MONTHLY_FEE_ITEM_NAME

logger = logging.getLogger(__name__)


def _activate_from_verified_charge(company, txn, data: dict) -> bool:
    """Shared by ConfirmPaymentView (return_url) and PaystackWebhookView
    (async backup — same race PayFast's ITN-vs-return_url covered, now for
    charge.failed too, not just charge.success — see the webhook's docstring).
    Idempotent both ways. Returns True if the charge was valid and applied.
    """
    from core.services.notify import notify_company_billing_email

    if txn.status in ('complete', 'failed'):
        return txn.status == 'complete'  # already processed by the other path — don't re-email either way

    if (data or {}).get('status') != 'success':
        txn.status = 'failed'
        txn.payment_status = str((data or {}).get('status', ''))
        txn.raw_gateway_response = data or {}
        txn.save(update_fields=['status', 'payment_status', 'raw_gateway_response', 'updated_at'])
        notify_company_billing_email(
            company.id, 'Subscription payment failed',
            f"We couldn't confirm your {MONTHLY_FEE_ITEM_NAME} payment (R{MONTHLY_FEE:,.2f}). "
            "Your subscription was not activated — please try again.",
            link='/settings/billing',
        )
        return False

    # Amount verification: the gross paid must equal what we charged for.
    # Without this, a tampered/replayed payload could grant an active plan
    # for any amount.
    expected_cents = int((txn.amount * 100).quantize(Decimal('1')))
    if int(data.get('amount', -1)) != expected_cents:
        logger.warning(
            "Paystack charge amount mismatch: txn=%s expected=%s got=%s",
            txn.id, expected_cents, data.get('amount'),
        )
        txn.status = 'failed'
        txn.payment_status = 'amount_mismatch'
        txn.raw_gateway_response = data
        txn.save(update_fields=['status', 'payment_status', 'raw_gateway_response', 'updated_at'])
        notify_company_billing_email(
            company.id, 'Subscription payment failed',
            f"We couldn't confirm your {MONTHLY_FEE_ITEM_NAME} payment (R{MONTHLY_FEE:,.2f}) — the amount charged "
            "didn't match. Your subscription was not activated — please try again or contact support.",
            link='/settings/billing',
        )
        return False

    authorization = data.get('authorization') or {}
    customer = data.get('customer') or {}

    txn.status = 'complete'
    txn.payment_status = 'success'
    txn.gateway_transaction_id = str(data.get('id', ''))
    txn.raw_gateway_response = data
    txn.save(update_fields=['status', 'payment_status', 'gateway_transaction_id', 'raw_gateway_response', 'updated_at'])

    company.subscription_plan = txn.plan or 'pro'
    if authorization.get('authorization_code'):
        company.paystack_authorization_code = authorization['authorization_code']
        company.paystack_authorization_email = customer.get('email') or company.paystack_authorization_email
        company.paystack_card_last4 = authorization.get('last4', '') or ''
        company.paystack_card_type = authorization.get('card_type', '') or ''
        company.paystack_bank = authorization.get('bank', '') or ''
    if customer.get('customer_code'):
        company.paystack_customer_code = customer['customer_code']
    if not company.subscription_start:
        company.subscription_start = timezone.now()
    # This charge covers the month it lands in — next one is due a month out.
    from core.services.subscription_billing import add_one_month, record_charge_success
    company.next_billing_date = add_one_month(timezone.now().date())
    company.save(update_fields=[
        'subscription_plan', 'paystack_authorization_code',
        'paystack_authorization_email', 'paystack_card_last4', 'paystack_card_type',
        'paystack_bank', 'paystack_customer_code', 'subscription_start',
        'next_billing_date', 'updated_at',
    ])
    # record_charge_success only flips active/grace_period -> active — a
    # brand-new signup (status 'none') needs to go active explicitly here.
    record_charge_success(company)
    if company.subscription_status != 'active':
        company.subscription_status = 'active'
        company.save(update_fields=['subscription_status', 'updated_at'])

    notify_company_billing_email(
        company.id, 'Subscription payment confirmed',
        f'{MONTHLY_FEE_ITEM_NAME}: R{MONTHLY_FEE:,.2f} charged successfully. Your subscription is active.',
        link='/settings/billing',
    )
    return True


class SubscribeView(APIView):
    """POST /api/v1/billing/subscribe/ — Initiate the Paystack checkout that
    both charges the first month AND captures a reusable card-on-file. Also
    how a suspended/cancelled company reactivates — the successful charge
    itself is what flips subscription_status back to 'active'
    (_activate_from_verified_charge), whatever it was before.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SubscribeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        return_url = serializer.validated_data.get('return_url', '')
        company = request.user.company
        user = request.user

        if company.paystack_authorization_code and company.subscription_status == 'active':
            logger.info('Company %s starting a new checkout with an active card on file already.', company.id)

        result = paystack.initialize_transaction(
            email=user.email or '',
            amount=MONTHLY_FEE,
            callback_url=return_url,
            metadata={'company_id': company.id, 'plan': 'pro'},
        )
        if not result['success']:
            return Response({'detail': f"Could not start checkout: {result['error']}"}, status=status.HTTP_502_BAD_GATEWAY)

        reference = result['data']['reference']
        BillingTransaction.objects.create(
            company=company,
            amount=MONTHLY_FEE,
            payment_id=reference,
            status='pending',
            plan='pro',
        )

        return Response({
            'authorization_url': result['data']['authorization_url'],
            'reference': reference,
            'plan': 'pro',
            'amount': str(MONTHLY_FEE),
            'item_name': MONTHLY_FEE_ITEM_NAME,
        }, status=status.HTTP_200_OK)


class CancelSubscriptionView(APIView):
    """POST /api/v1/billing/cancel/ — Stop the monthly fee (and take-rate)
    cron from charging this company further. The card stays on file in case
    they resubscribe — nothing to revoke on Paystack's side.

    Per TruckWys_Fee_Billing_Spec.pdf §4: cancellation is an immediate,
    deliberate action ("Explicit cancellation action" -> 'cancelled', no
    grace period) — unlike a failed charge, which moves to 'grace_period'
    first. Quoting/invoicing block immediately (core/middleware/plan_limits.py).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = request.user.company

        if company.subscription_status not in ('active', 'grace_period', 'trialing'):
            return Response(
                {'detail': 'No active subscription to cancel.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company.subscription_status = 'cancelled'
        company.next_billing_date = None
        company.grace_period_expires_at = None
        company.save(update_fields=['subscription_status', 'next_billing_date', 'grace_period_expires_at', 'updated_at'])

        from core.services.notify import notify_company_billing_email
        notify_company_billing_email(
            company.id, 'Subscription cancelled',
            f'Your {MONTHLY_FEE_ITEM_NAME} subscription has been cancelled — you will not be charged again, and '
            'quoting/invoicing are now blocked. You can resubscribe any time from Settings → Billing.',
            link='/settings/billing',
        )

        return Response({'detail': 'Subscription cancelled successfully.'})


class BillingStatusView(APIView):
    """GET /api/v1/billing/status/ — Current billing status."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.services.subscription_billing import _grace_days

        company = request.user.company
        serializer = BillingStatusSerializer(company)
        is_paid = company.subscription_plan not in ('free', 'starter') and company.subscription_status == 'active'

        grace = None
        if company.subscription_status == 'grace_period' and company.grace_period_expires_at:
            days_remaining = (company.grace_period_expires_at.date() - timezone.now().date()).days
            grace = {
                'grace_period_expires_at': company.grace_period_expires_at,
                'days_remaining': max(0, days_remaining),
                'grace_period_days': _grace_days(),
            }

        return Response({
            **serializer.data,
            'amount': str(MONTHLY_FEE) if is_paid else '0.00',
            'item_name': MONTHLY_FEE_ITEM_NAME if is_paid else 'Free',
            'flat_plan': {
                'key': 'pro',
                'label': MONTHLY_FEE_ITEM_NAME,
                'amount': str(MONTHLY_FEE),
                # Surfaced so the signup screen can disclose the take-rate
                # BEFORE anyone adds a card — not just after the fact on an
                # invoice. Single source of truth: settings.DELIVERY_FEE_PCT.
                'take_rate_pct': str(getattr(settings, 'DELIVERY_FEE_PCT', 0.25)),
            },
            'card': {
                'last4': company.paystack_card_last4,
                'card_type': company.paystack_card_type,
                'bank': company.paystack_bank,
            } if company.paystack_card_last4 else None,
            # Set only while subscription_status == 'grace_period' — the
            # countdown to show in Billing Settings before suspension.
            'grace': grace,
            'suspended': company.subscription_status == 'suspended',
        })


class BillingHistoryView(APIView):
    """GET /api/v1/billing/history/ — every charge ever taken from this
    company's card: the monthly subscription fee AND the 0.25% delivery
    take-rate, merged into one list (they're separate models — see
    DeliveryFeeCharge's docstring for why) so there's one place a user can
    audit every deduction, not just the subscription ones.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import DeliveryFeeCharge

        company = request.user.company
        transactions = BillingTransaction.objects.filter(company=company)
        results = [
            {
                'id': f'sub-{t.id}',
                'kind': 'subscription',
                'label': MONTHLY_FEE_ITEM_NAME,
                'amount': str(t.amount),
                'status': t.status,
                'reference': t.gateway_transaction_id or t.payment_id,
                'created_at': t.created_at,
            }
            for t in transactions
        ]

        charges = DeliveryFeeCharge.objects.filter(company=company).select_related('invoice')
        results += [
            {
                'id': f'fee-{c.id}',
                'kind': 'delivery_fee',
                'label': f'Delivery fee · {c.invoice.invoice_number}',
                'amount': str(c.amount),
                # DeliveryFeeCharge and BillingTransaction use different status
                # vocabularies (charged/failed vs complete/failed) — normalise
                # so the frontend renders one consistent set of colours.
                'status': 'complete' if c.status == 'charged' else c.status,
                'reference': c.invoice.invoice_number,
                'created_at': c.created_at,
            }
            for c in charges
        ]
        results.sort(key=lambda r: r['created_at'], reverse=True)

        return Response({'results': results, 'count': len(results)})


class PaystackWebhookView(APIView):
    """POST /api/v1/billing/webhook/ — Paystack event webhook.

    Public (AllowAny) but validates the HMAC-SHA512 signature before
    processing anything.

    `charge.success` is the async backup for the initial subscribe checkout
    (return_url normally fires first via ConfirmPaymentView; this covers the
    race/drop case, same role PayFast's ITN played).

    `charge.failed` is the safety net for a checkout that's declined and then
    simply abandoned — Paystack's hosted checkout does NOT auto-redirect back
    to return_url on a decline (unlike success), it shows its own retry
    screen and waits for the shopper to close it. If they never do, the
    browser-driven paths (CompleteSignupView / ConfirmPaymentView) never
    fire, and without this handler nobody — not the customer, not
    TruckWys — would ever find out the payment failed. This webhook fires
    regardless of what the browser does, so it's the only guaranteed path.

    The take-rate and monthly-fee charges we trigger ourselves get their
    success/failure synchronously from the charge_authorization call itself,
    so they don't depend on this webhook.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        signature = request.headers.get('x-paystack-signature', '')
        if not paystack.verify_webhook_signature(request.body, signature):
            return Response({'detail': 'Invalid signature.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = request.data
        except Exception:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        event = payload.get('event', '')
        data = payload.get('data', {}) or {}
        reference = data.get('reference', '')

        if event == 'charge.success':
            txn = BillingTransaction.objects.filter(payment_id=reference).select_related('company').first()
            if txn:
                _activate_from_verified_charge(txn.company, txn, data)

        elif event == 'charge.failed':
            # Existing company reactivating/subscribing — a BillingTransaction
            # already exists (created 'pending' by SubscribeView up front).
            txn = BillingTransaction.objects.filter(payment_id=reference).select_related('company').first()
            if txn:
                _activate_from_verified_charge(txn.company, txn, data)
            else:
                # Fresh signup — no BillingTransaction exists yet for a
                # failed attempt (only ever created on success), so look up
                # the PendingSignup row this checkout belongs to instead.
                from core.models import PendingSignup
                from core.views import _notify_pending_signup_payment_failed
                pending = PendingSignup.objects.filter(paystack_reference=reference, email_verified=True).first()
                if pending:
                    _notify_pending_signup_payment_failed(
                        pending,
                        "We couldn't confirm your TruckWys subscription payment, so your account was not created. "
                        "Your registration details are saved — you can try the payment again.",
                    )
        # Other events (transfer.*, etc.) are acknowledged but not acted on.

        return Response(status=status.HTTP_200_OK)


class ConfirmPaymentView(APIView):
    """
    POST /api/v1/billing/confirm/ {"reference": "..."}
    Called by the frontend right after Paystack redirects back to
    callback_url (which arrives with ?reference=... appended). Verifies the
    transaction server-side and activates the subscription — this is the
    primary path; the webhook above is only the async backup.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = request.user.company
        serializer = ConfirmPaymentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        reference = serializer.validated_data['reference']

        txn = BillingTransaction.objects.filter(company=company, payment_id=reference).first()
        if not txn:
            return Response({'detail': 'No matching transaction found.'}, status=status.HTTP_404_NOT_FOUND)
        if txn.status == 'complete':
            return Response(BillingStatusSerializer(company).data)

        result = paystack.verify_transaction(reference)
        if not result['success']:
            return Response(
                {'detail': f"Payment could not be confirmed: {result['error']}"},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        ok = _activate_from_verified_charge(company, txn, result['data'])
        if not ok:
            return Response(
                {'detail': 'Payment was not successful.'},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        return Response(BillingStatusSerializer(company).data)
