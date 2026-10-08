"""Weekly margin review: quoted vs actual margin by lane and by customer.

ONE service function, weekly_margin_figures(company, as_of), feeds both the
Monday email and GET /api/v1/reports/weekly-margin/. Actuals come from
core.services.reports.load_economics (read-only), so when the trip-economics
work improves that function (fuel, tolls, driver from real trip data) this
report improves with it, with no change here.

Honesty rules (no invented numbers):
  - Quoted margin = (quote price − stored cost floor) / quote price, from the
    quote the load was made from. Loads without a quote or a floor are counted
    but carry no quoted margin.
  - Actual margin = revenue − ACTUAL cost (recorded expenses), only for loads
    whose cost basis is 'actual'. load_economics' modelled cost estimate is
    never presented as an actual; those loads count as "no actual costs yet".
    Revenue is the invoiced amount excl. VAT, else the load price (flagged
    revenue_basis 'estimate').
  - Every block says how many loads it rests on; with none, `enough_data` is
    false and clients/email show "Not enough data yet".
Week = Monday 00:00 to Sunday 24:00 SAST; "4 weeks" = the 28 days ending
with that Sunday. A load belongs to the week it was delivered
(actual_delivered_at, else delivery_date).
"""
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)
SAST = ZoneInfo('Africa/Johannesburg')
DONE_STATUSES = ('DELIVERED', 'INVOICED')
WORST_LANES = 3
TOP_ROWS = 10


def last_full_week(as_of=None):
    """(monday, sunday) dates of the last full SAST week before as_of."""
    as_of = as_of or timezone.now()
    today = as_of.astimezone(SAST).date()
    this_monday = today - timedelta(days=today.weekday())
    monday = this_monday - timedelta(days=7)
    return monday, monday + timedelta(days=6)


def _start(d):
    return datetime.combine(d, time(0, 0), tzinfo=SAST)


def _pct(margin, revenue):
    return round(margin / revenue * 100, 1) if revenue else None


def _lane_of(load):
    o = (load.pickup_city or '').strip() or (getattr(load.quote, 'origin', '') or '').strip() or '—'
    d = (load.delivery_city or '').strip() or (getattr(load.quote, 'destination', '') or '').strip() or '—'
    return f'{o} → {d}'


def _load_rows(company, start_d, end_d):
    from core.models import Load
    from core.services.reports import load_economics
    s, e = _start(start_d), _start(end_d + timedelta(days=1))
    loads = list(Load.objects.filter(company=company, status__in=DONE_STATUSES).filter(
        Q(actual_delivered_at__gte=s, actual_delivered_at__lt=e)
        | Q(actual_delivered_at__isnull=True, delivery_date__gte=s, delivery_date__lt=e))
        .select_related('quote', 'customer'))
    econ = load_economics(company, loads) if loads else {}
    rows = []
    for l in loads:
        ec = econ.get(l.pk) or {}
        q = l.quote
        q_price = float(q.total_amount) if q is not None and q.total_amount else None
        q_floor = float(q.cost_floor) if q is not None and q.cost_floor is not None else None
        actual_cost = float(ec['cost']) if ec.get('cost') is not None and ec.get('cost_basis') == 'actual' else None
        rows.append({
            'load_id': l.pk, 'lane': _lane_of(l),
            'customer': (getattr(l.customer, 'name', '') or '—').strip() or '—',
            'customer_id': l.customer_id,
            'quoted_price': q_price, 'quoted_floor': q_floor,
            'revenue': float(ec.get('revenue') or 0), 'revenue_basis': ec.get('revenue_basis'),
            'actual_cost': actual_cost,
        })
    return rows


