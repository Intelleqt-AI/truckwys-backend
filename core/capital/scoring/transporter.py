"""Transporter (client / seller) scorecard, Phase 1 (docs/capital-risk/03-design.md §2.2, §2.5, §2.6).

Points out of 100: KYC 15, tenure 10, volume and stability 15, real trip
margin 15, dilution 15, concentration 10, cash stress and subscription 20.

Windows are day-based and end at ``as_of`` (inclusive): 6 months = 180 days
(six 30-day buckets for the coefficient of variation), 12 months = 365 days,
90 days for failed delivery-fee charges. Open receivables (concentration,
receivables_share) use the invoices' current balances, which are not
reconstructed for a past ``as_of``.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from core.capital.policy import worse_grade
from core.capital.reasons import reason

from . import ScoreOutput
from .debtor import _final_points, _order, as_of_date, jsonable, q1, q3

logger = logging.getLogger(__name__)

MODEL_VERSION = 'transporter-scorecard-1.0'

D = Decimal
ZERO = D('0')
ONE = D('1')

OPEN_EXCLUDED = ('DRAFT', 'CANCELLED', 'PAID', 'CREDITED')
DELIVERED_LOAD_STATUSES = ('DELIVERED', 'INVOICED', 'COMPLETED')
WINDOW_6M = 180
WINDOW_12M = 365


def _issued(company):
    from core.models import Invoice
    return Invoice.objects.filter(company=company, status__in=Invoice.ISSUED_STATUSES)


def _excl_vat(row) -> Decimal:
    return (row['total_amount'] or ZERO) - (row['vat_amount'] or ZERO)


def _months_between(start: date, end: date) -> int:
    m = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        m -= 1
    return max(0, m)


def _sqrt(v: Decimal) -> Decimal:
    return v.sqrt() if v > 0 else ZERO


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------

def _kyc(company):
    from core.models import CapitalApplication
    missing = []
    if not (company.registration_number or '').strip():
        missing.append('company registration number')
    if company.vat_registered and not (company.vat_number or '').strip():
        missing.append('VAT number')
    if not ((company.bank_account_number or '').strip() and (company.bank_account_holder or '').strip()):
        missing.append('bank account details')
    app = CapitalApplication.objects.filter(company=company).only('status').first()
    if app is None or app.status != 'APPROVED':
        missing.append('approved Fast Pay application')
    pts = D('3.75') * D(4 - len(missing))
    if missing:
        r = reason('T-KYC-GAP', missing=', '.join(missing))
        return pts, [(r, D(15) - pts)], missing
    return pts, [(reason('T-KYC-COMPLETE'), pts)], missing


def _tenure(company, a: date):
    created = company.created_at.date() if company.created_at else a
    months = _months_between(created, a)
    if months >= 24:
        pts = 10
    elif months >= 12:
        pts = 7
    elif months >= 6:
        pts = 5
    elif months >= 3:
        pts = 3
    else:
        pts = 1
    r = (reason('T-NEW', months=months), D(10 - pts)) if months < 3 else (reason('T-TENURE', months=months), D(pts))
    return D(pts), [r], months


def _volume(company, a: date):
    start = a - timedelta(days=WINDOW_6M)
    rows = list(_issued(company).filter(issue_date__gt=start, issue_date__lte=a)
                .values('issue_date', 'total_amount', 'vat_amount'))
    n = len(rows)
    value = sum((_excl_vat(r) for r in rows), ZERO)
    if n >= 30:
        n_pts = 10
    elif n >= 10:
        n_pts = 7
    elif n >= 3:
        n_pts = 4
    else:
        n_pts = 1
    reasons = [(reason('T-VOLUME', n=n, value=str(value.quantize(ONE, rounding=ROUND_HALF_UP))), D(n_pts))
               if n >= 10 else (reason('T-VOLUME-LOW', n=n), D(10 - n_pts))]
    buckets = [ZERO] * 6
    for r in rows:
        idx = min(5, (a - r['issue_date']).days // 30)
        buckets[idx] += _excl_vat(r)
    mean = sum(buckets, ZERO) / D(6)
    cv = None
    if mean > 0:
        var = sum(((b - mean) ** 2 for b in buckets), ZERO) / D(6)
        cv = q3(_sqrt(var) / mean)
    if cv is not None and cv < D('0.35'):
        cv_pts = 5
    elif cv is not None and cv < D('0.7'):
        cv_pts = 3
    else:
        cv_pts = 0
    if cv is not None and cv_pts < 5:
        reasons.append((reason('T-VOLATILE', cv=str(cv.quantize(D('0.01')))), D(5 - cv_pts)))
    return D(n_pts + cv_pts), reasons, {'n': n, 'value_excl_vat': value, 'cv': cv,
                                        'monthly_excl_vat': buckets, 'n_points': n_pts, 'cv_points': cv_pts}


def _margin_band(pct: Decimal) -> int:
    if pct >= 15:
        return 15
    if pct >= 8:
        return 10
    if pct >= 3:
        return 5
    return 0


def _margin(company, a: date):
    """Real trip margin over 6 months. Never raises: any failure -> unknown."""
    unknown = (D(5), [(reason('T-MARGIN-UNKNOWN'), D(10))], {'basis': None})
    try:
        from core.models import Load
        from core.services import report_figures as rf
        start = a - timedelta(days=WINDOW_6M)
        loads = list(Load.objects.filter(company=company, status__in=DELIVERED_LOAD_STATUSES)
                     .filter(Q(actual_delivered_at__date__gt=start, actual_delivered_at__date__lte=a)
                             | Q(actual_delivered_at__isnull=True, delivery_date__date__gt=start,
                                 delivery_date__date__lte=a))
                     .only('id', 'distance', 'total_amount', 'trip_type', 'return_of', 'costing_snapshot',
                           'empty_return_assumed', 'costing_source', 'costing_inputs', 'status',
                           'costs_closed'))
        if not loads:
            return unknown
        # The same merged cost as the economics endpoint: a load counts as
        # ACTUAL only when invoiced and its costs are complete (key costs
        # recorded, or closed); part-actual loads are modelled.
        from core.services.trip_economics import economics_rows
        rows = economics_rows(company, loads)
        both = [i for i, r in rows.items() if r['revenue_basis'] == 'actual' and r['cost_complete']
                and r['cost'] is not None]
        if len(both) >= 3:
            rev = sum((rows[i]['revenue'] for i in both), ZERO)
            cost = sum((rows[i]['cost'] for i in both), ZERO)
            if rev > 0:
                pct = q1((rev - cost) / rev * 100)
                pts = _margin_band(pct)
                r = (reason('T-MARGIN', pct=str(pct), basis='actual'), D(pts)) if pts >= 10 else \
                    (reason('T-MARGIN-THIN', pct=str(pct), basis='actual'), D(15 - pts))
                return D(pts), [r], {'basis': 'actual', 'pct': pct, 'loads': len(both),
                                     'revenue_excl_vat': rev, 'cost_excl_vat': cost}
        # Modelled: the merged estimate (actual where recorded, estimate for
        # the rest) where costs are not complete.
        costed = [e for e in rows.values() if e['cost'] is not None]
        rev = sum((D(str(e['revenue'])) for e in costed), ZERO)
        cost = sum((D(str(e['cost'])) for e in costed), ZERO)
        if costed and rev > 0:
            pct = q1((rev - cost) / rev * 100)
            pts = min(7, _margin_band(pct))
            r = (reason('T-MARGIN', pct=str(pct), basis='modelled'), D(pts)) if pct >= 8 else \
                (reason('T-MARGIN-THIN', pct=str(pct), basis='modelled'), D(15 - pts))
            return D(pts), [r], {'basis': 'modelled', 'pct': pct, 'loads': len(costed),
                                 'revenue_excl_vat': rev, 'cost_excl_vat': cost}
    except Exception as exc:  # margin must never break a score
        logger.warning('transporter margin failed for company %s: %s', getattr(company, 'pk', None), exc)
    return unknown


def _dilution(company, a: date):
    from core.models import CreditNote
    start = a - timedelta(days=WINDOW_12M)
    inv = list(_issued(company).filter(issue_date__gt=start, issue_date__lte=a)
               .values('status', 'total_amount', 'vat_amount'))
    invoiced = sum((_excl_vat(r) for r in inv), ZERO)
    disputed = sum((_excl_vat(r) for r in inv if r['status'] == 'DISPUTED'), ZERO)
    credits = (CreditNote.objects.filter(company=company, status=CreditNote.ISSUED,
                                         issue_date__gt=start, issue_date__lte=a)
               .aggregate(t=Sum('subtotal'))['t'] or ZERO)
    if invoiced <= 0:
        # No invoicing to measure against: neutral points, no reason (cold start covers it).
        return D(10), [], {'invoiced_excl_vat': ZERO, 'credit_notes_excl_vat': credits,
                           'disputed_excl_vat': disputed, 'pct': None}
    pct = q1((credits + disputed) / invoiced * 100)
    if pct < 1:
        pts = 15
    elif pct < 3:
        pts = 10
    elif pct <= 5:
        pts = 5
    else:
        pts = 0
    r = (reason('T-DILUTION-LOW', pct=str(pct)), D(pts)) if pts >= 10 else \
        (reason('T-DILUTION', pct=str(pct)), D(15 - pts))
    return D(pts), [r], {'invoiced_excl_vat': invoiced, 'credit_notes_excl_vat': credits,
                         'disputed_excl_vat': disputed, 'pct': pct}


def _open_by_debtor(company, a: date) -> dict:
    """{key: open balance} where key is the debtor identity (or the customer when unlinked)."""
    out = defaultdict(lambda: ZERO)
    for r in (_issued(company).exclude(status__in=OPEN_EXCLUDED).filter(balance__gt=0, issue_date__lte=a)
              .values('customer_id', 'customer__debtor_identity_id', 'balance')):
        key = ('D', r['customer__debtor_identity_id']) if r['customer__debtor_identity_id'] else ('C', r['customer_id'])
        out[key] += r['balance']
    return dict(out)


def _concentration(company, a: date):
    by = _open_by_debtor(company, a)
    total = sum(by.values(), ZERO)
    if total <= 0:
        return D(10), [], {'top_share': None, 'open_total': ZERO}
    share = max(by.values()) / total
    pct = share * 100
    if pct < 30:
        pts = 10
    elif pct < 50:
        pts = 6
    elif pct <= 70:
        pts = 3
    else:
        pts = 0
    reasons = [(reason('T-CONCENTRATION', pct=str(q1(pct))), D(10 - pts))] if pct >= 50 else []
    return D(pts), reasons, {'top_share': q3(share), 'open_total': total, 'debtors': len(by)}


def _stress(company, a: date):
    from core.models import DeliveryFeeCharge
    status = (company.subscription_status or 'none').lower()
    hard = False
    reasons = []
    if status in ('active', 'trialing'):
        sub = 10
        reasons.append((reason('T-SUB-OK'), D(10)))
    elif status in ('suspended', 'cancelled'):
        sub = 0
        hard = True
        reasons.append((reason('T-HARD', detail=f'subscription {status}'), D(100)))
    else:  # grace_period, or 'none' (never subscribed): stressed / unknown
        sub = 3
        reasons.append((reason('T-STRESS-SUB', status=status), D(7)))
    start = a - timedelta(days=90)
    failed = (DeliveryFeeCharge.objects.filter(company=company, status='failed')
              .annotate(at=Coalesce('last_attempted_at', 'created_at'))
              .filter(at__date__gt=start, at__date__lte=a).count())
    if failed == 0:
        fee = 10
    elif failed == 1:
        fee = 6
    elif failed <= 3:
        fee = 3
    else:
        fee = 0
    if failed:
        reasons.append((reason('T-STRESS-FEES', n=failed), D(10 - fee)))
    return D(sub + fee), reasons, hard, {'subscription_status': status, 'failed_fee_charges_90d': failed,
                                         'subscription_points': sub, 'fee_points': fee}


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------

def score_transporter(company, policy, *, as_of=None) -> ScoreOutput:
    a = as_of_date(as_of)
    kyc_pts, kyc_r, missing = _kyc(company)
    ten_pts, ten_r, months = _tenure(company, a)
    vol_pts, vol_r, vol = _volume(company, a)
    mar_pts, mar_r, mar = _margin(company, a)
    dil_pts, dil_r, dil = _dilution(company, a)
    con_pts, con_r, con = _concentration(company, a)
    str_pts, str_r, hard_stop, stress = _stress(company, a)

    total = kyc_pts + ten_pts + vol_pts + mar_pts + dil_pts + con_pts + str_pts
    points = _final_points(total)
    grade = policy.grade_for_points(points)
    pd = policy.pd_for_points(points)
    cold = months < 3 or vol['n'] < 3
    capped = False
    if cold:
        new_grade = worse_grade(grade, 'C')
        if new_grade != grade:
            capped = True
            grade = new_grade
            pd = max(pd, policy.representative_pd(grade))
    # A new transporter without a hard stop is referred (D, line <= R500k and
    # manual review), not declined: too little history is not a bad history.
    if cold and not hard_stop and grade == 'E':
        grade = 'D'
        pd = policy.representative_pd('D')
    if hard_stop:
        grade = 'E'
        pd = max(policy.representative_pd('E'), pd)

    reasons = _order(kyc_r + ten_r + vol_r + mar_r + dil_r + con_r + str_r)
    try:
        dr = dilution_reserve_pct(company, policy, as_of=a)
    except Exception as exc:  # informational only here
        logger.warning('dilution reserve failed: %s', exc)
        dr = None
    inputs = jsonable({
        'as_of': a,
        'company_id': company.pk,
        'is_demo': bool(getattr(company, 'is_demo', False)),
        'kyc': {'missing': missing, 'points': kyc_pts},
        'tenure_months': months,
        'volume': vol,
        'margin': mar,
        'dilution': dil,
        'dilution_reserve_pct': dr,
        'concentration': con,
        'stress': stress,
        'components': {'kyc': kyc_pts, 'tenure': ten_pts, 'volume': vol_pts, 'margin': mar_pts,
                       'dilution': dil_pts, 'concentration': con_pts, 'stress': str_pts, 'total': total},
        'cold_start': cold, 'cold_start_grade_capped': capped,
        'policy': policy.to_snapshot(),
        'model_version': MODEL_VERSION,
    })
    return ScoreOutput(kind='TRANSPORTER', grade=grade, points=points, pd_12m=pd, reason_codes=reasons,
                       inputs=inputs, model_version=MODEL_VERSION, expected_dtp_days=None,
                       hard_stop=hard_stop, cold_start=cold)


def receivables_share(company, debtor, *, as_of=None) -> Decimal:
    """Share (0-1, 4 dp) of the company's open receivables owed by customers linked to ``debtor``."""
    a = as_of_date(as_of)
    by = _open_by_debtor(company, a)
    total = sum(by.values(), ZERO)
    if total <= 0 or debtor is None:
        return D('0.0000')
    mine = by.get(('D', debtor.pk), ZERO)
    return (mine / total).quantize(D('0.0001'), rounding=ROUND_HALF_UP)


