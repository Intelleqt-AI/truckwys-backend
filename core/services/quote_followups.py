"""Expiry and follow-up nudges for sent quotes, and the customer reminder.

Daily (beat 08:00 SAST), per company with follow-ups on:
  - SENT quotes expiring within `expiry_nudge_days` (default 2):
      "Quote Q-123 for Acme expires on Fri."
  - SENT quotes with no answer `follow_up_after_days` (default 3) after they
    were sent:
      "No answer from Acme on Q-123 (sent 3 days ago). Follow up?"
    with a one-click "send reminder email to customer" action (preview first;
    the customer is only emailed when a user confirms).
Each stage nudges once per send cycle (QuoteFollowUp; the expiry stage once
per valid_until date). A quote that leaves SENT is never nudged; sending it
again starts a new cycle. Bell rows always; push per the user's
'quote_reminders' setting; no email for nudges (the bell/push is the nudge).
"""
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
SAST = ZoneInfo('Africa/Johannesburg')
REMINDER_MIN_HOURS = 24
NOTE_MAX = 500
_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def _sast_date(dt):
    return dt.astimezone(SAST).date()


def _expiry_phrase(valid_until, today):
    days = (valid_until - today).days
    if days <= 0:
        return 'today'
    if days == 1:
        return 'tomorrow'
    if days < 7:
        return f'on {_DAYS[valid_until.weekday()]}'
    return f'on {_DAYS[valid_until.weekday()]} {valid_until.day} {_MONTHS[valid_until.month - 1]}'


def _long_date(d):
    return f'{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}'


def _customer(quote):
    return (getattr(quote.customer, 'name', '') or 'your customer').strip()


def _ident(quote):
    return quote.quote_number or f'Quote {quote.id}'


def follow_up_for(quote, now=None):
    """The quote's QuoteFollowUp; created for a SENT quote that has none (sent
    before this feature, or by a path without signals) with sent_at estimated
    from updated_at — later than or equal to the real send, so a nudge is
    never early."""
    from core.models import QuoteFollowUp
    try:
        return quote.follow_up
    except QuoteFollowUp.DoesNotExist:
        pass
    defaults = {}
    if quote.status == 'SENT':
        defaults = {'sent_at': quote.updated_at or now or timezone.now(),
                    'sent_at_source': QuoteFollowUp.SENT_AT_ESTIMATED}
    fu, _ = QuoteFollowUp.objects.get_or_create(quote=quote, defaults=defaults)
    quote.follow_up = fu
    return fu


def record_sent(quote, now=None):
    """A new send cycle: stamp sent_at, clear the nudges (signal hook)."""
    from core.models import QuoteFollowUp
    now = now or timezone.now()
    QuoteFollowUp.objects.update_or_create(quote=quote, defaults={
        'sent_at': now, 'sent_at_source': QuoteFollowUp.SENT_AT_RECORDED,
        'expiry_nudged_at': None, 'expiry_nudged_for': None, 'no_answer_nudged_at': None,
    })


def days_since_sent(fu, today):
    if fu.sent_at is None:
        return None
    return (today - _sast_date(fu.sent_at)).days


def sweep_quote_nudges(now=None):
    """Run the daily nudges. Idempotent; returns a summary dict."""
    from core.models import Quote, QuoteFollowUp
    from core.services.notify import notify_company
    from core.services.quote_automation import get_settings
    now = now or timezone.now()
    today = _sast_date(now)
    summary = {'expiry': 0, 'no_answer': 0, 'combined': 0}
    settings_cache = {}
    qs = (Quote.objects.filter(status='SENT', company__isnull=False, company__is_deleted=False)
          .select_related('customer', 'company').order_by('id'))
    for quote in qs.iterator():
        s = settings_cache.get(quote.company_id)
        if s is None:
            s = settings_cache[quote.company_id] = get_settings(quote.company)
        if not s.follow_ups_enabled:
            continue
        try:
            fu = follow_up_for(quote, now)
            vu = quote.valid_until
            expiry_due = (vu is not None and today <= vu <= today + timedelta(days=s.expiry_nudge_days)
                          and fu.expiry_nudged_for != vu)
            age = days_since_sent(fu, today)
            no_answer_due = (fu.no_answer_nudged_at is None and age is not None
                             and age >= s.follow_up_after_days)
            if not (expiry_due or no_answer_due):
                continue
            # Claim the stage(s) first (a concurrent run claims nothing twice).
            with transaction.atomic():
                rows = QuoteFollowUp.objects.select_for_update().filter(pk=fu.pk)
                cur = rows.first()
                if cur is None:
                    continue
                expiry_due = expiry_due and cur.expiry_nudged_for != vu
                no_answer_due = no_answer_due and cur.no_answer_nudged_at is None
                upd = {}
                if expiry_due:
                    upd.update(expiry_nudged_at=now, expiry_nudged_for=vu)
                if no_answer_due:
                    upd['no_answer_nudged_at'] = now
                if upd:
                    rows.update(**upd)
            if not (expiry_due or no_answer_due):
                continue
            ident, cust = _ident(quote), _customer(quote)
            link = f'/bookings/quotes/{quote.id}?follow_up=1'
            if no_answer_due:
                sent = f"sent {age} day{'s' if age != 1 else ''} ago"
                msg = f'No answer from {cust} on {ident} ({sent}).'
                if expiry_due:
                    msg += f' It expires {_expiry_phrase(vu, today)}.'
                msg += ' Follow up?'
                notify_company(quote.company_id, 'WARNING' if expiry_due else 'INFO',
                               'No answer yet', msg, link=link, event='quote.no_answer')
                summary['combined' if expiry_due else 'no_answer'] += 1
            else:
                msg = f'Quote {ident} for {cust} expires {_expiry_phrase(vu, today)}.'
                notify_company(quote.company_id, 'WARNING', 'Quote expiring soon', msg, link=link,
                               event='quote.expiring')
                summary['expiry'] += 1
        except Exception:
            logger.exception('quote nudge failed for quote %s', quote.pk)
    return summary


