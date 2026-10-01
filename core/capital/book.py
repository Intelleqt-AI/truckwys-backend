"""The funder's book: exposures, limits and headroom, concentration, stress, risk index.

Everything is derived from the append-only ledger (``core.capital.ledger``):
exposure = committed = reserved + outstanding, per scope. Limits come from the
policy in force (``core.capital.policy``), overridden by the newest
``CapitalLimit`` row for a scope. Design: docs/capital-risk/03-design.md §3.

``load_state(funder)`` reads the book once (a handful of grouped queries); the
decision engine calls it *under the funder row lock*, so the headroom it sees
cannot change before it reserves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from core.capital import ledger
from core.capital.policy import GRADES, Policy, policy_for_funder

ZERO = Decimal('0.00')
D = Decimal


def _q(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal('0.01'))


def _pct(part, whole) -> Decimal:
    return (Decimal(part) / Decimal(whole)) if whole else Decimal('0')


SECTOR_LABELS = dict()


def sector_label(code) -> str:
    if not SECTOR_LABELS:
        from core.models import DebtorIdentity
        SECTOR_LABELS.update(dict(DebtorIdentity.SECTOR_CHOICES))
    return SECTOR_LABELS.get(code or 'UNKNOWN', code or 'Unknown')


@dataclass
class BookState:
    funder: object
    policy: Policy
    pot: Decimal
    reserved: Decimal
    outstanding: Decimal
    committed: Decimal
    by_debtor: dict = field(default_factory=dict)      # debtor_id (None = unidentified) -> committed
    by_company: dict = field(default_factory=dict)
    by_pair: dict = field(default_factory=dict)        # (company_id, debtor_id) -> committed
    by_sector: dict = field(default_factory=dict)      # sector code -> committed
    debtor_meta: dict = field(default_factory=dict)    # debtor_id -> {name, sector, grade, pd, dtp, hold, cold_start}
    limits: dict = field(default_factory=dict)         # (scope, debtor_id, company_id, sector) -> CapitalLimit
    small_book: bool = True
    top10_ids: list = field(default_factory=list)
    top10_share: Decimal = Decimal('0')
    top10_band: str = 'ok'
    top1_share: Decimal = Decimal('0')
    hhi: Decimal = Decimal('0')
    n_eff: Decimal | None = None

    @property
    def headroom(self) -> Decimal:
        return max(ZERO, self.pot - self.committed)


def _latest_limits(funder) -> dict:
    from core.models import CapitalLimit
    today = timezone.localdate()
    out = {}
    for row in CapitalLimit.objects.filter(funder=funder).order_by('created_at', 'id'):
        key = (row.scope, row.debtor_id, row.company_id, row.sector or '')
        out[key] = row  # newest wins (ordered ascending)
    return {k: v for k, v in out.items() if v.valid_until is None or v.valid_until >= today}


def _debtor_meta(debtor_ids) -> dict:
    from core.models import CapitalScore, DebtorIdentity
    ids = [d for d in debtor_ids if d is not None]
    meta = {}
    for d in DebtorIdentity.objects.filter(pk__in=ids):
        meta[d.pk] = {'name': d.display_name, 'sector': d.sector or 'UNKNOWN', 'grade': None, 'pd': None,
                      'dtp': None, 'hold': d.on_hold, 'cold_start': None, 'registration_number': d.registration_number}
    seen = set()
    for s in (CapitalScore.objects.filter(kind='DEBTOR', debtor_id__in=ids)
              .order_by('-created_at', '-id').only('debtor_id', 'grade', 'pd_12m', 'expected_dtp_days',
                                                   'cold_start')):
        if s.debtor_id in seen:
            continue
        seen.add(s.debtor_id)
        meta[s.debtor_id].update(grade=s.grade, pd=s.pd_12m, dtp=s.expected_dtp_days, cold_start=s.cold_start)
    return meta


def load_state(funder, policy: Policy | None = None) -> BookState:
    policy = policy or policy_for_funder(funder)
    totals = ledger.balances(funder=funder)
    by_debtor = ledger.committed_by(funder, 'debtor')
    by_company = ledger.committed_by(funder, 'company')
    by_pair = ledger.committed_by_pair(funder)
    meta = _debtor_meta(by_debtor.keys())
    by_sector: dict = {}
    for did, amt in by_debtor.items():
        sector = meta.get(did, {}).get('sector', 'UNKNOWN') if did else 'UNKNOWN'
        by_sector[sector] = by_sector.get(sector, ZERO) + amt

    st = BookState(funder=funder, policy=policy, pot=_q(funder.pot_limit), reserved=totals['reserved'],
                   outstanding=totals['outstanding'], committed=totals['committed'], by_debtor=by_debtor,
                   by_company=by_company, by_pair=by_pair, by_sector=by_sector, debtor_meta=meta,
                   limits=_latest_limits(funder))
    st.small_book = st.committed <= policy.dec('small_book_threshold')

    named = sorted(((d, a) for d, a in by_debtor.items() if d is not None and a > 0), key=lambda x: -x[1])
    total = st.committed
    if total > 0 and named:
        st.top10_ids = [d for d, _ in named[:10]]
        st.top10_share = _pct(sum(a for _, a in named[:10]), total)
        st.top1_share = _pct(named[0][1], total)
        shares = [_pct(a, total) for _, a in named]
        st.hhi = sum((s * s for s in shares), Decimal('0'))
        st.n_eff = (Decimal('1') / st.hhi).quantize(Decimal('0.1')) if st.hhi > 0 else None
    if not st.small_book:
        if st.top10_share > policy.dec('top10_hard_pct'):
            st.top10_band = 'hard'
        elif st.top10_share > policy.dec('top10_soft_pct'):
            st.top10_band = 'soft'
    return st


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

def _limit_row(st: BookState, scope, *, debtor_id=None, company_id=None, sector=''):
    return st.limits.get((scope, debtor_id, company_id, sector or ''))


def debtor_cap(st: BookState, debtor_id, grade: str | None, cold_start: bool) -> tuple[Decimal, str]:
    p = st.policy
    row = _limit_row(st, 'DEBTOR', debtor_id=debtor_id)
    if row is not None and row.hold:
        return ZERO, 'hold'
    if row is not None and row.amount is not None:
        return _q(row.amount), 'manual'
    if grade is None:  # never scored (e.g. legacy exposure): treat as a new, weak name
        grade, cold_start = 'D', True
    cap = _q(st.pot * p.dec('debtor_cap_pct', grade))
    source = f'grade {grade} {p.dec("debtor_cap_pct", grade) * 100:.0f}% of pot'
    if grade == 'D':
        cap = min(cap, p.dec('debtor_cap_d_max'))
    if cold_start:
        if p.dec('new_debtor_cap') < cap:
            cap, source = p.dec('new_debtor_cap'), 'new debtor'
    if st.small_book and p.dec('small_book_debtor_cap') < cap:
        cap, source = p.dec('small_book_debtor_cap'), 'small book'
    return _q(cap), source


def transporter_cap(st: BookState, company_id, line_limit, grade: str | None) -> tuple[Decimal, str]:
    p = st.policy
    row = _limit_row(st, 'TRANSPORTER', company_id=company_id)
    if row is not None and row.hold:
        return ZERO, 'hold'
    line = _q(line_limit)
    if row is not None and row.amount is not None:
        return min(line, _q(row.amount)), 'manual'
    grade = grade or 'D'  # never scored: the new-transporter line
    cap, source = min(line, p.dec('transporter_cap', grade)), f'grade {grade}'
    if line <= cap:
        source = 'line limit'
    if st.small_book and p.dec('small_book_transporter_cap') < cap:
        cap, source = p.dec('small_book_transporter_cap'), 'small book'
    return _q(cap), source


def pair_cap(st: BookState, company_id, debtor_id, d_cap: Decimal, t_cap: Decimal) -> tuple[Decimal, str]:
    p = st.policy
    row = _limit_row(st, 'PAIR', debtor_id=debtor_id, company_id=company_id)
    if row is not None and row.hold:
        return ZERO, 'hold'
    if row is not None and row.amount is not None:
        return _q(row.amount), 'manual'
    cap = min(d_cap, _q(t_cap * p.dec('pair_share_of_line')))
    source = 'min(debtor cap, 60% of line)'
    if st.small_book and p.dec('small_book_pair_cap') < cap:
        cap, source = p.dec('small_book_pair_cap'), 'small book'
    return _q(cap), source


def sector_cap(st: BookState, sector: str) -> tuple[Decimal, str]:
    p = st.policy
    row = _limit_row(st, 'SECTOR', sector=sector)
    if row is not None and row.hold:
        return ZERO, 'hold'
    if row is not None and row.amount is not None:
        return _q(row.amount), 'manual'
    return _q(st.pot * p.dec('sector_cap_pct')), f'{p.dec("sector_cap_pct") * 100:.0f}% of pot'


def headroom(st: BookState, *, company_id, line_limit, debtor_id, debtor_grade, debtor_cold_start,
             transporter_grade, sector) -> dict:
    """Headroom per scope for one (transporter, debtor) request.

    Returns ``{scope: {cap, used, headroom, source}}`` plus ``advance_brake_pp``
    (top-10 soft brake) and ``ticket_cap`` (top-10 soft ticket limit).
    """
    p = st.policy
    out: dict = {}

    def put(scope, cap, used, source):
        cap, used = _q(cap), _q(used)
        out[scope] = {'cap': cap, 'used': used, 'headroom': max(ZERO, cap - used), 'source': source}

    put('pot', st.pot, st.committed, 'funder pot')
    d_cap, d_src = debtor_cap(st, debtor_id, debtor_grade, debtor_cold_start)
    put('debtor', d_cap, st.by_debtor.get(debtor_id, ZERO), d_src)
    t_cap, t_src = transporter_cap(st, company_id, line_limit, transporter_grade)
    put('transporter', t_cap, st.by_company.get(company_id, ZERO), t_src)
    pc, p_src = pair_cap(st, company_id, debtor_id, d_cap, t_cap)
    put('pair', pc, st.by_pair.get((company_id, debtor_id), ZERO), p_src)
    sector = sector or 'UNKNOWN'
    s_cap, s_src = sector_cap(st, sector)
    put('sector', s_cap, st.by_sector.get(sector, ZERO), s_src)

    brake = Decimal('0')
    ticket_cap = None
    if not st.small_book and debtor_id in st.top10_ids:
        if st.top10_band == 'hard':
            put('top10', ZERO, ZERO, f'top-10 share {st.top10_share * 100:.1f}% above hard stop')
        elif st.top10_band == 'soft':
            brake = p.dec('top10_soft_brake_pp')
            ticket_cap = p.dec('top10_soft_ticket_cap')
            put('top10', ticket_cap, ZERO, f'top-10 share {st.top10_share * 100:.1f}%: soft brake')
    if st.n_eff is not None and not st.small_book and debtor_id in st.top10_ids \
            and st.n_eff < Decimal(p.int('neff_stop_top10')):
        put('top10', ZERO, ZERO, f'N_eff {st.n_eff} below {p.int("neff_stop_top10")}')

    if (debtor_grade or 'E') in ('C', 'D'):
        cd_used = sum((amt for did, amt in st.by_debtor.items()
                       if (st.debtor_meta.get(did, {}).get('grade') or 'C') in ('C', 'D') and did is not None),
                      ZERO)
        if st.small_book:
            put('grade_mix', p.dec('small_book_cd_cap'), cd_used, 'C/D absolute cap (small book)')
        else:
            share = p.dec('grade_cd_share_cap')
            # (cd + x) / (total + x) <= share  =>  x <= (share*total - cd) / (1 - share)
            room = (share * st.committed - cd_used) / (Decimal('1') - share)
            put('grade_mix', cd_used + max(ZERO, room), cd_used, f'C/D share <= {share * 100:.0f}% of book')
    out['advance_brake_pp'] = brake
    out['ticket_cap'] = ticket_cap
    return out


SCOPE_REASON = {'pot': 'L-POT', 'debtor': 'L-DEBTOR', 'transporter': 'L-TRANSPORTER', 'pair': 'L-PAIR',
                'sector': 'L-SECTOR', 'top10': 'L-TOP10', 'grade_mix': 'L-GRADE-MIX'}


def binding(headrooms: dict, wanted: Decimal) -> tuple[Decimal, str]:
    """Fundable amount after every scope and the scope that binds (or '')."""
    fundable, scope = _q(wanted), ''
    for name, h in headrooms.items():
        if not isinstance(h, dict):
            continue
        if h['headroom'] < fundable:
            fundable, scope = h['headroom'], name
    return max(ZERO, fundable), scope


# ---------------------------------------------------------------------------
# Risk: expected loss, stress, risk index
# ---------------------------------------------------------------------------

def pd_horizon(pd_12m, dtp_days) -> Decimal:
    """PD over the invoice's expected life (DTP + 30 days)."""
    pd = float(pd_12m or 0)
    h = (float(dtp_days or 60) + 30) / 365.0
    return Decimal(str(round(1 - (1 - pd) ** h, 6)))