def _group(rows, key):
    groups = {}
    for r in rows:
        g = groups.setdefault(r[key], {'name': r[key], 'loads': 0, 'quoted_loads': 0, 'quoted_revenue': 0.0,
                                       'quoted_margin': 0.0, 'actual_loads': 0, 'actual_revenue': 0.0,
                                       'actual_margin': 0.0, 'revenue_estimated_loads': 0})
        g['loads'] += 1
        if r['quoted_price'] and r['quoted_floor'] is not None:
            g['quoted_loads'] += 1
            g['quoted_revenue'] += r['quoted_price']
            g['quoted_margin'] += r['quoted_price'] - r['quoted_floor']
        if r['actual_cost'] is not None:
            g['actual_loads'] += 1
            g['actual_revenue'] += r['revenue']
            g['actual_margin'] += r['revenue'] - r['actual_cost']
            if r['revenue_basis'] == 'estimate':
                g['revenue_estimated_loads'] += 1
    out = []
    for g in groups.values():
        out.append({
            'name': g['name'], 'loads': g['loads'],
            'quoted_loads': g['quoted_loads'],
            'quoted_margin_zar': round(g['quoted_margin'], 2) if g['quoted_loads'] else None,
            'quoted_margin_pct': _pct(g['quoted_margin'], g['quoted_revenue']) if g['quoted_loads'] else None,
            'actual_loads': g['actual_loads'],
            'actual_revenue_zar': round(g['actual_revenue'], 2) if g['actual_loads'] else None,
            'actual_margin_zar': round(g['actual_margin'], 2) if g['actual_loads'] else None,
            'actual_margin_pct': _pct(g['actual_margin'], g['actual_revenue']) if g['actual_loads'] else None,
            'revenue_estimated_loads': g['revenue_estimated_loads'],
        })
    out.sort(key=lambda x: (-(x['actual_revenue_zar'] or 0), -x['loads'], x['name']))
    return out


def _totals(rows):
    t = _group([{**r, 'all': 'all'} for r in rows], 'all')
    return t[0] if t else {'name': 'all', 'loads': 0, 'quoted_loads': 0, 'quoted_margin_zar': None,
                           'quoted_margin_pct': None, 'actual_loads': 0, 'actual_revenue_zar': None,
                           'actual_margin_zar': None, 'actual_margin_pct': None, 'revenue_estimated_loads': 0}


def _block(rows):
    lanes = _group(rows, 'lane')
    customers = _group(rows, 'customer')
    return {
        'totals': _totals(rows),
        'enough_data': any(r['actual_cost'] is not None for r in rows) or any(
            r['quoted_floor'] is not None for r in rows),
        'enough_actuals': any(r['actual_cost'] is not None for r in rows),
        'by_lane': lanes[:TOP_ROWS], 'lanes_total': len(lanes),
        'by_customer': customers[:TOP_ROWS], 'customers_total': len(customers),
        '_lanes_all': lanes,
    }


def _quotes(company, start_d, end_d, target):
    from core.models import Quote
    s, e = _start(start_d), _start(end_d + timedelta(days=1))
    won = list(Quote.objects.filter(company=company, accepted_at__gte=s, accepted_at__lt=e)
               .values_list('total_amount', 'cost_floor'))
    lost = Quote.objects.filter(company=company, rejected_at__gte=s, rejected_at__lt=e).count()
    priced = [(float(p), float(f)) for p, f in won if p and f is not None and float(p) > 0]
    avg = round(sum((p - f) / p * 100 for p, f in priced) / len(priced), 1) if priced else None
    decided = len(won) + lost
    return {
        'won': len(won), 'lost': lost,
        'won_value_zar': round(sum(float(p or 0) for p, _ in won), 2),
        'win_rate_pct': round(len(won) / decided * 100, 1) if decided else None,
        'avg_quoted_margin_won_pct': avg, 'avg_margin_basis_quotes': len(priced),
        'target_margin_pct': target,
        'vs_target_pts': round(avg - target, 1) if avg is not None else None,
    }


def weekly_margin_figures(company, as_of=None):
    """THE function behind the weekly margin email and its API preview."""
    from core.services.quote_costing import target_margin
    monday, sunday = last_full_week(as_of)
    four_start = sunday - timedelta(days=27)
    target = float(target_margin(company))
    week = _block(_load_rows(company, monday, sunday))
    four = _block(_load_rows(company, four_start, sunday))
    losing = [l for l in four.pop('_lanes_all') if l['actual_margin_zar'] is not None and l['actual_margin_zar'] < 0]
    losing.sort(key=lambda l: l['actual_margin_zar'])
    week.pop('_lanes_all')
    q_week = _quotes(company, monday, sunday, target)
    q_four = _quotes(company, four_start, sunday, target)
    avg_actual = four['totals']['actual_margin_pct']
    return {
        'company_id': company.id,
        'week': {'start': monday.isoformat(), 'end': sunday.isoformat()},
        'four_weeks': {'start': four_start.isoformat(), 'end': sunday.isoformat()},
        'target_margin_pct': target,
        'last_week': week,
        'last_4_weeks': four,
        'worst_lanes': losing[:WORST_LANES],
        'quotes': {'last_week': q_week, 'last_4_weeks': q_four},
        'average_margin': {
            'quoted_won_4_weeks_pct': q_four['avg_quoted_margin_won_pct'],
            'actual_4_weeks_pct': avg_actual,
            'target_pct': target,
            'actual_vs_target_pts': round(avg_actual - target, 1) if avg_actual is not None else None,
        },
        'has_activity': bool(four['totals']['loads'] or q_four['won'] or q_four['lost']),
        'notes': {
            'actual_cost': 'Actual margin uses recorded expenses only; loads without them are not counted.',
            'revenue': 'Revenue is invoiced excl. VAT, else the agreed load price.',
        },
    }


