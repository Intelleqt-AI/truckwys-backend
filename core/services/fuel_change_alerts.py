"""Fuel change alert: when a new official fuel price period starts (first
Wednesday 00:01 SAST) or a new official price is stored, tell each company
how its open quotes are affected.

    "Diesel up R 3,24/L today. 6 open quotes are now under your 10% target.
     Re-price them?"

Open quotes = DRAFT or SENT, valid_until today or later (SAST), priced before
the new price took effect, on the same fuel (diesel / petrol 95 / petrol 93)
and zone. Each quote's old vs new cost floor and margin come from
quote_costing.changes_since_priced (the reopen notice), so the alert and the
quote screen never disagree. One alert per company per period per fuel
(FuelChangeAlert unique constraint). Bell rows always; push / email per the
user's notification settings (push 'quote_reminders', email 'fuel_alerts');
the company switch QuoteAutomationSettings.fuel_alerts_enabled turns it off.
"""
import logging
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
SAST = ZoneInfo('Africa/Johannesburg')
PRODUCTS = ('diesel', 'petrol_95', 'petrol_93')
ZONES = ('INLAND', 'COASTAL')
EMAIL_LIST_LIMIT = 25
_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def fuel_word(product):
    return 'Diesel' if product == 'diesel' else f"Petrol {product.split('_')[1]}"


def price_changes(now=None):
    """{(product, zone): {'old', 'new', 'delta', 'effective_from', 'period_start'}}
    for every official price that changed at the start of the current period
    (the price in force now took effect this period and differs from the one
    before it by at least half a cent)."""
    from core.services.fuel_price import period_start, price_in_force
    now = now or timezone.now()
    start = period_start(now)
    out = {}
    for product in PRODUCTS:
        for zone in ZONES:
            if product == 'petrol_93' and zone == 'COASTAL':
                continue
            cur = price_in_force(zone, now, product=product)
            if cur is None or cur['effective_from'] < start:
                continue
            prev = price_in_force(zone, cur['effective_from'] - timedelta(seconds=1), product=product)
            if prev is None:
                continue
            delta = round(cur['price'] - prev['price'], 4)
            if abs(delta) < 0.005:
                continue
            out[(product, zone)] = {'old': prev['price'], 'new': cur['price'], 'delta': delta,
                                    'effective_from': cur['effective_from'], 'period_start': start}
    return out


def _quote_product(quote):
    from core.services.quote_snapshot import quote_fuel_product
    product = quote_fuel_product(quote)
    if product.startswith('petrol') and (quote.fuel_zone or '').upper() == 'COASTAL':
        product = 'petrol_95'
    return product


def _day_phrase(effective_from, now):
    d = effective_from.astimezone(SAST).date()
    if d == now.astimezone(SAST).date():
        return 'today'
    return f'on {_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}'


def _pct(v):
    from core.services.quote_costing import fmt_num
    v = float(v)
    return f'{fmt_num(v, 0 if v == int(v) else 1)}%'


def compose(product, change, affected, under, target, now):
    """(title, message) for the bell / push / email subject line."""
    from core.services.quote_costing import fmt_rand
    word = fuel_word(product)
    up = change['delta'] > 0
    head = f"{word} {'up' if up else 'down'} {fmt_rand(abs(change['delta']), 2)}/L {_day_phrase(change['effective_from'], now)}."
    title = f"{word} price {'up' if up else 'down'}"
    n = len(affected)
    if up and under:
        tail = (f" {under} open quote{'s are' if under != 1 else ' is'} now under your {_pct(target)} target. "
                f"Re-price {'them' if under != 1 else 'it'}?")
    elif up:
        tail = (f" {n} open quote{'s' if n != 1 else ''} cost{'' if n != 1 else 's'} more to run but "
                f"{'all still meet' if n != 1 else 'still meets'} your {_pct(target)} target.")
    else:
        tail = f" {n} open quote{'s' if n != 1 else ''} now cost{'' if n != 1 else 's'} less to run."
    return title, head + tail


