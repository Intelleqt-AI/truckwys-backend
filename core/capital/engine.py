"""The one Fast Pay decision path.

``evaluate(invoice)`` -> an ``Evaluation`` with the decision (FUND / PART_FUND /
QUEUE / REFER / DECLINE), sizing, fee, reasons and the scores and book state it
used. ``request(invoice, actor)`` evaluates again *under the funder row lock*,
records the immutable ``InvoiceAssessment`` and opens the advance (reserving
capacity) in the same transaction, so two concurrent requests can never both
use the last rand of a cap.

Used by every channel: the transporter Fast Pay API, the old ``/advances/``
create, ``/capital/eligible/``, the lender API, and the queue job. Nothing else
computes a fee or an advance amount.

Stages (docs/capital-risk/03-design.md §2.3, §2.4, §3.5, §4):
1. eligibility rules (hard fail -> DECLINE; V1 POD -> REFER),
2. debtor + transporter scores, verification tier, fraud checks,
3. advance rate -> eligible amount -> headroom across every limit,
4. expected days-to-pay, expected loss -> invoice grade -> fee,
5. routing (refer / decline rules, Mode A approval, Mode B envelope).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.capital import book as bookmod
from core.capital.policy import Policy, grade_at_least, policy_for_funder, REQUIRED_CONSENTS
from core.capital.reasons import reason

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
D = Decimal
MODEL_VERSION = 'fp-engine-1.0'
FUNDABLE_INVOICE_STATUSES = ('SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID')
LIVE_STATUSES = ('QUEUED', 'REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED')
STATUS_LABELS = {'DRAFT': 'a draft', 'PAID': 'paid', 'CANCELLED': 'void', 'DISPUTED': 'disputed',
                 'CREDITED': 'credited'}


def _q(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class NotLaunched(Exception):
    """Fast Pay requests are switched off for this company."""


class DemoCompany(Exception):
    """A demo company cannot request: no money moves."""


@dataclass
class Evaluation:
    invoice: object
    company: object
    policy: Policy
    facility: object = None
    funder: object = None
    debtor: object = None
    debtor_score: object = None
    transporter_score: object = None
    decision: str = 'DECLINE'
    eligible: bool = False
    eligibility: list = field(default_factory=list)
    soft_refer: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    verification_tier: str = 'V0'
    fraud_score: Decimal = D('0')
    invoice_total: Decimal = ZERO
    invoice_balance: Decimal = ZERO
    requested_amount: Decimal | None = None
    advance_rate_pct: Decimal = D('0')
    eligible_amount: Decimal = ZERO
    fundable_amount: Decimal = ZERO
    queued_amount: Decimal = ZERO
    binding_limit: str = ''
    headroom: dict = field(default_factory=dict)
    expected_dtp_days: Decimal | None = None
    expected_payment_date: object = None
    pd_horizon: Decimal = D('0')
    el_pct: Decimal = D('0')
    invoice_grade: str = ''
    fee_pct: Decimal = D('0')
    fee_amount: Decimal = ZERO
    fee_vat_amount: Decimal = ZERO
    fee_breakdown: dict = field(default_factory=dict)
    net_payout: Decimal = ZERO
    holdback_amount: Decimal = ZERO
    reasons: list = field(default_factory=list)
    explanation: str = ''
    explanation_source: str = 'TEMPLATE'
    auto_approve: bool = False
    book_band: str | None = None
    live_advance: object = None
    demo: bool = False

    # -- helpers --------------------------------------------------------------
    def fail(self, rule: str, code: str, **params):
        self.eligibility.append({'rule': rule, 'passed': False, 'code': code,
                                 'text': reason(code, **params)['text']})
        self.reasons.append(reason(code, **params))

    def ok(self, rule: str):
        self.eligibility.append({'rule': rule, 'passed': True, 'code': '', 'text': ''})

    @property
    def hard_failed(self) -> bool:
        return any(not r['passed'] for r in self.eligibility)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def line_for(company):
    from core.models import Facility
    if company is None:
        return None
    return (Facility.objects.select_related('funder').filter(company=company, status='ACTIVE')
            .order_by('-created_at').first())


def application_for(company):
    from core.models import CapitalApplication
    if company is None:
        return None
    return CapitalApplication.objects.filter(company=company).first()


def can_request(company) -> bool:
    """Launch switch: requests are allowed once launched, or for pilot companies."""
    if company is None:
        return False
    if getattr(settings, 'CAPITAL_LAUNCHED', False):
        return True
    return company.pk in tuple(getattr(settings, 'CAPITAL_PILOT_COMPANY_IDS', ()) or ())


# ---------------------------------------------------------------------------
# Stage 1: eligibility
# ---------------------------------------------------------------------------

def _delivered_on(load):
    when = getattr(load, 'actual_delivered_at', None) or getattr(load, 'delivery_date', None)
    if when is None:
        return None
    return timezone.localtime(when).date() if hasattr(when, 'tzinfo') and when.tzinfo else (
        when.date() if hasattr(when, 'date') else when)


def _cross_ageing_share(debtor, funder) -> Decimal:
    """Share of this debtor's funded balance whose invoices are > 60 days past due."""
    from core.models import AdvanceRequest
    if debtor is None:
        return D('0')
    cutoff = timezone.localdate() - timedelta(days=60)
    total = late = ZERO
    for adv in AdvanceRequest.objects.filter(debtor=debtor, status='DISBURSED').select_related('invoice'):
        total += adv.amount
        if adv.invoice.due_date and adv.invoice.due_date < cutoff:
            late += adv.amount
    return (late / total) if total else D('0')