# ---------------------------------------------------------------------------
# The email (Monday 07:00 SAST, admins, opt-out)
# ---------------------------------------------------------------------------

def _d(iso):
    from core.services.quote_costing import sa_date
    return sa_date(datetime.fromisoformat(iso).replace(hour=12, tzinfo=SAST))


def _rand(v):
    from core.services.quote_costing import fmt_rand
    return fmt_rand(v, 0) if v is not None else '—'


def _p(v):
    from core.services.quote_costing import fmt_num
    return f'{fmt_num(v, 1)}%' if v is not None else '—'


def build_email(company, fig):
    """(subject, html, text) — plain and scannable."""
    from core.services import followup_emails as fe
    name = getattr(company, 'company_name', '') or 'your company'
    wk = f"{_d(fig['week']['start'])} – {_d(fig['week']['end'])}"
    subject = f'Your margins last week ({wk})'
    t = fig['target_margin_pct']
    html, text = [], []

    def add(h, tx):
        html.append(h)
        text.append(tx)

    def summary_line(block, label):
        tot = block['totals']
        if not block['enough_data']:
            return f'{label}: not enough data yet ({tot["loads"]} load{"s" if tot["loads"] != 1 else ""} delivered).'
        parts = [f'{tot["loads"]} load{"s" if tot["loads"] != 1 else ""} delivered']
        parts.append(f'quoted margin {_p(tot["quoted_margin_pct"])}' if tot['quoted_loads'] else 'no quoted margin')
        parts.append(f'actual {_p(tot["actual_margin_pct"])} on {tot["actual_loads"]} with costs'
                     if tot['actual_loads'] else 'no actual costs recorded yet')
        return f'{label}: ' + ', '.join(parts) + '.'

    add(fe.para(fe.esc(summary_line(fig['last_week'], 'Last week'))),
        summary_line(fig['last_week'], 'Last week'))
    add(fe.para(fe.esc(summary_line(fig['last_4_weeks'], 'Last 4 weeks'))),
        summary_line(fig['last_4_weeks'], 'Last 4 weeks'))

    am = fig['average_margin']
    if am['actual_4_weeks_pct'] is not None:
        gap = am['actual_vs_target_pts']
        line = (f'Average actual margin {_p(am["actual_4_weeks_pct"])} against your {_p(t)} target '
                f'({"+" if gap >= 0 else "−"}{_p(abs(gap))[:-1]} points).')
    elif am['quoted_won_4_weeks_pct'] is not None:
        line = (f'Average quoted margin on won quotes {_p(am["quoted_won_4_weeks_pct"])} against your '
                f'{_p(t)} target. No actual costs recorded yet.')
    else:
        line = f'Your target margin is {_p(t)}. Not enough data yet to compare.'
    add(fe.h2('Margin against target') + fe.para(fe.esc(line)), 'MARGIN AGAINST TARGET\n' + line)

    # Worst lanes
    if fig['worst_lanes']:
        rows = [[fe.esc(l['name']), str(l['actual_loads']), _rand(l['actual_margin_zar']), _p(l['actual_margin_pct'])]
                for l in fig['worst_lanes']]
        hdr = ['Lane', 'Loads', 'Actual margin', '%']
        add(fe.h2('Lanes losing money (last 4 weeks)') + fe.table(hdr, rows, ['left', 'right', 'right', 'right']),
            'LANES LOSING MONEY (LAST 4 WEEKS)\n' + fe.text_table(hdr, rows))
    elif fig['last_4_weeks']['enough_actuals']:
        add(fe.h2('Lanes losing money (last 4 weeks)') + fe.para('None. Every lane with costs made money.'),
            'LANES LOSING MONEY (LAST 4 WEEKS)\nNone. Every lane with costs made money.')

    for key, title in (('by_lane', 'By lane'), ('by_customer', 'By customer')):
        block = fig['last_4_weeks']
        items = block[key]
        heading = f'{title} (last 4 weeks)'
        if not items:
            add(fe.h2(heading) + fe.para('Not enough data yet.', muted=True), heading.upper() + '\nNot enough data yet.')
            continue
        hdr = [title[3:].capitalize(), 'Loads', 'Quoted', 'Actual']
        rows = [[fe.esc(i['name']), str(i['loads']), _p(i['quoted_margin_pct']),
                 _p(i['actual_margin_pct']) + (f' ({i["actual_loads"]})' if i['actual_loads'] and
                                                i['actual_loads'] != i['loads'] else '')]
                for i in items]
        total_key = 'lanes_total' if key == 'by_lane' else 'customers_total'
        more = block[total_key] - len(items)
        add(fe.h2(heading) + fe.table(hdr, rows, ['left', 'right', 'right', 'right'])
            + (fe.para(f'And {more} more in TruckWys.', muted=True) if more > 0 else ''),
            heading.upper() + '\n' + fe.text_table(hdr, rows) + (f'\nAnd {more} more in TruckWys.' if more > 0 else ''))

    qw, q4 = fig['quotes']['last_week'], fig['quotes']['last_4_weeks']
    ql = (f'Last week: {qw["won"]} won, {qw["lost"]} lost'
          + (f' ({_p(qw["win_rate_pct"])} won)' if qw['win_rate_pct'] is not None else '') + '. '
          f'Last 4 weeks: {q4["won"]} won, {q4["lost"]} lost'
          + (f' ({_p(q4["win_rate_pct"])} won)' if q4['win_rate_pct'] is not None else '') + '.')
    add(fe.h2('Quotes won and lost') + fe.para(fe.esc(ql)), 'QUOTES WON AND LOST\n' + ql)

    base = fe.frontend_url()
    add(fe.button(f'{base}/finance/reports?tab=margin', 'Open margin report'),
        f'Open margin report: {base}/finance/reports?tab=margin')
    footer = (f'{fig["notes"]["actual_cost"]} {fig["notes"]["revenue"]} Quoted margin is the quote price less its '
              'cost floor. Turn this email off in Settings → Notifications.')
    intro = fe.para(fe.esc(f'{name}, {wk}.'), muted=True)
    html_body = fe.page(subject, intro, ''.join(html), footer)
    text_body = '\n\n'.join([subject, f'{name}, {wk}.'] + text + [footer])
    return subject, html_body, text_body


