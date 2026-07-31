"""The flat R4,499/month subscription fee — charged ad-hoc via
charge_authorization against the company's Paystack card-on-file, on the
monthly anniversary of their first successful payment.

Also home to the shared state-machine transition helpers used by BOTH the
monthly fee and the 0.25% delivery take-rate (core/services/delivery_fee_billing.py)
— per TruckWys_Fee_Billing_Spec.pdf §4, every charge attempt (whichever type)
drives the same subscription_status field:

    active        billing current                      full access
    grace_period  most recent charge attempt failed     full access (temporary)
    suspended     grace period expired, unresolved      quoting/invoicing blocked
    cancelled     explicit cancellation                 quoting/invoicing blocked

No Paystack Plan/Subscription object is used (see paystack.py docstring for
why): this cron + charge_authorization is the only recurring-billing
mechanism, shared with the take-rate.

Never raises: a billing hiccup must never block anything else.
"""
import calendar
import logging
from datetime import date

from django.utils import timezone

logger = logging.getLogger(__name__)


def _grace_days() -> int:
    from django.conf import settings
    return int(getattr(settings, 'DELIVERY_FEE_GRACE_DAYS', 7))


def add_one_month(d: date) -> date:
    """d one calendar month later, clamped to the target month's last day
    (e.g. Jan 31 -> Feb 28/29, not Mar 3)."""
    month = d.month + 1
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def billing_at_for_date(d: date):
    """The exact datetime a given next_billing_date will actually be
    attempted — 07:00 SAST, matching run_monthly_subscription_billing's
    Celery Beat schedule. Feeds Company.next_billing_at, which exists purely
    for the billing page's live countdown — charging itself still keys off
    next_billing_date (a plain date), unaffected by this."""
    from datetime import datetime, time as dt_time
    return timezone.make_aware(datetime.combine(d, dt_time(7, 0)))


# ---------------------------------------------------------------------------
# Shared state-machine transitions (spec §4) — called from every charge site:
# charge_monthly_subscription_fee below, and delivery_fee_billing's charge +
# retry functions. Centralised so the two charge types can never drift into
# inconsistent status handling.
# ---------------------------------------------------------------------------

def record_charge_attempt(company):
    """Stamp last_charge_attempt_at — called before every charge_authorization
    call, success or failure (spec §7's suggested field)."""
    company.last_charge_attempt_at = timezone.now()
    company.save(update_fields=['last_charge_attempt_at', 'updated_at'])


def record_charge_success(company):
    """Any successful charge (subscription or take-rate) returns the company
    to 'active' and clears the grace clock — spec §4: 'any successful charge
    during that window moves status straight back to active.'"""
    if company.subscription_status in ('active', 'grace_period'):
        company.subscription_status = 'active'
        company.grace_period_expires_at = None
        company.save(update_fields=['subscription_status', 'grace_period_expires_at', 'updated_at'])


def record_charge_failure(company) -> bool:
    """A charge attempt failed. Returns True only if this is what just pushed
    the company from 'active' into 'grace_period' — the caller should send
    the dunning notice ONLY in that case, not on every subsequent retry
    within the same window (spec §4's retry window is a single fixed 3-7 day
    countdown from the FIRST failure, not extended/reset by each retry).
    """
    if company.subscription_status != 'active':
        return False  # already grace_period/suspended/cancelled — no new transition
    company.subscription_status = 'grace_period'
    company.grace_period_expires_at = timezone.now() + timezone.timedelta(days=_grace_days())
    company.save(update_fields=['subscription_status', 'grace_period_expires_at', 'updated_at'])
    return True


def check_grace_period_expirations() -> dict:
    """Daily sweep (Celery Beat): suspend any company whose grace period has
    expired with no successful charge — spec §4: 'if the window elapses with
    no success, status moves to suspended.'
    """
    from core.models import Company
    from core.services.notify import notify_company, notify_company_billing_email

    now = timezone.now()
    summary = {'checked': 0, 'suspended': 0}

    expiring = Company.objects.filter(subscription_status='grace_period', grace_period_expires_at__isnull=False)
    for company in expiring:
        summary['checked'] += 1
        if company.grace_period_expires_at > now:
            continue

        company.subscription_status = 'suspended'
        company.save(update_fields=['subscription_status', 'updated_at'])
        summary['suspended'] += 1

        title = 'Account suspended — payment still failing'
        message = (
            "Your grace period ended without a successful charge. Quoting and invoicing are now blocked "
            "until you update your card and we're able to charge you successfully."
        )
        notify_company(company.id, 'ALERT', title, message, link='/settings/billing', event='subscription.suspended')
        notify_company_billing_email(company.id, title, message, link='/settings/billing')

    return summary