def _live_face_and_holdback(funder) -> tuple[Decimal, Decimal]:
    from core.models import AdvanceRequest
    face = hold = ZERO
    from django.db.models import Q
    for adv in (AdvanceRequest.objects.filter(Q(funder=funder) | Q(facility__funder=funder),
                                              status__in=('REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED'))
                .select_related('invoice').only('amount', 'invoice__total_amount', 'invoice__credited_amount')):
        f = _q(adv.invoice.total_amount) - _q(getattr(adv.invoice, 'credited_amount', 0))
        face += f
        hold += max(ZERO, f - _q(adv.amount))
    return face, hold


def risk_summary(st: BookState) -> dict:
    p = st.policy
    el_amount = ZERO
    for did, amt in st.by_debtor.items():
        if amt <= 0:
            continue
        m = st.debtor_meta.get(did, {}) if did else {}
        grade = m.get('grade') or 'C'
        pd = m.get('pd') if m.get('pd') is not None else p.representative_pd(grade)
        el_amount += amt * pd_horizon(pd, m.get('dtp')) * p.dec('lgd', grade)
    el_amount = _q(el_amount)
    el_pct = _pct(el_amount, st.committed) * 100 if st.committed else Decimal('0')
    # Fraud and dilution reserves on top of credit EL (design §3.4 example: ~0.2%).
    book_el_pct = el_pct + (p.dec('fraud_reserve_pct', 'V2') + p.dec('dilution_reserve_pct')) / 2 \
        if st.committed else Decimal('0')

    named = sorted(((d, a) for d, a in st.by_debtor.items() if d is not None and a > 0), key=lambda x: -x[1])
    top1 = named[0][1] if named else ZERO
    top3 = sum((a for _, a in named[:3]), ZERO)
    biggest_transporter = max(st.by_company.values(), default=ZERO)
    face, holdbacks = _live_face_and_holdback(st.funder)
    protection = holdbacks + _q(st.funder.first_loss_amount) + _q(st.funder.insurance_cover)

    def status(loss):
        if loss <= protection:
            return 'ok'
        return 'watch' if loss <= protection * Decimal('1.5') else 'breach'

    stress = [
        {'scenario': 'Largest debtor defaults (90% loss)', 'loss': _q(top1 * D('0.90')), 'protection': protection},
        {'scenario': 'Top-3 debtors default (80% loss)', 'loss': _q(top3 * D('0.80')), 'protection': protection},
        {'scenario': 'Dilution spike (8% of funded invoice face)', 'loss': _q(face * D('0.08')),
         'protection': holdbacks},
        {'scenario': 'Largest transporter fraud (100% of its exposure)', 'loss': _q(biggest_transporter),
         'protection': protection},
    ]
    for s in stress:
        s['status'] = status(s['loss']) if s['scenario'][:8] != 'Dilution' else (
            'ok' if s['loss'] <= s['protection'] else 'watch')
        s['note'] = ''
    stress.append({'scenario': 'Customers pay 30 days later', 'loss': ZERO, 'protection': protection,
                   'status': 'watch' if st.committed else 'ok',
                   'note': 'No direct loss; headroom shrinks and fees accrue for longer'})

    ri = p.params['risk_index']
    conc = (D(str(ri['lambda_hhi'])) * max(D('0'), st.hhi - D('0.05'))
            + D(str(ri['lambda_top10'])) * max(D('0'), st.top10_share - p.dec('top10_soft_pct'))
            + D(str(ri['lambda_top1'])) * max(D('0'), st.top1_share - D('0.15')))
    top3_loss = stress[1]['loss']
    stress_ratio = (top3_loss / protection) if protection > 0 else (D('99') if top3_loss > 0 else D('0'))
    index = None
    band = None
    if st.committed > 0:
        el_term = min(D('1'), book_el_pct / D(str(ri['el_norm_pct'])))
        conc_term = min(D('1'), conc)
        stress_term = min(D('1'), stress_ratio / D(str(ri['stress_norm'])))
        index = (D('100') - 40 * el_term - 30 * conc_term - 30 * stress_term).quantize(D('0.1'))
        band = 'green' if index >= ri['green'] else ('amber' if index >= ri['amber'] else 'red')
    note = ('Summary only: the limits are what bind.'
            + (' Small book: concentration is high by construction until outstanding passes R'
               f'{p.dec("small_book_threshold"):,.0f}.' if st.small_book and st.committed else '')
            + ('' if protection > 0 or not st.committed else
               ' No first-loss or insurance recorded, so any default counts against the index in full.'))
    return {
        'expected_loss': {'amount': el_amount, 'pct': el_pct.quantize(D('0.001'))},
        'book_el_pct': book_el_pct.quantize(D('0.001')),
        'stress': stress,
        'protection': protection,
        'risk_index': {'value': index, 'band': band,
                       'components': {'el_pct': book_el_pct.quantize(D('0.001')),
                                      'concentration_penalty': conc.quantize(D('0.001')),
                                      'stress_ratio': stress_ratio.quantize(D('0.001'))},
                       'note': note},
    }


