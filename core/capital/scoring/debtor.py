"""Debtor (obligor) scorecard, Phase 1 (docs/capital-risk/03-design.md §2.1, §2.6).

Network data = invoices across ALL tenants whose customer links to the
debtor's ``DebtorIdentity``, excluding DRAFT / CANCELLED invoices and demo
companies. Only invoices issued on or before ``as_of`` count; an invoice paid
after ``as_of`` counts as open at ``as_of`` (so a past ``as_of`` replays the
past). Credited invoices (fully reversed, nothing paid) are neither paid nor
open.

* days-to-pay (DTP) = paid date - issue date (PAID invoices)
* days late         = paid date - due date (due dates follow real terms since the foundation)
* censoring         an open invoice older than the paid mean (or the sector
                    prior when nothing is paid) counts as one more DTP
                    observation at its current age, which can only lengthen
                    the estimate (conservative).

Points out of 100: CIPC 20, bureau 30, network payment 40, sector 10.
Everything is Decimal and depends only on DB state, ``as_of`` and the policy
(plus the cached CIPC / bureau lookups, see core.capital.adapters).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from core.capital.policy import worse_grade
from core.capital.reasons import reason

from . import ScoreOutput

MODEL_VERSION = 'debtor-scorecard-1.0'

D = Decimal
ZERO = D('0')
ONE = D('1')
TENTH = D('0.1')
THOUSANDTH = D('0.001')

EXCLUDED_STATUSES = ('DRAFT', 'CANCELLED')
HARD_CIPC = ('BUSINESS_RESCUE', 'LIQUIDATION', 'DEREGISTERED', 'DEREGISTRATION')

SECTOR_POINTS = {
    'RETAIL_FMCG': 10, 'FUEL': 10,
    'AGRI': 7, 'MANUFACTURING': 7, 'LOGISTICS': 7,
    'MINING': 5, 'CONSTRUCTION': 5, 'OTHER': 5, 'UNKNOWN': 5,
    'GOVERNMENT': 4,
}
SECTOR_LABEL = {
    'RETAIL_FMCG': 'Retail / FMCG', 'MINING': 'Mining', 'AGRI': 'Agriculture', 'CONSTRUCTION': 'Construction',
    'MANUFACTURING': 'Manufacturing', 'FUEL': 'Fuel', 'LOGISTICS': 'Logistics',
    'GOVERNMENT': 'Government / SOE', 'OTHER': 'Other', 'UNKNOWN': 'Unknown',
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def as_of_date(as_of=None) -> date:
    if as_of is None:
        return timezone.localdate()
    if isinstance(as_of, datetime):
        return timezone.localtime(as_of).date() if timezone.is_aware(as_of) else as_of.date()
    return as_of


def q1(v: Decimal) -> Decimal:
    return v.quantize(TENTH, rounding=ROUND_HALF_UP)


def q3(v: Decimal) -> Decimal:
    return v.quantize(THOUSANDTH, rounding=ROUND_HALF_UP)


def _mean(values) -> Decimal | None:
    values = list(values)
    if not values:
        return None
    return D(sum(values)) / D(len(values))


def _paid_date(paid_at) -> date | None:
    if paid_at is None:
        return None
    return timezone.localtime(paid_at).date() if timezone.is_aware(paid_at) else paid_at.date()


def jsonable(value):
    """Decimals and dates to strings, recursively (for CapitalScore.inputs)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def sector_of(debtor) -> str:
    if getattr(debtor, 'is_government', False):
        return 'GOVERNMENT'
    s = (getattr(debtor, 'sector', '') or 'UNKNOWN').upper()
    return s if s in SECTOR_POINTS else 'UNKNOWN'


def _network_qs(debtor, *, company=None):
    from core.models import Invoice
    qs = (Invoice.objects.filter(customer__debtor_identity=debtor)
          .exclude(status__in=EXCLUDED_STATUSES)
          .exclude(company__is_demo=True))
    if company is not None:
        qs = qs.filter(company=company)
    return qs


def _rows(debtor, as_of: date, *, company=None) -> list[dict]:
    """One dict per network invoice as seen at ``as_of``: kind PAID / OPEN / CREDITED."""
    rows = []
    for r in (_network_qs(debtor, company=company).filter(issue_date__lte=as_of)
              .values('id', 'company_id', 'issue_date', 'due_date', 'status', 'paid_at')):
        paid_on = _paid_date(r['paid_at']) if r['status'] == 'PAID' else None
        if paid_on is not None and paid_on <= as_of:
            kind = 'PAID'
        elif r['status'] == 'CREDITED':
            kind = 'CREDITED'
        else:
            kind = 'OPEN'  # includes invoices paid after as_of
        rows.append({**r, 'kind': kind, 'paid_on': paid_on if kind == 'PAID' else None})
    return rows