def _month_start(d: date, back: int) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def dilution_vintages(company, *, as_of=None) -> list[dict]:
    """12 calendar-month vintages ending with ``as_of``'s month: invoiced (excl. VAT)
    and credit notes (excl. VAT, ISSUED, dated within 90 days of the invoice and
    on or before ``as_of``) raised against that month's invoices."""
    from core.models import CreditNote
    a = as_of_date(as_of)
    first = _month_start(a, 11)
    inv = list(_issued(company).filter(issue_date__gte=first, issue_date__lte=a)
               .values('id', 'issue_date', 'total_amount', 'vat_amount'))
    by_month = defaultdict(lambda: {'invoiced': ZERO, 'diluted': ZERO})
    issue = {}
    for r in inv:
        key = (r['issue_date'].year, r['issue_date'].month)
        by_month[key]['invoiced'] += _excl_vat(r)
        issue[r['id']] = (key, r['issue_date'])
    if issue:
        for cn in (CreditNote.objects.filter(invoice_id__in=list(issue), status=CreditNote.ISSUED, issue_date__lte=a)
                   .values('invoice_id', 'issue_date', 'subtotal')):
            key, inv_date = issue[cn['invoice_id']]
            if (cn['issue_date'] - inv_date).days <= 90:
                by_month[key]['diluted'] += cn['subtotal'] or ZERO
    out = []
    for back in range(11, -1, -1):
        ms = _month_start(a, back)
        v = by_month.get((ms.year, ms.month), {'invoiced': ZERO, 'diluted': ZERO})
        ratio = (v['diluted'] / v['invoiced']) if v['invoiced'] > 0 else None
        out.append({'month': ms.strftime('%Y-%m'), 'invoiced': v['invoiced'], 'diluted': v['diluted'],
                    'ratio': ratio})
    return out