def affected_quotes(company, product, changes, now):
    """Open quotes on `product` priced before the change, with old vs new
    floor and margin. Sorted: under target first, then the biggest margin drop."""
    from core.models import Quote
    from core.services.quote_costing import changes_since_priced, costing_for_quote, target_margin
    today = now.astimezone(SAST).date()
    target = target_margin(company)
    rows = []
    qs = (Quote.objects.filter(company=company, status__in=('DRAFT', 'SENT'), valid_until__gte=today,
                               priced_at__isnull=False, cost_floor__isnull=False)
          .select_related('customer', 'priced_vehicle_type', 'company'))
    for q in qs.iterator():
        zone = (q.fuel_zone or company.fuel_zone or 'INLAND').upper()
        if _quote_product(q) != product:
            continue
        change = changes.get((product, zone))
        if change is None or q.priced_at >= change['effective_from']:
            continue
        try:
            costing = costing_for_quote(q, now)
        except Exception:
            logger.exception('fuel alert: costing quote %s failed', q.pk)
            continue
        cs = changes_since_priced(q.total_amount, q.cost_floor, costing['floor'], q.priced_at)
        if not cs['changed'] or cs['margin_now'] is None:
            continue
        rows.append({
            'quote_id': q.id, 'quote_number': q.quote_number,
            'customer': getattr(q.customer, 'name', '') or '',
            'status': q.status, 'valid_until': q.valid_until.isoformat(),
            'price': cs['price'], 'floor_then': cs['floor_then'], 'floor_now': cs['floor_now'],
            'delta_zar': cs['delta_zar'],
            'margin_then': round(cs['margin_then'], 2) if cs['margin_then'] is not None else None,
            'margin_now': round(cs['margin_now'], 2),
            'under_target': cs['margin_now'] < target,
            'repriced_price_keep_margin': cs['repriced_price_keep_margin'],
            'zone': zone, 'link': f'/bookings/quotes/{q.id}',
        })
    rows.sort(key=lambda r: (not r['under_target'], (r['margin_now'] or 0) - (r['margin_then'] or 0)))
    return rows, target


def run_fuel_change_alerts(now=None, *, refresh=False):
    """Create and deliver this period's alerts (idempotent). refresh=True
    first makes sure the current official price is stored (network: FIASA),
    as the beat task does. Returns a summary dict."""
    from core.models import Company, FuelChangeAlert, Quote
    from core.services.quote_automation import get_settings
    now = now or timezone.now()
    if refresh:
        try:
            from core.services.fuel_price import refresh_official
            refresh_official(now=now)
        except Exception as exc:
            logger.warning('fuel alert: refresh failed: %s', exc)
    changes = price_changes(now)
    summary = {'changes': len(changes), 'alerts': 0, 'notified': 0, 'skipped_existing': 0}
    if not changes:
        return summary
    today = now.astimezone(SAST).date()
    company_ids = (Quote.objects.filter(status__in=('DRAFT', 'SENT'), valid_until__gte=today,
                                        priced_at__isnull=False, company__isnull=False)
                   .values_list('company_id', flat=True).distinct())
    products = sorted({p for p, _ in changes})
    for company in Company.objects.filter(id__in=list(company_ids), is_deleted=False):
        s = get_settings(company)
        if not s.fuel_alerts_enabled:
            continue
        for product in products:
            period = next(c['period_start'] for (p, _), c in changes.items() if p == product)
            period_date = period.astimezone(SAST).date()
            if FuelChangeAlert.objects.filter(company=company, period_start=period_date, product=product).exists():
                summary['skipped_existing'] += 1
                continue
            try:
                if _alert_company(company, product, changes, period_date, now):
                    summary['notified'] += 1
                summary['alerts'] += 1
            except IntegrityError:
                summary['skipped_existing'] += 1
            except Exception:
                logger.exception('fuel alert failed for company %s', company.pk)
    return summary


def _alert_company(company, product, changes, period_date, now):
    from core.models import FuelChangeAlert
    from core.services.notify import notify_company
    rows, target = affected_quotes(company, product, changes, now)
    zone = (company.fuel_zone or 'INLAND').upper()
    change = changes.get((product, zone)) or next(c for (p, _), c in changes.items() if p == product)
    if rows:
        zone = rows[0]['zone']
        change = changes.get((product, zone), change)
    under = sum(1 for r in rows if r['under_target'])
    title, message = compose(product, change, rows, under, target, now) if rows else ('', '')
    with transaction.atomic():
        alert = FuelChangeAlert.objects.create(
            company=company, period_start=period_date, product=product, zone=zone,
            old_price=Decimal(str(change['old'])), new_price=Decimal(str(change['new'])),
            effective_from=change['effective_from'], target_margin_pct=Decimal(str(target)),
            quotes=rows, quotes_affected=len(rows), quotes_under_target=under,
            title=title, message=message)
    if not rows:
        return False
    notify_company(company.id, 'WARNING' if under else 'INFO', title, message,
                   link=f'/bookings/quotes?fuel_alert={alert.id}', event='quote.fuel_alert')
    sent = _email(company, alert, product, change)
    FuelChangeAlert.objects.filter(pk=alert.pk).update(notified_at=now, emails_sent=sent)
    return True