def send_weekly_margin_emails(now=None):
    """Monday task: one email per company per week to its active admins who
    haven't opted out. Idempotent (WeeklyMarginReport per company + week)."""
    from django.db import IntegrityError, transaction
    from core.models import Company, User, WeeklyMarginReport
    from core.services import followup_emails as fe
    from core.services.notification_prefs import should_notify
    from core.services.quote_automation import get_settings
    now = now or timezone.now()
    monday, _ = last_full_week(now)
    summary = {'companies': 0, 'emails_sent': 0, 'skipped_no_activity': 0}
    for company in Company.objects.filter(is_deleted=False).iterator():
        try:
            if not get_settings(company).weekly_margin_email_enabled:
                continue
            if WeeklyMarginReport.objects.filter(company=company, week_start=monday).exists():
                continue
            admins = [u for u in User.objects.filter(company=company, is_active=True, role='ADMIN')
                      if u.email and should_notify(u, 'email', 'margin_report')]
            if not admins:
                continue
            fig = weekly_margin_figures(company, now)
            if not fig['has_activity']:
                summary['skipped_no_activity'] += 1
                continue
            try:
                with transaction.atomic():
                    report = WeeklyMarginReport.objects.create(company=company, week_start=monday,
                                                               figures=fig, recipients=len(admins))
            except IntegrityError:
                continue
            subject, html, text = build_email(company, fig)
            sent = sum(1 for u in admins if fe.deliver(u.email, subject, html, text))
            WeeklyMarginReport.objects.filter(pk=report.pk).update(emails_sent=sent)
            summary['companies'] += 1
            summary['emails_sent'] += sent
        except Exception:
            logger.exception('weekly margin email failed for company %s', company.pk)
    return summary
