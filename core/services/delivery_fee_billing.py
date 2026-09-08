"""The 0.25% delivery take-rate: charged ad-hoc against the company's Paystack
card-on-file authorization the moment a load auto-invoices on delivery.

Per TruckWys_Fee_Billing_Spec.pdf §3, this charge doubles as a real-time card
health check — attempted every time regardless of whether the company is
currently 'active' or already 'grace_period' (full access either way; a
success is exactly how grace_period recovers back to active). It is NOT
attempted for 'suspended'/'cancelled' companies — but that should be moot in
practice, since invoice generation itself is gated for those statuses (see
core/services/invoicing.py and core/middleware/plan_limits.py), so there's
normally no invoice here to charge a fee on in the first place.

Flow: charge_delivery_fee_for_invoice() fires once, from the auto-invoice
signal. If it fails, the charge sits as 'failed' and
retry_failed_delivery_fee_charges() (a daily cron command) retries it. The
grace-period clock is company-level (Company.grace_period_expires_at, set by
core.services.subscription_billing.record_charge_failure) — NOT tracked per
charge — so suspension is driven by core.services.subscription_billing.check_grace_period_expirations,
not by anything in this module.

Never raises: a billing hiccup must never block the delivery/invoice flow.
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _rate_pct() -> Decimal:
    return Decimal(str(getattr(settings, 'DELIVERY_FEE_PCT', 0.25)))


def charge_delivery_fee_for_invoice(invoice):
    """Charge (or record a pending/failed attempt for) the take-rate fee on
    this invoice. Idempotent — safe to call more than once for the same
    invoice. Returns the DeliveryFeeCharge, or None if the feature is off or
    the company isn't in a billable state (never subscribed, or already
    suspended/cancelled — see module docstring).
    """
    if not getattr(settings, 'AUTO_CHARGE_DELIVERY_FEE', True):
        return None

    from core.models import DeliveryFeeCharge
    from core.services.paystack import charge_authorization
    from core.services.notify import notify_company, notify_company_billing_email
    from core.services.subscription_billing import record_charge_attempt, record_charge_success, record_charge_failure, _grace_days

    company = invoice.company
    # The take-rate is a term of the PAID subscription ("R4,499/month plus
    # 0.25%") — a company that never subscribed never agreed to it and has
    # no card to charge. 'suspended'/'cancelled' shouldn't reach here at all
    # (no invoice should exist to call this for), but skip defensively too.
    if (company is None or company.subscription_status not in ('active', 'grace_period')
            or not company.paystack_authorization_code):
        return None

    charge, created = DeliveryFeeCharge.objects.get_or_create(
        invoice=invoice,
        defaults={
            'company': company,
            'rate_pct': _rate_pct(),
            'base_amount': invoice.total_amount,
            'amount': (Decimal(str(invoice.total_amount)) * _rate_pct() / Decimal('100')).quantize(Decimal('0.01')),
        },
    )
    if charge.status == 'charged':
        return charge  # already done — never double-charge

    now = timezone.now()
    charge.attempt_count += 1
    if not charge.first_attempted_at:
        charge.first_attempted_at = now
    charge.last_attempted_at = now

    record_charge_attempt(company)
    result = charge_authorization(
        company.paystack_authorization_code, company.paystack_authorization_email, charge.amount,
        metadata={'kind': 'delivery_fee', 'invoice_number': invoice.invoice_number},
    )
    charge.gateway_response = result.get('raw') or {}

    if result['success']:
        charge.status = 'charged'
        charge.charged_at = now
        charge.failure_reason = ''
        charge.save()
        record_charge_success(company)
        title = 'Delivery fee charged'
        message = f'R{float(charge.amount):,.2f} (0.25% of {invoice.invoice_number}) charged successfully.'
        link = f'/finance/invoices/{invoice.id}'
        notify_company(company.id, 'SUCCESS', title, message, link=link, event='delivery_fee.charged')
        notify_company_billing_email(company.id, title, message, link=link)
        return charge

    charge.status = 'failed'
    charge.failure_reason = result['error'] or 'Unknown error'
    charge.save()

    entered_grace = record_charge_failure(company)
    if entered_grace:
        grace_deadline = company.grace_period_expires_at
        title = 'Could not charge delivery fee'
        message = (
            f"We couldn't charge R{float(charge.amount):,.2f} (0.25% of {invoice.invoice_number}) to your "
            f"card on file. You have until {grace_deadline.strftime('%d %b %Y')} to resolve this before your "
            "account is suspended."
        )
        notify_company(company.id, 'ALERT', title, message, link='/settings/billing', event='delivery_fee.failed')
        notify_company_billing_email(company.id, title, message, link='/settings/billing')
    return charge


def retry_failed_delivery_fee_charges() -> dict:
    """Daily sweep: retry every 'failed' charge for companies still in
    'active'/'grace_period' (nothing to gain retrying a suspended/cancelled
    company — they need to explicitly reactivate first). Suspension itself
    is handled separately by
    core.services.subscription_billing.check_grace_period_expirations, once
    the company-level grace clock runs out.

    Returns a summary dict for the management command to print.
    """
    from core.models import DeliveryFeeCharge
    from core.services.paystack import charge_authorization
    from core.services.notify import notify_company, notify_company_billing_email
    from core.services.subscription_billing import record_charge_attempt, record_charge_success, record_charge_failure

    now = timezone.now()
    summary = {
        'retried': 0, 'charged': 0, 'still_failing': 0, 'entered_grace': 0,
        'skipped_not_billable': 0,
        # Cards whose stored authorization is permanently dead — cleared here
        # rather than retried forever. Callers print this; keep the key.
        'dead_authorization': 0,
    }

    charges = DeliveryFeeCharge.objects.filter(status='failed').select_related('company', 'invoice')
    for charge in charges:
        company = charge.company
        if company is None or company.subscription_status not in ('active', 'grace_period'):
            summary['skipped_not_billable'] += 1
            continue

        summary['retried'] += 1
        charge.attempt_count += 1
        charge.last_attempted_at = now

        record_charge_attempt(company)
        result = charge_authorization(
            company.paystack_authorization_code, company.paystack_authorization_email, charge.amount,
            metadata={'kind': 'delivery_fee', 'invoice_number': charge.invoice.invoice_number},
        )
        charge.gateway_response = result.get('raw') or {}

        if result['success']:
            charge.status = 'charged'
            charge.charged_at = now
            charge.failure_reason = ''
            charge.save()
            record_charge_success(company)
            summary['charged'] += 1
            title = 'Delivery fee charged'
            message = f'R{float(charge.amount):,.2f} (0.25% of {charge.invoice.invoice_number}) charged successfully after retry.'
            link = f'/finance/invoices/{charge.invoice_id}'
            notify_company(company.id, 'SUCCESS', title, message, link=link, event='delivery_fee.charged')
            notify_company_billing_email(company.id, title, message, link=link)
            continue

        charge.failure_reason = result['error'] or 'Unknown error'
        charge.save()

        if result.get('dead_authorization'):
            # The token is permanently invalid — every future sweep would fail
            # on it identically. Clear it so charge_authorization() short-circuits
            # ("No Paystack authorization on file") instead of hitting Paystack
            # again daily, and tell the company to re-add their card. The grace
            # clock below still runs, so this doesn't let them off the hook.
            company.paystack_authorization_code = ''
            company.save(update_fields=['paystack_authorization_code', 'updated_at'])
            summary['dead_authorization'] += 1
            title = 'Your saved card is no longer valid'
            message = (
                f"We couldn't charge R{float(charge.amount):,.2f} (0.25% of "
                f"{charge.invoice.invoice_number}) because the saved card can no longer be "
                "charged. Please add a payment method again to keep your account active."
            )
            notify_company(company.id, 'ALERT', title, message, link='/settings/billing',
                           event='delivery_fee.failed')
            notify_company_billing_email(company.id, title, message, link='/settings/billing')

        entered_grace = record_charge_failure(company)
        if entered_grace:
            summary['entered_grace'] += 1
            grace_deadline = company.grace_period_expires_at
            title = 'Could not charge delivery fee'
            message = (
                f"We couldn't charge R{float(charge.amount):,.2f} (0.25% of {charge.invoice.invoice_number}) to "
                f"your card on file. You have until {grace_deadline.strftime('%d %b %Y')} to resolve this before "
                "your account is suspended."
            )
            notify_company(company.id, 'ALERT', title, message, link='/settings/billing', event='delivery_fee.failed')
            notify_company_billing_email(company.id, title, message, link='/settings/billing')
        else:
            summary['still_failing'] += 1

    return summary