def overview(funder, *, policy: Policy | None = None) -> dict:
    """The Book payload for the capital desk and the funder API."""
    from core.models import AdvanceRequest, CapitalAlert, Company, CapitalScore, Facility
    st = load_state(funder, policy)
    p = st.policy
    risk = risk_summary(st)

    sectors = []
    for sector, amt in sorted(st.by_sector.items(), key=lambda x: -x[1]):
        cap, _ = sector_cap(st, sector)
        sectors.append({'sector': sector, 'label': sector_label(sector), 'exposure': amt,
                        'pct_of_pot': (_pct(amt, st.pot) * 100).quantize(D('0.1')), 'cap': cap})
    grades = {}
    for did, amt in st.by_debtor.items():
        g = (st.debtor_meta.get(did, {}).get('grade') if did else None) or 'Unscored'
        grades[g] = grades.get(g, ZERO) + amt
    grade_rows = [{'grade': g, 'exposure': grades[g],
                   'pct': (_pct(grades[g], st.committed) * 100).quantize(D('0.1'))}
                  for g in list(GRADES) + ['Unscored'] if g in grades]

    top = []
    for did, amt in sorted(((d, a) for d, a in st.by_debtor.items() if d is not None),
                           key=lambda x: -x[1])[:15]:
        m = st.debtor_meta.get(did, {})
        cap, _ = debtor_cap(st, did, m.get('grade'), bool(m.get('cold_start')))
        top.append({'debtor_id': did, 'name': m.get('name', f'Debtor {did}'), 'grade': m.get('grade'),
                    'sector': m.get('sector', 'UNKNOWN'), 'exposure': amt, 'cap': cap,
                    'utilisation_pct': (_pct(amt, cap) * 100).quantize(D('0.1')) if cap else None,
                    'hold': bool(m.get('hold'))})
    if None in st.by_debtor:
        top.append({'debtor_id': None, 'name': 'Unidentified debtors (legacy)', 'grade': None,
                    'sector': 'UNKNOWN', 'exposure': st.by_debtor[None], 'cap': ZERO,
                    'utilisation_pct': None, 'hold': False})

    companies = {c.pk: c for c in Company.objects.filter(pk__in=[k for k in st.by_company if k])}
    lines = {f.company_id: f for f in Facility.objects.filter(funder=funder, status='ACTIVE')}
    tscores = {}
    for s in CapitalScore.objects.filter(kind='TRANSPORTER', company_id__in=list(companies)).order_by(
            '-created_at', '-id'):
        tscores.setdefault(s.company_id, s.grade)
    transporters = []
    for cid, amt in sorted(st.by_company.items(), key=lambda x: -x[1]):
        line = lines.get(cid)
        limit = _q(line.limit) if line else ZERO
        transporters.append({'company_id': cid, 'name': getattr(companies.get(cid), 'company_name', f'#{cid}'),
                             'grade': tscores.get(cid), 'exposure': amt, 'line_limit': limit,
                             'utilisation_pct': (_pct(amt, limit) * 100).quantize(D('0.1')) if limit else None})

    alerts = CapitalAlert.objects.filter(funder=funder, resolved_at__isnull=True)
    queued = AdvanceRequest.objects.filter(funder=funder, status='QUEUED')
    pending = AdvanceRequest.objects.filter(funder=funder, status__in=('REQUESTED', 'SCORING'))

    def agg(qs):
        return {'count': qs.count(), 'amount': _q(sum((a.amount for a in qs.only('amount')), ZERO))}

    return {
        'funder': {'id': funder.pk, 'name': funder.name, 'status': funder.status,
                   'operating_mode': funder.operating_mode, 'pot_limit': _q(funder.pot_limit),
                   'cost_of_funds_pct': funder.cost_of_funds_pct, 'recourse': funder.recourse},
        'policy_version': p.version,
        'as_of': timezone.now().isoformat(),
        'pot_limit': st.pot, 'outstanding': st.outstanding, 'reserved': st.reserved,
        'committed': st.committed, 'headroom': st.headroom,
        'utilisation_pct': (_pct(st.committed, st.pot) * 100).quantize(D('0.1')) if st.pot else D('0'),
        'small_book': st.small_book,
        'risk_index': risk['risk_index'],
        'concentration': {'hhi': st.hhi.quantize(D('0.0001')), 'n_eff': st.n_eff,
                          'top1_pct': (st.top1_share * 100).quantize(D('0.1')),
                          'top10_pct': (st.top10_share * 100).quantize(D('0.1')),
                          'top10_band': st.top10_band},
        'sectors': sectors,
        'grades': grade_rows,
        'top_debtors': top,
        'transporters': transporters,
        'stress': risk['stress'],
        'expected_loss': risk['expected_loss'],
        'alerts_open': {sev.lower(): alerts.filter(severity=sev).count() for sev in ('RED', 'AMBER', 'INFO')},
        'queue': agg(queued),
        'pending_approvals': agg(pending),
    }


def concentration_metrics(funder) -> dict:
    """Lightweight metrics for monitoring and snapshots."""
    st = load_state(funder)
    risk = risk_summary(st)
    return {'state': st, 'risk': risk}