def _eligibility(ev: Evaluation, *, ignore_advance=None):
    from core.models import AdvanceRequest
    inv, p, company = ev.invoice, ev.policy, ev.company
    today = timezone.localdate()

    # Line, funder, demo
    if ev.facility is None:
        ev.fail('line', 'E-NO-LINE')
    elif ev.funder is None:
        ev.fail('funder', 'E-NO-FUNDER')
    elif not ev.funder.accepts_new_advances:
        ev.fail('funder', 'E-FUNDER-PAUSED')
    else:
        ev.ok('line')
    if getattr(company, 'is_demo', False):
        ev.demo = True
        ev.fail('demo', 'E-DEMO')

    # Invoice
    if inv.status not in FUNDABLE_INVOICE_STATUSES:
        ev.fail('status', 'E-STATUS', status=inv.status, status_label=STATUS_LABELS.get(inv.status, inv.status.lower()))
    else:
        ev.ok('status')
    if ev.invoice_balance <= 0:
        ev.fail('balance', 'E-BALANCE')
    live_qs = AdvanceRequest.objects.filter(invoice=inv, status__in=LIVE_STATUSES)
    if ignore_advance is not None:
        live_qs = live_qs.exclude(pk=ignore_advance.pk)
    live = live_qs.order_by('-created_at').first()
    ev.live_advance = live
    if live is not None:
        ev.fail('already', 'E-ALREADY')
    if inv.status == 'DISPUTED' or _q(getattr(inv, 'credited_amount', 0)) > 0:
        ev.fail('dispute', 'E-DISPUTE')
    if inv.issue_date and (today - inv.issue_date).days > p.int('max_invoice_age_days'):
        ev.fail('invoice_age', 'E-INVOICE-AGE', days=(today - inv.issue_date).days,
                limit=p.int('max_invoice_age_days'))

    # Load + delivery evidence
    load = getattr(inv, 'load', None)
    if load is None:
        ev.fail('load', 'E-NO-LOAD')
    else:
        if load.status not in ('DELIVERED', 'INVOICED'):
            ev.fail('delivered', 'E-NOT-DELIVERED', load=load.load_number)
        delivered = _delivered_on(load)
        if delivered and (today - delivered).days > p.int('max_days_since_delivery'):
            ev.fail('delivery_age', 'E-DELIVERY-AGE', days=(today - delivered).days,
                    limit=p.int('max_days_since_delivery'))
        load_value = _q(load.total_amount)
        if load_value > 0 and _q(inv.subtotal) > load_value * (1 + p.dec('invoice_load_tolerance_pct')):
            ev.fail('amount_match', 'E-AMOUNT-MISMATCH',
                    tolerance=(p.dec('invoice_load_tolerance_pct') * 100).normalize())
    from core.capital.verification import verification_tier, fraud_checks
    vt = verification_tier(load, p) if load is not None else {'tier': 'V0', 'reasons': [], 'details': {}}
    ev.verification_tier = vt['tier']
    ev.checks['verification'] = vt.get('details', {})
    min_tier = p.get('min_verification_tier', 'V2')
    if ev.verification_tier == 'V0':
        ev.fail('pod', 'E-POD-V0')
    elif ev.verification_tier < min_tier:  # 'V1' < 'V2'
        ev.soft_refer.append(reason('E-POD-V1'))
        ev.reasons.extend(r for r in vt.get('reasons', []) if r['code'] != 'E-POD-V1')
    else:
        ev.reasons.extend(vt.get('reasons', []))

    fc = fraud_checks(inv, p)
    ev.fraud_score = D(str(fc.get('score', 0))).quantize(D('0.001'))
    ev.checks['fraud'] = {'score': str(ev.fraud_score), 'flags': [f['code'] for f in fc.get('flags', [])],
                          'duplicate': fc.get('duplicate', False), 'detail': fc.get('duplicate_detail', '')}
    if fc.get('duplicate'):
        ev.fail('duplicate', 'E-DUPLICATE', detail=fc.get('duplicate_detail', ''))
    ev.reasons.extend(f for f in fc.get('flags', []) if f['code'] != 'E-DUPLICATE')

    # Debtor
    customer = inv.customer
    debtor = ev.debtor
    if debtor is None:
        if p.get('require_debtor_identity', True):
            ev.fail('debtor_identity', 'E-DEBTOR-UNIDENTIFIED')
    else:
        if p.get('exclude_government', True) and (debtor.is_government or debtor.sector == 'GOVERNMENT'):
            ev.fail('debtor_government', 'E-DEBTOR-GOVERNMENT')
        if p.get('exclude_foreign', True) and debtor.is_foreign:
            ev.fail('debtor_foreign', 'E-DEBTOR-FOREIGN')
        if debtor.on_hold:
            ev.fail('debtor_hold', 'E-DEBTOR-HOLD', reason=debtor.hold_reason or 'capital desk hold')
        if debtor.cession_status == 'PROHIBITED':
            ev.fail('cession', 'E-DEBTOR-CESSION')
        share = _cross_ageing_share(debtor, ev.funder)
        if share > p.dec('cross_ageing_max_pct'):
            ev.fail('cross_ageing', 'E-CROSS-AGEING', pct=int(share * 100))
    if p.get('exclude_foreign', True) and (getattr(customer, 'country', 'ZA') or 'ZA').upper() != 'ZA' \
            and not (debtor and debtor.is_foreign):
        ev.fail('debtor_foreign', 'E-DEBTOR-FOREIGN')

    # Transporter application, consents, insurance, hold
    app = application_for(company)
    if p.get('require_application_approved', True):
        if app is None or app.status != 'APPROVED':
            ev.fail('application', 'E-APPLICATION',
                    status=(app.get_status_display() if app else 'Not started'))
    if app is not None:
        missing = [c for c in REQUIRED_CONSENTS if c not in app.consent_purposes()]
        if missing:
            ev.fail('consent', 'E-CONSENT', missing=', '.join(missing))
        if app.on_hold:
            ev.fail('transporter_hold', 'E-TRANSPORTER-HOLD', reason=app.hold_reason or 'capital desk hold')
        if p.get('require_git_insurance', True) and (
                app.git_insurance_expiry is None or app.git_insurance_expiry < today):
            ev.fail('git_insurance', 'E-GIT')
    elif p.get('require_application_approved', True):
        pass  # already failed above
    if ev.funder is not None:
        st_hold = bookmod._latest_limits(ev.funder).get(('TRANSPORTER', None, company.pk, ''))
        if st_hold is not None and st_hold.hold:
            ev.fail('transporter_hold', 'E-TRANSPORTER-HOLD', reason=st_hold.reason)
        if debtor is not None:
            d_hold = bookmod._latest_limits(ev.funder).get(('DEBTOR', debtor.pk, None, ''))
            if d_hold is not None and d_hold.hold:
                ev.fail('debtor_hold', 'E-DEBTOR-HOLD', reason=d_hold.reason)