def _observations(rows, as_of: date, threshold: Decimal | None):
    """(paid DTPs, censored open ages) as Decimals."""
    paid = [D((r['paid_on'] - r['issue_date']).days) for r in rows if r['kind'] == 'PAID']
    censored = []
    if threshold is not None:
        for r in rows:
            if r['kind'] == 'OPEN':
                age = D((as_of - r['issue_date']).days)
                if age > threshold:
                    censored.append(age)
    return paid, censored


def _stats(rows, as_of: date) -> dict:
    """Decimal statistics for a set of invoice rows (network or one pair)."""
    paid_rows = [r for r in rows if r['kind'] == 'PAID']
    paid_n = len(paid_rows)
    open_n = sum(1 for r in rows if r['kind'] == 'OPEN')
    dtps = [D((r['paid_on'] - r['issue_date']).days) for r in paid_rows]
    lates = [D((r['paid_on'] - r['due_date']).days) for r in paid_rows]
    avg_dtp = _mean(dtps)
    late30 = q3(D(sum(1 for x in lates if x > 30)) / D(paid_n)) if paid_n else None
    ontime = q3(D(sum(1 for x in lates if x <= 0)) / D(paid_n)) if paid_n else None

    def window_mean(days):
        start = as_of - timedelta(days=days)
        vals = [D((r['paid_on'] - r['issue_date']).days) for r in paid_rows if r['paid_on'] > start]
        return _mean(vals)

    dtp_60d = window_mean(60)
    dtp_12m = window_mean(365)
    _, censored = _observations(rows, as_of, avg_dtp)
    return {
        'paid_n': paid_n, 'open_n': open_n,
        'avg_dtp': q1(avg_dtp) if avg_dtp is not None else None,
        'late30_share': late30, 'ontime_share': ontime,
        'dtp_60d': q1(dtp_60d) if dtp_60d is not None else None,
        'dtp_12m': q1(dtp_12m) if dtp_12m is not None else None,
        'trend_days': q1(dtp_60d - dtp_12m) if dtp_60d is not None and dtp_12m is not None else None,
        'censored_n': len(censored),
    }