def dilution_reserve_from(ed: Decimal, ds: Decimal, *, sf: Decimal = D('1.75'), dhr: Decimal = D('1.2')) -> Decimal:
    """DR% = (SF*ED + (DS-ED)*DS/ED) * DHR, as a percent to 0.1. ED/DS are ratios (0.015 = 1.5%)."""
    if ed <= 0:
        # ED = 0 means no dilution in any vintage; only the stress term on DS remains (0 when DS = 0).
        dr = ds * dhr
    else:
        dr = (sf * ed + (ds - ed) * ds / ed) * dhr
    return q1(dr * 100)


def dilution_reserve_pct(company, policy, *, as_of=None) -> Decimal:
    """Dynamic dilution reserve (design §2.5) as a percent, e.g. Decimal('11.2').

    ED = invoiced-weighted 12-month dilution ratio, DS = worst monthly vintage,
    SF 1.75, DHR 1.2. Fewer than 6 vintages with invoicing -> cold-start ED/DS
    from ``policy.params['cold_start_dilution']``.
    """
    vintages = [v for v in dilution_vintages(company, as_of=as_of) if v['ratio'] is not None]
    if len(vintages) < 6:
        cs = policy.params.get('cold_start_dilution') or {'ed': '0.02', 'ds': '0.05'}
        return dilution_reserve_from(D(str(cs['ed'])), D(str(cs['ds'])))
    invoiced = sum((v['invoiced'] for v in vintages), ZERO)
    diluted = sum((v['diluted'] for v in vintages), ZERO)
    ed = diluted / invoiced if invoiced > 0 else ZERO
    ds = max(v['ratio'] for v in vintages)
    return dilution_reserve_from(ed, ds)


def stressed_pd(score) -> Decimal:
    """min(1, pd_12m x 3) for a CapitalScore row or a ScoreOutput."""
    pd = D(str(getattr(score, 'pd_12m', score)))
    return min(ONE, pd * 3).quantize(D('0.00001'), rounding=ROUND_HALF_UP)