# ---------------------------------------------------------------------------
# Stages 2-5
# ---------------------------------------------------------------------------

def _scores(ev: Evaluation):
    from core.capital.scoring import current_debtor_score, current_transporter_score
    if ev.debtor is not None:
        ev.debtor_score = current_debtor_score(ev.debtor, ev.policy)
    ev.transporter_score = current_transporter_score(ev.company, ev.policy)
    if ev.debtor_score is not None and (ev.debtor_score.grade == 'E' or ev.debtor_score.hard_stop):
        ev.fail('debtor_grade', 'E-DEBTOR-E')
        ev.reasons.extend(r for r in ev.debtor_score.reason_codes if r.get('direction') == '!')
    if ev.transporter_score is not None and (ev.transporter_score.grade == 'E' or ev.transporter_score.hard_stop):
        ev.fail('transporter_grade', 'E-TRANSPORTER-E')


def _advance_rate(ev: Evaluation, pair: dict, brake_pp: Decimal) -> Decimal:
    p = ev.policy
    dg = ev.debtor_score.grade if ev.debtor_score else 'E'
    tg = ev.transporter_score.grade if ev.transporter_score else 'E'
    rate = p.dec('base_advance_pct', dg) + p.dec('transporter_adj_pp', tg)
    rate += D(str(p.params['verification_adj_pp'].get(ev.verification_tier, '-100')))
    if (pair.get('paid_n') or 0) < p.int('new_pair_paid_invoices'):
        rate += p.dec('new_pair_adj_pp')
        ev.reasons.append(reason('P-NEW-PAIR', n=p.int('new_pair_paid_invoices')))
    elif pair.get('avg_dtp') is not None:
        ev.reasons.append(reason('P-HIST', n=pair['paid_n'], days=int(round(float(pair['avg_dtp'])))))
    rate -= brake_pp
    # Holdback floor: max(10%, the transporter's dilution reserve).
    from core.capital.scoring.transporter import dilution_reserve_pct
    try:
        dr = D(str(dilution_reserve_pct(ev.company, p)))
    except Exception:  # never let a reserve calculation crash a decision
        logger.exception('dilution reserve failed for company %s', ev.company.pk)
        dr = p.dec('min_holdback_pct')
    holdback_pct = max(p.dec('min_holdback_pct'), dr)
    ev.fee_breakdown['holdback_floor_pct'] = str(holdback_pct.quantize(D('0.01')))
    rate = min(rate, D('100') - holdback_pct)
    return max(D('0'), rate).quantize(D('0.01'))