def _network(debtor, as_of: date) -> dict:
    from core.models import CreditNote
    rows = _rows(debtor, as_of)
    out = _stats(rows, as_of)
    # credit-note rate: share of invoices issued in the last 12 months with an ISSUED credit note
    start = as_of - timedelta(days=365)
    recent_ids = [r['id'] for r in rows if r['issue_date'] > start]
    if recent_ids:
        credited = (CreditNote.objects.filter(invoice_id__in=recent_ids, status=CreditNote.ISSUED,
                                              issue_date__lte=as_of)
                    .values('invoice_id').distinct().count())
        out['credit_note_rate'] = q3(D(credited) / D(len(recent_ids)))
    else:
        out['credit_note_rate'] = None
    out['n_transporters'] = len({r['company_id'] for r in rows if r['company_id']})
    first = min((r['issue_date'] for r in rows), default=None)
    out['months_active'] = ((as_of - first).days // 30) if first else 0
    return out


# ---------------------------------------------------------------------------
# public features
# ---------------------------------------------------------------------------

def network_features(debtor, *, as_of=None) -> dict:
    """JSON-safe network payment features (Decimals as strings)."""
    return jsonable(_network(debtor, as_of_date(as_of)))


def pair_features(company, debtor, *, as_of=None) -> dict:
    """Payment features of one transporter's invoices to this debtor (JSON-safe)."""
    a = as_of_date(as_of)
    s = _stats(_rows(debtor, a, company=company), a)
    return jsonable({'paid_n': s['paid_n'], 'avg_dtp': s['avg_dtp'], 'open_n': s['open_n'],
                     'late30_share': s['late30_share'], 'censored_n': s['censored_n']})


def _prior(policy, sector: str) -> Decimal:
    priors = policy.params.get('dtp_prior_days') or {}
    v = priors.get(sector, priors.get('UNKNOWN', 60))
    return D(str(v))


def _blend(rows, as_of: date, threshold: Decimal | None, base: Decimal, k: Decimal):
    paid, censored = _observations(rows, as_of, threshold)
    obs = paid + censored
    n = len(obs)
    if not n:
        return base, {'n': 0, 'paid_n': len(paid), 'censored_n': 0, 'mean': None, 'z': '0'}
    mean = _mean(obs)
    z = D(n) / (D(n) + k)
    est = z * mean + (ONE - z) * base
    return est, {'n': n, 'paid_n': len(paid), 'censored_n': len(censored), 'mean': str(q1(mean)),
                 'z': str(z.quantize(D('0.0001'), rounding=ROUND_HALF_UP))}


def expected_dtp(debtor, policy, *, company=None, as_of=None):
    """Credibility-weighted expected days-to-pay: (Decimal days to 0.1, basis dict).

    network: ``Z = n/(n+k)``, ``est = Z*mean + (1-Z)*prior(sector)``; open
    invoices older than the paid mean (or the prior when nothing is paid) count
    as observations at their age. With ``company`` and at least one paid pair
    invoice, the pair mean is blended over the network estimate the same way.
    """
    a = as_of_date(as_of)
    sector = sector_of(debtor)
    prior = _prior(policy, sector)
    k = D(policy.int('dtp_credibility_k'))
    rows = _rows(debtor, a)
    paid_mean = _mean(D((r['paid_on'] - r['issue_date']).days) for r in rows if r['kind'] == 'PAID')
    est, net_basis = _blend(rows, a, paid_mean if paid_mean is not None else prior, prior, k)
    basis = {'prior_days': str(prior), 'sector': sector, 'k': int(k), 'network': net_basis,
             'network_estimate': str(q1(est)), 'pair': None, 'as_of': a.isoformat()}
    if company is not None:
        prow = [r for r in rows if r['company_id'] == company.pk]
        pair_paid_mean = _mean(D((r['paid_on'] - r['issue_date']).days) for r in prow if r['kind'] == 'PAID')
        if pair_paid_mean is not None:
            est, pair_basis = _blend(prow, a, pair_paid_mean, est, k)
            basis['pair'] = pair_basis
        else:
            basis['pair'] = {'n': 0, 'paid_n': 0, 'note': 'no paid invoices on this pair'}
    return q1(est), basis


# ---------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------

def _years_between(start: date, end: date) -> Decimal:
    return D((end - start).days) / D('365.25')


def _resolve_cipc(debtor, cipc_result):
    """CIPC data to score with: the lookup result, else what is stored on the identity."""
    from core.capital.adapters import CIPCResult, lookup_cipc
    res = cipc_result if cipc_result is not None else lookup_cipc(debtor)
    if not res.available and (debtor.cipc_status or 'UNKNOWN') != 'UNKNOWN':
        res = CIPCResult(available=True, status=debtor.cipc_status, legal_name=debtor.legal_name or '',
                         incorporation_date=debtor.incorporation_date, source='stored', is_fake=False)
    return res


def _cipc_part(res, as_of: date):
    """(points, reasons-with-impact, hard_stop, years)."""
    if not res.available or res.status == 'UNKNOWN':
        return 8, [(reason('D-CIPC-UNKNOWN'), 12)], False, None
    if res.status in HARD_CIPC:
        return 0, [(reason('D-CIPC-HARD', status=res.status.replace('_', ' ').lower()), 100)], True, None
    if res.incorporation_date is None:
        # In business but age unknown: scored like an unchecked status (conservative).
        return 8, [(reason('D-CIPC-UNKNOWN'), 12)], False, None
    years = _years_between(res.incorporation_date, as_of)
    yrs = str(q1(years))
    if years >= 5:
        return 20, [(reason('D-CIPC-OK', years=yrs), 20)], False, years
    if years >= 2:
        return 14, [(reason('D-CIPC-OK', years=yrs), 14)], False, years
    return 6, [(reason('D-YOUNG', years=yrs), 14)], False, years


def _bureau_part(res):
    if not res.available or res.score is None:
        return D(12), [(reason('D-BUREAU-NONE'), 18)]
    pts = D(30) * D(res.score) / D(100)
    out = []
    if res.score >= 60:
        out.append((reason('D-BUREAU-GOOD', score=res.score), pts))
    else:
        out.append((reason('D-BUREAU-WEAK', score=res.score), D(30) - pts))
    if res.judgments:
        pen = min(D(15), D(5) * D(int(res.judgments)))
        pts -= pen
        out.append((reason('D-JUDGMENTS', n=int(res.judgments)), pen))
    return max(ZERO, pts), out


def _network_part(net: dict, policy):
    min_paid = policy.int('new_debtor_paid_invoices')
    if net['paid_n'] < min_paid:
        cap = policy.params.get('new_debtor_cap')
        return D(16), [(reason('D-COLD-START', n=min_paid, paid=net['paid_n'], cap=cap), 24)], True
    out = []
    late = net['late30_share'] or ZERO
    ontime = net['ontime_share'] or ZERO
    pay_pts = D(25) * (ONE - late)
    if late >= D('0.2'):
        out.append((reason('D-NET-LATE', pct=int(q1(late * 100)), n=net['n_transporters']), D(25) - pay_pts))
    else:
        out.append((reason('D-NET-ONTIME', pct=int(q1(ontime * 100)), n=net['n_transporters']), pay_pts))
    avg = net['avg_dtp']
    if avg <= 45:
        dtp_pts = D(10)
    elif avg >= 90:
        dtp_pts = ZERO
    else:
        dtp_pts = D(10) * (D(90) - avg) / D(45)
    out.append((reason('D-NET-DTP', days=str(avg), n=net['paid_n']), dtp_pts))
    nt = net['n_transporters']
    breadth = D(5) if nt >= 3 else D(3) if nt == 2 else D(1) if nt == 1 else ZERO
    out.append((reason('D-NET-BREADTH', n=nt), breadth))
    pts = pay_pts + dtp_pts + breadth
    if net['trend_days'] is not None and net['trend_days'] > 10:
        pts -= 5
        out.append((reason('D-NET-TREND-UP', days=str(net['trend_days'])), D(5)))
    if net['credit_note_rate'] is not None and net['credit_note_rate'] > D('0.05'):
        pts -= 5
        out.append((reason('D-NET-DISPUTES', pct=str(q1(net['credit_note_rate'] * 100))), D(5)))
    return max(ZERO, pts), out, False


def _order(scored: list) -> list[dict]:
    """Hard stops first, then negatives by points lost, then positives by points earned."""
    rank = {'!': 0, '-': 1, '+': 2}
    ordered = sorted(enumerate(scored), key=lambda t: (rank.get(t[1][0]['direction'], 3), -D(t[1][1]), t[0]))
    return [r for _, (r, _impact) in ordered]


def _final_points(total: Decimal) -> int:
    return int(max(ZERO, min(D(100), total)).quantize(ONE, rounding=ROUND_HALF_UP))


def score_debtor(debtor, policy, *, as_of=None, cipc_result=None, bureau_result=None) -> ScoreOutput:
    """Phase 1 debtor scorecard. ``cipc_result`` / ``bureau_result`` may be
    passed to skip the (cached) adapter lookups."""
    from core.capital.adapters import lookup_bureau

    a = as_of_date(as_of)
    sector = sector_of(debtor)
    cipc = _resolve_cipc(debtor, cipc_result)
    bureau = bureau_result if bureau_result is not None else lookup_bureau(debtor)
    net = _network(debtor, a)

    cipc_pts, cipc_reasons, hard_stop, years = _cipc_part(cipc, a)
    bureau_pts, bureau_reasons = _bureau_part(bureau)
    net_pts, net_reasons, cold = _network_part(net, policy)
    sec_pts = SECTOR_POINTS[sector]
    sec_label = SECTOR_LABEL[sector]
    sec_reason = (reason('D-SECTOR', sector=sec_label), D(sec_pts)) if sec_pts >= 7 else \
        (reason('D-SECTOR-RISK', sector=sec_label), D(10 - sec_pts))

    total = D(cipc_pts) + bureau_pts + net_pts + D(sec_pts)
    points = _final_points(total)
    grade = policy.grade_for_points(points)
    pd = policy.pd_for_points(points)
    capped = False
    if cold and not (bureau.available and bureau.score is not None and bureau.score >= 70):
        new_grade = worse_grade(grade, 'C')
        if new_grade != grade:
            capped = True
            grade = new_grade
            pd = max(pd, policy.representative_pd(grade))
    # No data is not bad data: a new debtor with no bureau file and no hard
    # flag is referred (D), not declined (E). Bad bureau data can still give E.
    if cold and not hard_stop and grade == 'E' and not (bureau.available and bureau.score is not None):
        grade = 'D'
        pd = policy.representative_pd('D')
    if hard_stop:
        grade = 'E'
        pd = max(policy.representative_pd('E'), pd)

    reasons = _order(cipc_reasons + bureau_reasons + net_reasons + [sec_reason])
    exp_dtp, dtp_basis = expected_dtp(debtor, policy, as_of=a)

    inputs = jsonable({
        'as_of': a,
        'debtor_id': debtor.pk,
        'sector': sector,
        'network': net,
        'cipc': {**cipc.summary(), 'years': q1(years) if years is not None else None},
        'bureau': bureau.summary(),
        'components': {'cipc': cipc_pts, 'bureau': q1(bureau_pts), 'network': q1(net_pts), 'sector': sec_pts,
                       'total': q1(total)},
        'cold_start': cold, 'cold_start_grade_capped': capped,
        'expected_dtp': {'days': exp_dtp, 'basis': dtp_basis},
        'policy': policy.to_snapshot(),
        'model_version': MODEL_VERSION,
    })
    return ScoreOutput(kind='DEBTOR', grade=grade, points=points, pd_12m=pd, reason_codes=reasons,
                       inputs=inputs, model_version=MODEL_VERSION, expected_dtp_days=exp_dtp,
                       hard_stop=hard_stop, cold_start=cold)