# ---------------------------------------------------------------------------
# The flat monthly fee itself.
# ---------------------------------------------------------------------------

def charge_monthly_subscription_fee(company) -> dict:
    """Charge this month's flat fee for an active-or-grace-period company.
    Idempotent per day — safe to call more than once (won't double-charge the
    same billing cycle). Returns a result dict; never raises.

    Runs for 'grace_period' companies too, not just 'active' — spec §4/§7:
    "a scheduled job retries the charge over the grace window." A success
    here is exactly how a company recovers back to 'active' automatically.
    """
    from core.models import BillingTransaction
    from core.services.paystack import charge_authorization, MONTHLY_FEE, MONTHLY_FEE_ITEM_NAME
    from core.services.notify import notify_company, notify_company_billing_email

    today = timezone.now().date()

    if company.subscription_status not in ('active', 'grace_period'):
        return {'charged': False, 'reason': 'not an active/grace-period subscription'}
    if not company.next_billing_date or company.next_billing_date > today:
        return {'charged': False, 'reason': 'not due yet'}
    if not company.paystack_authorization_code:
        return {'charged': False, 'reason': 'no card on file'}

    # Idempotency: never bill the same cycle twice, even if the cron runs
    # more than once on the due date.
    already = BillingTransaction.objects.filter(
        company=company, plan='pro', status='complete', created_at__date=today,
    ).exists()
    if already:
        return {'charged': False, 'reason': 'already billed this cycle'}

    txn = BillingTransaction.objects.create(
        company=company, amount=MONTHLY_FEE, payment_id='', status='pending', plan='pro',
    )
    record_charge_attempt(company)
    result = charge_authorization(
        company.paystack_authorization_code, company.paystack_authorization_email,
        MONTHLY_FEE, metadata={'company_id': company.id, 'kind': 'monthly_fee'},
    )
    txn.raw_gateway_response = result.get('raw') or {}

    if result['success']:
        txn.status = 'complete'
        txn.payment_status = 'success'
        txn.gateway_transaction_id = str((result['data'] or {}).get('id', ''))
        txn.save(update_fields=['status', 'payment_status', 'gateway_transaction_id', 'raw_gateway_response', 'updated_at'])
        company.next_billing_date = add_one_month(company.next_billing_date)
        company.next_billing_at = billing_at_for_date(company.next_billing_date)
        company.save(update_fields=['next_billing_date', 'next_billing_at', 'updated_at'])
        record_charge_success(company)
        title = 'Subscription fee charged'
        message = f'{MONTHLY_FEE_ITEM_NAME}: R{MONTHLY_FEE:,.2f} charged successfully.'
        notify_company(company.id, 'SUCCESS', title, message, link='/settings/billing', event='subscription.charged')
        notify_company_billing_email(company.id, title, message, link='/settings/billing')
        return {'charged': True}

    txn.status = 'failed'
    txn.payment_status = 'failed'
    txn.save(update_fields=['status', 'payment_status', 'raw_gateway_response', 'updated_at'])
    entered_grace = record_charge_failure(company)
    if entered_grace:
        grace_deadline = company.grace_period_expires_at
        title = 'Could not charge subscription fee'
        message = (
            f"We couldn't charge your {MONTHLY_FEE_ITEM_NAME} subscription (R{MONTHLY_FEE:,.2f}). "
            f"You have until {grace_deadline.strftime('%d %b %Y')} to update your card before your "
            "account is suspended."
        )
        notify_company(company.id, 'ALERT', title, message, link='/settings/billing', event='subscription.failed')
        notify_company_billing_email(company.id, title, message, link='/settings/billing')
    return {'charged': False, 'reason': result['error']}


def run_monthly_subscription_billing() -> dict:
    """Daily sweep (Celery Beat): charge every active-or-grace-period company
    whose next_billing_date has arrived. Returns a summary dict for the
    management command."""
    from core.models import Company

    today = timezone.now().date()
    summary = {'checked': 0, 'charged': 0, 'failed': 0, 'skipped': 0}

    companies = Company.objects.filter(
        subscription_status__in=['active', 'grace_period'],
        next_billing_date__lte=today,
        next_billing_date__isnull=False,
    )
    for company in companies:
        summary['checked'] += 1
        result = charge_monthly_subscription_fee(company)
        if result['charged']:
            summary['charged'] += 1
        elif result['reason'] in ('no card on file',):
            summary['skipped'] += 1
        else:
            summary['failed'] += 1

    return summary