def _price(ev: Evaluation, pair_share: Decimal):
    """Expected loss and fee (design §2.3 stage 3 and the §4 worked sample)."""
    from core.capital.scoring.debtor import expected_dtp
    p = ev.policy
    if ev.debtor is not None:
        dtp, basis = expected_dtp(ev.debtor, p, company=ev.company)
    else:
        dtp, basis = D(str(p.params['dtp_prior_days']['UNKNOWN'])), {'prior': 'UNKNOWN'}
    ev.expected_dtp_days = D(str(dtp)).quantize(D('0.1'))
    ev.checks['dtp_basis'] = basis
    # The advance is outstanding only for the days still to run: expected
    # days-to-pay from issue, less the invoice's age, at least a week.
    today = timezone.localdate()
    issue = ev.invoice.issue_date or today
    age = max(0, (today - issue).days)
    remaining = max(D('7'), ev.expected_dtp_days - D(age))
    ev.fee_breakdown['funding_days'] = str(remaining)
    ev.expected_payment_date = today + timedelta(days=int(round(float(remaining))))

    dg = ev.debtor_score.grade if ev.debtor_score else 'E'
    pd12 = D(str(ev.debtor_score.pd_12m)) if ev.debtor_score else p.representative_pd('E')
    ev.pd_horizon = bookmod.pd_horizon(pd12, remaining)
    lgd = p.dec('lgd', dg)
    el_nonrec = ev.pd_horizon * lgd
    tpd = D(str(ev.transporter_score.pd_12m)) if ev.transporter_score else D('1')
    q = max(min(D('1'), tpd * 3), pair_share, p.dec('recourse_q_floor'))
    if pair_share > p.dec('recourse_q_dependency_share'):
        q = D('1')
    el_rec = el_nonrec * q
    recourse = getattr(ev.funder, 'recourse', 'TBD')
    el_credit = el_nonrec if recourse == 'NON_RECOURSE' else el_rec
    el_credit_pct = el_credit * 100
    dil_pct = p.dec('dilution_reserve_pct')
    fraud_pct = D(str(p.params['fraud_reserve_pct'].get(ev.verification_tier, '1.00')))
    el_pct = el_credit_pct + dil_pct + fraud_pct
    ev.el_pct = el_pct.quantize(D('0.0001'))
    grade = 'I-E'
    for g, upper in p.params['invoice_grade_bands']:
        if ev.el_pct < D(str(upper)):
            grade = g
            break
    ev.invoice_grade = grade

    cof = D(str(getattr(ev.funder, 'cost_of_funds_pct', None) or '13.5'))
    funding_pct = cof * remaining / D('365')
    opex, platform, margin = p.dec('opex_pct'), p.dec('platform_fee_pct'), p.dec('funder_margin_pct')
    fee_pct = funding_pct + el_pct + opex + platform + margin
    fee_pct = min(max(fee_pct, p.dec('min_fee_pct')), p.dec('max_fee_pct'))
    ev.fee_pct = fee_pct.quantize(D('0.001'))
    ev.fee_breakdown.update({
        'funding_pct': str(funding_pct.quantize(D('0.001'))),
        'cost_of_funds_pct': str(cof), 'expected_dtp_days': str(ev.expected_dtp_days),
        'credit_el_pct': str(el_credit_pct.quantize(D('0.001'))),
        'credit_el_non_recourse_pct': str((el_nonrec * 100).quantize(D('0.001'))),
        'credit_el_recourse_pct': str((el_rec * 100).quantize(D('0.001'))),
        'recourse_q': str(q.quantize(D('0.01'))), 'recourse_basis': recourse,
        'dilution_reserve_pct': str(dil_pct), 'fraud_reserve_pct': str(fraud_pct),
        'opex_pct': str(opex), 'platform_fee_pct': str(platform), 'funder_margin_pct': str(margin),
        'pd_12m': str(pd12), 'pd_horizon': str(ev.pd_horizon), 'lgd': str(lgd),
        'vat_note': 'VAT at 15% applies to the platform-fee part only; the funding charge is a financial service.',
    })