# ---------------------------------------------------------------------------
# Reminder email to the customer (explicit user action only)
# ---------------------------------------------------------------------------

def follow_up_state(quote, now=None):
    now = now or timezone.now()
    today = _sast_date(now)
    from core.models import QuoteFollowUp
    if quote.status == 'SENT':
        fu = follow_up_for(quote, now)
    else:
        fu = QuoteFollowUp.objects.filter(quote=quote).first()
    from core.services.quote_automation import _iso
    can, reason = reminder_allowed(quote, fu, now)
    return {
        'quote_id': quote.id, 'status': quote.status,
        'sent_at': _iso(fu.sent_at) if fu else None,
        'sent_at_estimated': bool(fu and fu.sent_at_source == 'estimated'),
        'days_since_sent': days_since_sent(fu, today) if fu else None,
        'valid_until': quote.valid_until.isoformat() if quote.valid_until else None,
        'expires_in_days': (quote.valid_until - today).days if quote.valid_until else None,
        'expiry_nudged_at': _iso(fu.expiry_nudged_at) if fu else None,
        'no_answer_nudged_at': _iso(fu.no_answer_nudged_at) if fu else None,
        'reminder': {
            'last_sent_at': _iso(fu.reminder_sent_at) if fu else None,
            'count': fu.reminder_count if fu else 0,
            'last_sent_to': (fu.reminder_last_to or None) if fu else None,
            'can_send': can, 'reason': reason,
        },
    }


REASON_TEXT = {
    'not_sent': 'Only a sent quote can get a reminder.',
    'expired': 'This quote has expired. Extend it before sending a reminder.',
    'no_customer_email': 'This customer has no email address on file.',
    'too_soon': 'A reminder went out less than a day ago.',
    'demo': 'The demo account never emails customers.',
}


def reminder_allowed(quote, fu=None, now=None):
    now = now or timezone.now()
    if getattr(quote.company, 'is_demo', False):
        return False, 'demo'            # the demo account never emails customers
    if quote.status != 'SENT':
        return False, 'not_sent'
    if quote.valid_until and quote.valid_until < _sast_date(now):
        return False, 'expired'
    if not (quote.customer and (quote.customer.email or '').strip()):
        return False, 'no_customer_email'
    if fu is not None and fu.reminder_sent_at and now - fu.reminder_sent_at < timedelta(hours=REMINDER_MIN_HOURS):
        return False, 'too_soon'
    return True, None


def reminder_email(quote, user, note=''):
    """(subject, text, html, to, reply_to) for the customer reminder."""
    from core.services import followup_emails as fe
    from core.services.quote_share import quote_share_url
    company = quote.company
    company_name = (getattr(company, 'company_name', '') or 'us').strip()
    cust = getattr(quote.customer, 'name', '') or ''
    sender = (user.get_full_name() or user.first_name or '').strip() if user else ''
    url = quote_share_url(quote)
    route = ' to '.join(p for p in (quote.pickup_location, quote.delivery_location) if p)
    valid = f' It is valid until {_long_date(quote.valid_until)}.' if quote.valid_until else ''
    note = (note or '').strip()[:NOTE_MAX]
    subject = f'Reminder: quote {_ident(quote)} from {company_name}'
    greeting = f'Hi {cust},' if cust else 'Hi,'
    body = (f'Just a reminder about our quote {_ident(quote)}' + (f' for {route}' if route else '') + '.' + valid
            + ' You can view it and accept or decline it here:')
    sign = '\n'.join(p for p in ('Thanks,', sender, company_name) if p)
    text = '\n\n'.join(p for p in (greeting, note, body, url, sign) if p)
    html = fe.page(
        f'Quote {_ident(quote)}',
        fe.para(fe.esc(greeting)) + (fe.para(fe.esc(note).replace('\n', '<br>')) if note else '')
        + fe.para(fe.esc(body)),
        fe.button(url, 'View quote') + fe.para(fe.esc(sign).replace('\n', '<br>')),
        f'Sent by {company_name} using TruckWys.')
    to = (quote.customer.email or '').strip() if quote.customer else ''
    reply_to = (user.email or '').strip() if user else ''
    return {'to': to, 'reply_to': reply_to or None, 'subject': subject, 'text': text, 'html': html}


def send_reminder(quote, user, note='', now=None):
    """Email the reminder (explicit user action). Returns (ok, reason, state)."""
    from core.models import QuoteFollowUp
    from core.services import followup_emails as fe
    now = now or timezone.now()
    with transaction.atomic():
        fu = follow_up_for(quote, now)
        fu = QuoteFollowUp.objects.select_for_update().get(pk=fu.pk)
        ok, reason = reminder_allowed(quote, fu, now)
        if not ok:
            return False, reason, None
        mail = reminder_email(quote, user, note)
        if getattr(quote.company, 'is_demo', False):
            return False, 'demo', None
        if not fe.deliver(mail['to'], mail['subject'], mail['html'], mail['text'], reply_to=mail['reply_to']):
            return False, 'send_failed', None
        QuoteFollowUp.objects.filter(pk=fu.pk).update(
            reminder_sent_at=now, reminder_count=fu.reminder_count + 1,
            reminder_last_to=mail['to'][:254], reminder_last_by=user)
    try:
        quote.follow_up.refresh_from_db()
    except Exception:
        pass
    return True, None, mail
