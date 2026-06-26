"""Collections / dunning — actually chase the money.

Turns the billing short-pay audit from advisory into action: sends real,
escalating payment reminders (gentle → firm → final) via Resend, throttled so a
customer is never spammed, and tracked on the invoice. A dunning sweep scans all
overdue/short-paid invoices for a company and reminds the ones that are due.
"""
import logging
from datetime import date

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

OUTSTANDING_STATUSES = ['SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID']


def _resend_enabled() -> bool:
    return bool(getattr(settings, 'RESEND_API_KEY', ''))


def _tone_for(days_overdue: int, reminder_count: int) -> str:
    if days_overdue > 30 or reminder_count >= 3:
        return 'final'
    if days_overdue > 0 or reminder_count >= 1:
        return 'firm'
    return 'gentle'


def send_payment_reminder(invoice, *, company=None) -> dict:
    """Send one payment reminder for an invoice. Never raises.

    Returns {sent, reason?, tone?, recipient?, reminder_count?}.
    """
    if invoice.status in ('PAID', 'CANCELLED'):
        return {'sent': False, 'reason': 'invoice is not outstanding'}

    balance = invoice.balance if invoice.balance is not None else invoice.total_amount
    if not balance or balance <= 0:
        return {'sent': False, 'reason': 'nothing outstanding'}

    customer = getattr(invoice, 'customer', None)
    recipient = getattr(customer, 'email', None)
    if not recipient:
        return {'sent': False, 'reason': 'customer has no email on file'}

    days_overdue = (date.today() - invoice.due_date).days if invoice.due_date else 0
    days_overdue = max(days_overdue, 0)
    tone = _tone_for(days_overdue, invoice.reminder_count or 0)

    if not _resend_enabled():
        return {'sent': False, 'reason': 'email not configured (set RESEND_API_KEY)',
                'tone': tone, 'recipient': recipient}

    # Ensure a public view token exists so the email link works
    if not getattr(invoice, 'view_token', None):
        import secrets
        invoice.view_token = secrets.token_urlsafe(32)
        invoice.save(update_fields=['view_token'])

    company = company or getattr(invoice, 'company', None)
    try:
        from core.services.resend_email import send_payment_reminder_email
        send_payment_reminder_email(invoice, company, tone=tone, days_overdue=days_overdue)
    except Exception as exc:
        logger.warning('payment reminder send failed for %s: %s', invoice.invoice_number, exc)
        return {'sent': False, 'reason': f'send failed: {exc}', 'tone': tone, 'recipient': recipient}

    invoice.last_reminder_at = timezone.now()
    invoice.reminder_count = (invoice.reminder_count or 0) + 1
    invoice.save(update_fields=['last_reminder_at', 'reminder_count'])

    return {'sent': True, 'tone': tone, 'recipient': recipient,
            'reminder_count': invoice.reminder_count, 'days_overdue': days_overdue}


def run_dunning(company=None, *, throttle_days: int = 3, limit: int = 200) -> dict:
    """Scan overdue/short-paid invoices and send due reminders. Never raises.

    Throttled: an invoice reminded within `throttle_days` is skipped so customers
    aren't spammed. Tone escalates automatically with age + prior reminder count.
    """
    from core.models import Invoice

    summary = {'scanned': 0, 'sent': 0, 'skipped_throttled': 0,
               'skipped_no_email': 0, 'failed': 0, 'reminders': []}

    # Don't iterate (or report false failures) when email isn't configured.
    if not _resend_enabled():
        summary['disabled'] = True
        summary['reason'] = 'email not configured (set RESEND_API_KEY)'
        return summary

    try:
        today = date.today()
        qs = Invoice.objects.filter(
            status__in=OUTSTANDING_STATUSES,
            balance__gt=0,
            due_date__lt=today,
        ).select_related('customer', 'company')
        if company is not None:
            qs = qs.filter(company=company)
        qs = qs.order_by('due_date')[:limit]

        cutoff = timezone.now() - timezone.timedelta(days=throttle_days)
        for inv in qs:
            summary['scanned'] += 1
            if inv.last_reminder_at and inv.last_reminder_at > cutoff:
                summary['skipped_throttled'] += 1
                continue
            result = send_payment_reminder(inv, company=inv.company)
            if result['sent']:
                summary['sent'] += 1
                summary['reminders'].append({
                    'invoice_number': inv.invoice_number,
                    'tone': result['tone'],
                    'amount': float(inv.balance or 0),
                })
            elif 'no email' in result['reason']:
                summary['skipped_no_email'] += 1
            else:
                summary['failed'] += 1
    except Exception as exc:
        logger.warning('run_dunning failed: %s', exc)
        summary['error'] = str(exc)
    return summary