def _amounts(ev: Evaluation):
    p = ev.policy
    amt = ev.fundable_amount
    ev.fee_amount = _q(amt * ev.fee_pct / 100)
    ev.fee_vat_amount = _q(amt * p.dec('platform_fee_pct') / 100 * p.dec('platform_fee_vat_rate'))
    ev.net_payout = _q(amt - ev.fee_amount - ev.fee_vat_amount)
    ev.holdback_amount = _q(max(ZERO, ev.invoice_balance - amt)) if amt > 0 else ZERO


def _route(ev: Evaluation, book_band: str | None):
    p = ev.policy
    dg = ev.debtor_score.grade if ev.debtor_score else 'E'
    tg = ev.transporter_score.grade if ev.transporter_score else 'E'
    if ev.hard_failed:
        ev.decision, ev.eligible = 'DECLINE', False
        return
    if ev.eligible_amount <= 0:
        # Nothing can be advanced on this invoice at this rate (e.g. the
        # adjustments take the rate to zero): decline, with the EL reason.
        ev.decision, ev.eligible = 'DECLINE', False
        ev.reasons.append(reason('R-DECLINE-EL', el=ev.el_pct.quantize(D('0.01'))))
        return
    ev.eligible = True
    if ev.fraud_score >= p.dec('decline_fraud_score'):
        ev.decision = 'DECLINE'
        ev.reasons.append(reason('R-DECLINE-FRAUD', score=ev.fraud_score))
        return
    if ev.invoice_grade in p.get('decline_invoice_grades', []):
        ev.decision = 'DECLINE'
        ev.reasons.append(reason('R-DECLINE-EL', el=ev.el_pct.quantize(D('0.01'))))
        return

    refer = list(ev.soft_refer)
    if dg in p.get('refer_debtor_grades', []):
        refer.append(reason('R-REFER-GRADE', party='debtor', grade=dg))
    if tg in p.get('refer_transporter_grades', []):
        refer.append(reason('R-REFER-GRADE', party='transporter', grade=tg))
    if ev.invoice_grade in p.get('refer_invoice_grades', []):
        refer.append(reason('R-REFER-INVOICE', grade=ev.invoice_grade, el=ev.el_pct.quantize(D('0.01'))))
    if ev.fraud_score >= p.dec('refer_fraud_score'):
        refer.append(reason('R-REFER-FRAUD', score=ev.fraud_score))
    if ev.eligible_amount > p.dec('refer_amount_over'):
        refer.append(reason('R-REFER-AMOUNT', limit=f"{p.dec('refer_amount_over'):,.0f}"))
    if book_band == 'red':
        ev.reasons.append(reason('R-BOOK-RED', index=ev.checks.get('risk_index')))
        if p.get('red_index_refers_all', False):
            refer.append(reason('R-BOOK-RED', index=ev.checks.get('risk_index')))

    threshold = min(ev.eligible_amount,
                    max(p.dec('min_fundable_amount'), _q(ev.eligible_amount * p.dec('min_fundable_share'))))
    if ev.eligible_amount <= 0 or ev.fundable_amount <= 0 or ev.fundable_amount < threshold:
        ev.decision = 'QUEUE'
        ev.queued_amount = ev.eligible_amount
        ev.fundable_amount = ZERO
        ev.reasons.append(reason('L-QUEUED', amount=f'{ev.queued_amount:,.0f}'))
        return
    if refer:
        ev.decision = 'REFER'
        ev.reasons.extend(refer)
    elif ev.fundable_amount >= ev.eligible_amount:
        ev.decision = 'FUND'
        ev.reasons.append(reason('R-FUND'))
    else:
        ev.decision = 'PART_FUND'
    if ev.fundable_amount < ev.eligible_amount:
        ev.queued_amount = _q(ev.eligible_amount - ev.fundable_amount)
        ev.reasons.append(reason('L-QUEUED', amount=f'{ev.queued_amount:,.0f}'))

    # Approval route: Mode A by default; Mode B only inside the signed envelope.
    env = p.params.get('auto_approve', {})
    ev.auto_approve = bool(
        ev.decision in ('FUND', 'PART_FUND')
        and getattr(settings, 'CAPITAL_AUTO_APPROVE_ENABLED', False)
        and ev.funder is not None and ev.funder.operating_mode == 'B' and ev.funder.auto_approve_enabled
        and book_band != 'red'
        and ev.fundable_amount <= D(str(env.get('max_amount', '0')))
        and grade_at_least(dg, env.get('min_debtor_grade', 'C'))
        and grade_at_least(tg, env.get('min_transporter_grade', 'C'))
        and ev.invoice_grade in env.get('invoice_grades', [])
        and ev.verification_tier >= env.get('min_tier', 'V3')
        and ev.fraud_score <= D(str(env.get('max_fraud_score', '0')))
    )
    ev.reasons.append(reason('R-AUTO' if ev.auto_approve else 'R-MODE-A'))