def _email(company, alert, product, change):
    from core.models import User
    from core.services import followup_emails as fe
    from core.services.notification_prefs import should_notify
    from core.services.quote_costing import fmt_rand
    users = [u for u in User.objects.filter(company=company, is_active=True)
             if u.email and should_notify(u, 'email', 'fuel_alerts')]
    if not users:
        return 0
    base = fe.frontend_url()
    rows = alert.quotes[:EMAIL_LIST_LIMIT]
    headers = ['Quote', 'Customer', 'Price', 'Cost floor', 'Margin', '']
    align = ['left', 'left', 'right', 'right', 'right', 'left']

    def m(v):
        from core.services.quote_costing import fmt_num
        return f'{fmt_num(v, 1)}%' if v is not None else '—'
    html_rows, text_rows = [], []
    for r in rows:
        floor = f"{fmt_rand(r['floor_then'], 0)} → {fmt_rand(r['floor_now'], 0)}"
        margin = f"{m(r['margin_then'])} → {m(r['margin_now'])}"
        flag = 'Under target' if r['under_target'] else ''
        link = f'<a href="{fe.esc(base + r["link"])}" style="color:#2563EB;">{fe.esc(r["quote_number"])}</a>'
        html_rows.append([link, fe.esc(r['customer']), fmt_rand(r['price'], 0), floor, margin, flag])
        text_rows.append([r['quote_number'], r['customer'], fmt_rand(r['price'], 0), floor, margin, flag])
    more = len(alert.quotes) - len(rows)
    old_new = f"{fmt_rand(change['old'], 2)} → {fmt_rand(change['new'], 2)}/L (official {alert.zone.lower()})"
    intro = fe.para(fe.esc(alert.message)) + fe.para(fe.esc(old_new), muted=True)
    sections = fe.table(headers, html_rows, align)
    if more > 0:
        sections += fe.para(f'And {more} more in TruckWys.', muted=True)
    sections += fe.button(f"{base}/bookings/quotes?fuel_alert={alert.id}", 'Review quotes')
    footer = ('You get this because fuel price alerts are on in Settings → Notifications. '
              'Margins are on the quoted price; cost floors are excl. VAT.')
    text = '\n\n'.join(filter(None, [
        alert.message, old_new, fe.text_table(headers[:-1] + ['Note'], text_rows),
        f'And {more} more in TruckWys.' if more > 0 else '',
        f"Review quotes: {base}/bookings/quotes?fuel_alert={alert.id}", footer]))
    html = fe.page(alert.title, intro, sections, footer)
    sent = 0
    for u in users:
        if fe.deliver(u.email, f'{alert.title}: {alert.quotes_affected} open quote'
                      f"{'s' if alert.quotes_affected != 1 else ''} affected", html, text):
            sent += 1
    return sent


def alert_out(alert, include_quotes=True):
    from core.services.quote_automation import _iso
    out = {
        'id': alert.id, 'product': alert.product, 'fuel': fuel_word(alert.product), 'zone': alert.zone,
        'period_start': alert.period_start.isoformat(), 'effective_from': _iso(alert.effective_from),
        'old_price': float(alert.old_price), 'new_price': float(alert.new_price),
        'delta': round(float(alert.new_price - alert.old_price), 4),
        'target_margin_pct': float(alert.target_margin_pct),
        'quotes_affected': alert.quotes_affected, 'quotes_under_target': alert.quotes_under_target,
        'title': alert.title, 'message': alert.message, 'notified_at': _iso(alert.notified_at),
        'created_at': _iso(alert.created_at),
    }
    if include_quotes:
        from core.models import Quote
        live = dict(Quote.objects.filter(id__in=[r['quote_id'] for r in alert.quotes])
                    .values_list('id', 'status'))
        out['quotes'] = [{**r, 'status_now': live.get(r['quote_id']),
                          'still_open': live.get(r['quote_id']) in ('DRAFT', 'SENT')} for r in alert.quotes]
    return out
