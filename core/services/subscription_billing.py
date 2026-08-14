"""The flat R4,499/month subscription fee — charged ad-hoc via
charge_authorization against the company's Paystack card-on-file, every 30
days from their first successful payment (a fixed cycle length, not a
calendar-month anniversary — see add_billing_cycle for why).

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
import logging
from datetime import date, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


def _grace_days() -> int:
    from django.conf import settings
    return int(getattr(settings, 'DELIVERY_FEE_GRACE_DAYS', 7))


def _test_mode() -> bool:
    """Testing-only: compresses the ~monthly billing cycle down to a few
    minutes (settings.SUBSCRIPTION_TEST_MODE) so subscribe -> auto-recharge
    and cancel -> auto-finalize can be watched end-to-end without waiting
    weeks. NEVER on in production — every function below that checks this
    only adds a new branch; the default (off) path is untouched production
    logic, unchanged from before test mode existed."""
    from django.conf import settings
    return bool(getattr(settings, 'SUBSCRIPTION_TEST_MODE', False))


def _test_cycle_minutes() -> int:
    from django.conf import settings
    return int(getattr(settings, 'SUBSCRIPTION_TEST_CYCLE_MINUTES', 5))


def add_billing_cycle(d: date) -> date:
    """d 30 days later — a fixed-length cycle rather than 'same day next
    calendar month', so the countdown a customer sees is always exactly 30
    days regardless of which months it spans (a calendar-month step varies
    28-31 days depending on the months involved — e.g. from a 31-day month
    it reads as 31 days, from February as 28)."""
    return d + timedelta(days=30)


def billing_at_for_date(d: date):
    """The exact datetime a given next_billing_date will actually be
    attempted — 07:00 SAST, matching run_monthly_subscription_billing's
    Celery Beat schedule. Feeds Company.next_billing_at, which exists purely
    for the billing page's live countdown — charging itself still keys off
    next_billing_date (a plain date), unaffected by this."""
    from datetime import datetime, time as dt_time
    return timezone.make_aware(datetime.combine(d, dt_time(7, 0)))


def compute_next_cycle(previous_next_billing_date):
    """Returns (next_billing_date, next_billing_at) for the cycle after
    previous_next_billing_date (None on first activation, in which case it's
    based off today instead). Normally 30 days out; in test mode,
    SUBSCRIPTION_TEST_CYCLE_MINUTES minutes from right now instead — a
    DateField can't represent sub-day precision, so test mode is the one
    case where next_billing_at (not next_billing_date) is what actually
    governs due-ness; see _is_due below."""
    if _test_mode():
        next_at = timezone.now() + timezone.timedelta(minutes=_test_cycle_minutes())
        return next_at.date(), next_at
    base = previous_next_billing_date or timezone.now().date()
    next_date = add_billing_cycle(base)
    return next_date, billing_at_for_date(next_date)


def _due_at(company):
    """Precise fallback datetime a company's next charge is due — used only
    by the test-mode branches below (production stays on the coarser,
    unchanged next_billing_date/today date comparison)."""
    if company.next_billing_at:
        return company.next_billing_at
    if company.next_billing_date:
        return billing_at_for_date(company.next_billing_date)
    return None


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


def check_pending_cancellations() -> dict:
    """Daily sweep (Celery Beat): finalise any company that requested
    cancellation while still active/grace_period once the period they already
    paid for actually runs out — see CancelSubscriptionView's docstring for
    why the flip isn't immediate. Until this runs, cancel_at_period_end
    companies keep full access exactly like any other active/grace_period
    company (run_monthly_subscription_billing already skips charging them
    again — see its own filter)."""
    from core.models import Company
    from core.services.notify import notify_company, notify_company_billing_email

    today = timezone.now().date()
    summary = {'checked': 0, 'cancelled': 0}

    pending = Company.objects.filter(
        cancel_at_period_end=True,
        subscription_status__in=['active', 'grace_period'],
        next_billing_date__isnull=False,
    )
    for company in pending:
        summary['checked'] += 1
        if _test_mode():
            due_at = _due_at(company)
            if due_at and due_at > timezone.now():
                continue
        elif company.next_billing_date > today:
            continue

        company.subscription_status = 'cancelled'
        company.cancel_at_period_end = False
        company.next_billing_date = None
        company.grace_period_expires_at = None
        company.save(update_fields=[
            'subscription_status', 'cancel_at_period_end',
            'next_billing_date', 'grace_period_expires_at', 'updated_at',
        ])
        summary['cancelled'] += 1

        title = 'Subscription ended'
        message = (
            'Your subscription period has ended as scheduled — quoting and invoicing are now blocked. '
            'You can resubscribe any time from Settings → Billing.'
        )
        notify_company(company.id, 'INFO', title, message, link='/settings/billing', event='subscription.cancelled')
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
    now = timezone.now()

    if company.subscription_status not in ('active', 'grace_period'):
        return {'charged': False, 'reason': 'not an active/grace-period subscription'}
    if company.cancel_at_period_end:
        return {'charged': False, 'reason': 'cancelling at period end — not billed again'}

    if _test_mode():
        # A DateField can't represent "due in 5 minutes" — use the precise
        # datetime instead. Production is untouched below (the elif).
        due_at = _due_at(company)
        if not due_at or due_at > now:
            return {'charged': False, 'reason': 'not due yet'}
    elif not company.next_billing_date or company.next_billing_date > today:
        return {'charged': False, 'reason': 'not due yet'}

    if not company.paystack_authorization_code:
        return {'charged': False, 'reason': 'no card on file'}

    # Idempotency: never bill the same cycle twice, even if the cron runs
    # more than once on the due date. Production dedups per calendar day
    # (a whole month's cycle, so this is generous on purpose — see
    # docstring). Test mode's cycle is itself only minutes long — a window
    # anywhere near that length would wrongly block the NEXT genuine cycle
    # too, since next_billing_at is only ever a few minutes out. This just
    # needs to catch the cron firing twice for the same due moment (e.g.
    # overlapping ticks), so a short fixed window is enough — the real
    # protection against double-charging is next_billing_at itself already
    # having been advanced past "now" by the time any later tick checks it.
    if _test_mode():
        already = BillingTransaction.objects.filter(
            company=company, plan='pro', status='complete',
            created_at__gt=now - timezone.timedelta(seconds=10),
        ).exists()
    else:
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
        company.next_billing_date, company.next_billing_at = compute_next_cycle(company.next_billing_date)
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
        cancel_at_period_end=False,
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