def evaluate(invoice, *, requested_amount=None, state: bookmod.BookState | None = None,
             ignore_advance=None) -> Evaluation:
    """Evaluate one invoice against the book as it stands. Writes no ledger rows.

    Scores may be computed and stored (``CapitalScore`` rows are history, not
    decisions). Pass ``state`` when the caller already loaded the book under
    the funder lock.
    """
    company = invoice.company
    facility = line_for(company)
    funder = getattr(facility, 'funder', None)
    policy = state.policy if state is not None else policy_for_funder(funder)
    customer = getattr(invoice, 'customer', None)
    ev = Evaluation(invoice=invoice, company=company, policy=policy, facility=facility, funder=funder,
                    debtor=getattr(customer, 'debtor_identity', None) if customer else None)
    ev.invoice_total = _q(invoice.total_amount)
    ev.invoice_balance = _q(invoice.balance if invoice.balance is not None else invoice.total_amount)
    ev.requested_amount = _q(requested_amount) if requested_amount not in (None, '') else None

    _eligibility(ev, ignore_advance=ignore_advance)
    _scores(ev)

    pair = {}
    pair_share = D('0')
    if ev.debtor is not None:
        from core.capital.scoring.debtor import pair_features
        from core.capital.scoring.transporter import receivables_share
        pair = pair_features(company, ev.debtor)
        pair_share = D(str(receivables_share(company, ev.debtor)))

    brake = D('0')
    book_band = None
    if funder is not None and facility is not None:
        st = state or bookmod.load_state(funder, policy)
        risk = bookmod.risk_summary(st)
        book_band = risk['risk_index']['band']
        ev.book_band = book_band
        ev.checks['risk_index'] = str(risk['risk_index']['value']) if risk['risk_index']['value'] is not None else None
        hr = bookmod.headroom(
            st, company_id=company.pk, line_limit=facility.limit,
            debtor_id=ev.debtor.pk if ev.debtor else None,
            debtor_grade=ev.debtor_score.grade if ev.debtor_score else None,
            debtor_cold_start=bool(getattr(ev.debtor_score, 'cold_start', True)),
            transporter_grade=ev.transporter_score.grade if ev.transporter_score else None,
            sector=getattr(ev.debtor, 'sector', 'UNKNOWN'))
        brake = hr.pop('advance_brake_pp', D('0'))
        hr.pop('ticket_cap', None)
        # The line's own cached figures are a second, independent bound (they
        # also hold any pre-ledger exposure the reconciliation is flagging).
        t = hr['transporter']
        t['headroom'] = max(ZERO, min(t['headroom'], _q(facility.available)))
        ev.headroom = hr

    ev.advance_rate_pct = _advance_rate(ev, pair, brake)
    if brake:
        ev.reasons.append(reason('L-TOP10', pct=ev.headroom.get('top10', {}).get('source', ''),
                                 detail=f'-{brake}pp advance rate', amount='', headroom=''))
    base = ev.invoice_balance
    ev.eligible_amount = _q(base * ev.advance_rate_pct / 100)
    if ev.requested_amount is not None:
        ev.eligible_amount = min(ev.eligible_amount, ev.requested_amount)
    if ev.headroom:
        ev.fundable_amount, ev.binding_limit = bookmod.binding(ev.headroom, ev.eligible_amount)
    else:
        ev.fundable_amount = ZERO
    if ev.binding_limit:
        h = ev.headroom[ev.binding_limit]
        code = bookmod.SCOPE_REASON[ev.binding_limit]
        ev.reasons.append(reason(code, headroom=f"{h['headroom']:,.0f}", amount=f'{ev.fundable_amount:,.0f}',
                                 pct=h.get('source', ''), detail=h.get('source', '')))

    _price(ev, pair_share)
    _route(ev, book_band)
    if ev.decision == 'DECLINE':
        ev.fundable_amount = ZERO
        ev.queued_amount = ZERO
    _amounts(ev)
    _order_reasons(ev)
    from core.capital import explain
    ev.explanation, ev.explanation_source = explain.explain(ev)
    return ev


def _order_reasons(ev: Evaluation):
    order = {'!': 0, '-': 1, '+': 2}
    seen, out = set(), []
    for r in sorted(ev.reasons, key=lambda r: order.get(r.get('direction'), 3)):
        key = (r['code'], r['text'])
        if key not in seen:
            seen.add(key)
            out.append(r)
    ev.reasons = out


# ---------------------------------------------------------------------------
# Persisting decisions and opening advances
# ---------------------------------------------------------------------------

def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def record(ev: Evaluation, *, purpose: str = 'OFFER', actor=None, actor_label: str = ''):
    """Write the immutable InvoiceAssessment for an evaluation."""
    from core.models import InvoiceAssessment
    hours = ev.policy.int('offer_valid_hours')
    payload = {
        'invoice': ev.invoice.pk, 'company': ev.company.pk, 'funder': getattr(ev.funder, 'pk', None),
        'debtor': getattr(ev.debtor, 'pk', None), 'decision': ev.decision, 'eligibility': ev.eligibility,
        'checks': ev.checks, 'tier': ev.verification_tier, 'fraud': str(ev.fraud_score),
        'balance': str(ev.invoice_balance), 'rate': str(ev.advance_rate_pct),
        'eligible': str(ev.eligible_amount), 'fundable': str(ev.fundable_amount),
        'queued': str(ev.queued_amount), 'binding': ev.binding_limit,
        'headroom': {k: {kk: str(vv) for kk, vv in v.items()} for k, v in ev.headroom.items()},
        'dtp': str(ev.expected_dtp_days), 'el': str(ev.el_pct), 'grade': ev.invoice_grade,
        'fee_pct': str(ev.fee_pct), 'fee': str(ev.fee_amount), 'fee_vat': str(ev.fee_vat_amount),
        'breakdown': ev.fee_breakdown, 'reasons': [r['code'] for r in ev.reasons],
        'policy': ev.policy.version, 'model': MODEL_VERSION,
        'debtor_score': getattr(ev.debtor_score, 'pk', None),
        'transporter_score': getattr(ev.transporter_score, 'pk', None),
    }
    user = actor if (actor is not None and getattr(actor, 'pk', None)
                     and type(actor).__name__ != 'LenderUser') else None
    return InvoiceAssessment.objects.create(
        invoice=ev.invoice, company=ev.company, funder=ev.funder, debtor=ev.debtor,
        debtor_score=ev.debtor_score, transporter_score=ev.transporter_score, purpose=purpose,
        decision=ev.decision, eligible=ev.eligible, eligibility=ev.eligibility, checks=ev.checks,
        verification_tier=ev.verification_tier, fraud_score=ev.fraud_score, invoice_total=ev.invoice_total,
        invoice_balance=ev.invoice_balance, requested_amount=ev.requested_amount,
        advance_rate_pct=ev.advance_rate_pct, eligible_amount=ev.eligible_amount,
        fundable_amount=ev.fundable_amount, queued_amount=ev.queued_amount, binding_limit=ev.binding_limit,
        headroom=payload['headroom'], expected_dtp_days=ev.expected_dtp_days,
        expected_payment_date=ev.expected_payment_date, pd_horizon=ev.pd_horizon, el_pct=ev.el_pct,
        invoice_grade=ev.invoice_grade, fee_pct=ev.fee_pct, fee_amount=ev.fee_amount,
        fee_vat_amount=ev.fee_vat_amount, fee_breakdown=ev.fee_breakdown, net_payout=ev.net_payout,
        holdback_amount=ev.holdback_amount, reason_codes=ev.reasons, explanation=ev.explanation,
        explanation_source=ev.explanation_source, policy_version=ev.policy.version, model_version=MODEL_VERSION,
        content_hash=_hash(payload), created_by=user, actor_label=(actor_label or '')[:120],
        valid_until=timezone.now() + timedelta(hours=hours),
    )


def _audit(action, resource, resource_id, details, actor=None):
    from core.models import AuditLog
    user = actor if (actor is not None and getattr(actor, 'pk', None)
                     and type(actor).__name__ != 'LenderUser') else None
    try:
        AuditLog.objects.create(user=user, action=action, resource_type=resource, resource_id=str(resource_id),
                                details=details)
    except Exception:  # audit must never break a money path; the ledger and decision rows remain
        logger.exception('audit log write failed for %s %s', resource, resource_id)


def lock_funder(funder):
    from core.models import Funder
    return Funder.objects.select_for_update().get(pk=funder.pk)


def request(invoice, *, actor=None, actor_label: str = '', purpose: str = 'REQUEST', requested_amount=None):
    """Evaluate under the funder lock, record the decision, open the advance.

    Returns ``(advance_or_None, assessment, evaluation, created)``. DECLINE
    opens nothing. An invoice that already has a live advance returns it with
    ``created=False`` (idempotent retries).
    """
    from core.models import AdvanceRequest
    from core.services import facility_ledger

    company = invoice.company
    facility = line_for(company)
    funder = getattr(facility, 'funder', None)
    with transaction.atomic():
        if funder is not None:
            funder = lock_funder(funder)
            st = bookmod.load_state(funder)
        else:
            st = None
        existing = AdvanceRequest.objects.filter(invoice=invoice, status__in=LIVE_STATUSES).first()
        if existing is not None:
            ev = evaluate(invoice, requested_amount=requested_amount, state=st)
            return existing, existing.assessment, ev, False
        ev = evaluate(invoice, requested_amount=requested_amount, state=st)
        assessment = record(ev, purpose=purpose, actor=actor, actor_label=actor_label)
        _audit('DECIDE', 'InvoiceAssessment', assessment.pk, {
            'invoice': invoice.invoice_number, 'decision': ev.decision, 'fundable': str(ev.fundable_amount),
            'fee_pct': str(ev.fee_pct), 'reasons': [r['code'] for r in ev.reasons], 'channel': purpose,
            'actor': actor_label or getattr(actor, 'username', '')}, actor)
        if ev.decision == 'DECLINE':
            return None, assessment, ev, False

        common = dict(
            assessment=assessment, debtor=ev.debtor, fee_percent=ev.fee_pct,
            holdback_amount=ev.holdback_amount,
            notes=f'Fast Pay decision {ev.decision} (assessment #{assessment.pk}, policy v{ev.policy.version})',
        )
        if ev.decision == 'QUEUE':
            priority = queue_priority(ev)
            advance, created = facility_ledger.open_advance(
                invoice=invoice, facility=facility, amount=ev.queued_amount, actor=actor, status='QUEUED',
                queue_priority=priority, fee_amount=ZERO, net_amount=ZERO, **common)
        else:
            advance, created = facility_ledger.open_advance(
                invoice=invoice, facility=facility, amount=ev.fundable_amount, actor=actor,
                fee_amount=ev.fee_amount, net_amount=ev.net_payout,
                topup_pending=ev.queued_amount, **common)
        if created and ev.auto_approve:
            facility_ledger.approve_advance(advance, actor=None, actor_label='auto-approval (Mode B envelope)')
            _audit('APPROVE', 'AdvanceRequest', advance.pk, {'auto': True, 'assessment': assessment.pk})
    return advance, assessment, ev, created


def queue_priority(ev: Evaluation) -> Decimal:
    """Risk-adjusted margin per rand-day (design §3.5): (fee% - EL%) / E[DTP]."""
    dtp = ev.expected_dtp_days or D('60')
    return ((ev.fee_pct - ev.el_pct) / max(D('1'), dtp)).quantize(D('0.0001'))
