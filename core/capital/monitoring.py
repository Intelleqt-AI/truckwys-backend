"""Early warnings, limit / concentration / risk-index alerts and daily book snapshots.

Design: docs/capital-risk/03-design.md §3.6 (triggers), §3.4 (risk index), §6.3 (jobs).

Alerts (``CapitalAlert``) are idempotent: one *open* alert per ``dedupe_key``
(a partial unique index enforces it). Every rule re-evaluates its whole
population each run, raises what is true now and auto-resolves what cleared,
so the hourly ``monitor`` job can run as often as it likes.

Dedupe keys are ``fp:<funder id>:<rule>:<subject>``. A rule auto-resolves only
keys under its own prefix, so e.g. the nightly grade-downgrade alerts (kind
DEBTOR_WARNING, raised by ``core.capital.jobs``) are never closed by the DTP
drift rule that shares the kind.

What monitoring may change: only ``DebtorIdentity.on_hold`` (with a reason) on
a hard CIPC status, which stops *new* exposure; existing advances run off.
Limits are never cut automatically and a paid invoice is never settled here
(the capital desk settles against payment evidence).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

D = Decimal
ZERO = D('0.00')
SEVERITY_RANK = {'INFO': 0, 'AMBER': 1, 'RED': 2}
HARD_CIPC = ('BUSINESS_RESCUE', 'LIQUIDATION', 'DEREGISTERED', 'DEREGISTRATION')
DEC_JAN_WIDEN_DAYS = 15        # design §2.7: widen Dec/Jan triggers until 12 months of history exist
MIN_PAID_FOR_DRIFT = 3         # fewer paid invoices than this: no DTP drift signal
OVERDUE_RED_DAYS = 60
MANUAL_RESOLVE_SNOOZE = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def key(funder, rule: str, subject='') -> str:
    fid = getattr(funder, 'pk', funder)
    return f'fp:{fid}:{rule}:{subject}' if subject != '' else f'fp:{fid}:{rule}'


def prefix(funder, rule: str) -> str:
    return f'fp:{getattr(funder, "pk", funder)}:{rule}:'


def jsonsafe(obj):
    """Decimals to strings, dates to ISO, recursively (JSONField-safe, exact)."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date,)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {(k if isinstance(k, str) else str(k)): jsonsafe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonsafe(v) for v in obj]
    return obj


def _pct(part, whole) -> Decimal:
    return (D(part) / D(whole)) if whole else D('0')


def _fmt_pct(x: Decimal) -> str:
    return f'{(D(x) * 100).quantize(D("0.1"))}%'


def _r(v) -> str:
    return f'R{D(v):,.2f}'


# ---------------------------------------------------------------------------
# Raise / resolve
# ---------------------------------------------------------------------------

def notify_staff(alert) -> None:
    """Best-effort in-app notification to TruckWys staff and the funder's members."""
    try:
        from django.contrib.auth import get_user_model
        from core.models import FunderMembership, Notification
        User = get_user_model()
        ids = set(User.objects.filter(is_staff=True, is_active=True).values_list('pk', flat=True))
        if alert.funder_id:
            ids |= set(FunderMembership.objects.filter(funder_id=alert.funder_id, user__is_active=True)
                       .values_list('user_id', flat=True))
        ntype = {'RED': 'ALERT', 'AMBER': 'WARNING'}.get(alert.severity, 'INFO')
        Notification.objects.bulk_create([
            Notification(user_id=uid, type=ntype, title=f'Fast Pay: {alert.title}'[:200],
                         message=alert.message or alert.title, link='/capital')
            for uid in ids])
    except Exception:
        logger.exception('capital alert notification failed (alert %s)', getattr(alert, 'pk', None))
    logger.log(logging.INFO if alert.severity == 'INFO' else logging.WARNING, 'Capital alert [%s/%s] %s: %s',
               alert.severity, alert.kind, alert.title, alert.message)


def _recently_resolved_by_hand(dedupe_key: str, severity: str) -> bool:
    """A person resolved this alert in the last 24h at the same or worse
    severity: don't reopen it every hour while the condition persists."""
    from core.models import CapitalAlert
    row = (CapitalAlert.objects.filter(dedupe_key=dedupe_key, resolved_at__isnull=False,
                                       resolved_by__isnull=False,
                                       resolved_at__gte=timezone.now() - MANUAL_RESOLVE_SNOOZE)
           .order_by('-resolved_at').first())
    return row is not None and SEVERITY_RANK.get(row.severity, 0) >= SEVERITY_RANK.get(severity, 0)


def _open_alert(dedupe_key: str):
    from core.models import CapitalAlert
    return CapitalAlert.objects.filter(dedupe_key=dedupe_key, resolved_at__isnull=True).first()


def raise_alert(funder, kind: str, severity: str, dedupe_key: str, title: str, message: str = '',
                data: dict | None = None, *, notify: bool = True):
    """Open an alert unless one is already open for ``dedupe_key``.

    * Same or better severity than the open one: the open alert's text and
      data are refreshed (a better severity is recorded in place, no notice).
    * Worse severity: the open alert is resolved and a new one opened (and
      notified), so the escalation is visible in the history.
    * A concurrent insert hitting the open-alert unique index is not an
      error: the winner is returned.

    Returns ``(alert, created)``.
    """
    from core.models import CapitalAlert
    data = jsonsafe(data or {})
    title, dedupe_key = title[:200], dedupe_key[:200]
    open_row = _open_alert(dedupe_key)
    if open_row is not None:
        if SEVERITY_RANK.get(severity, 0) <= SEVERITY_RANK.get(open_row.severity, 0):
            open_row.severity, open_row.title, open_row.message, open_row.data = severity, title, message, data
            open_row.save(update_fields=['severity', 'title', 'message', 'data'])
            return open_row, False
        open_row.resolved_at = timezone.now()
        open_row.save(update_fields=['resolved_at'])
    elif _recently_resolved_by_hand(dedupe_key, severity):
        return None, False
    try:
        with transaction.atomic():
            alert = CapitalAlert.objects.create(
                funder=funder if getattr(funder, 'pk', None) else None, kind=kind, severity=severity,
                dedupe_key=dedupe_key, title=title, message=message, data=data)
    except IntegrityError:
        existing = CapitalAlert.objects.filter(dedupe_key=dedupe_key, resolved_at__isnull=True).first()
        if existing is None:
            raise
        return existing, False
    if notify:
        notify_staff(alert)
    return alert, True


def auto_resolve(funder, kind: str, keep_keys, *, key_prefix: str | None = None) -> int:
    """Resolve open alerts of ``kind`` for ``funder`` whose key is not in
    ``keep_keys`` (their condition cleared). ``key_prefix`` limits it to one rule."""
    from core.models import CapitalAlert
    qs = CapitalAlert.objects.filter(funder=funder, kind=kind, resolved_at__isnull=True)
    if key_prefix:
        qs = qs.filter(dedupe_key__startswith=key_prefix)
    keep = set(keep_keys or ())
    n = 0
    now = timezone.now()
    for al in qs:
        if al.dedupe_key in keep:
            continue
        al.resolved_at = now
        al.save(update_fields=['resolved_at'])
        n += 1
    return n


class _Raiser:
    """Collects the keys a rule raised this run, then auto-resolves the rest."""

    def __init__(self, funder, kind: str, rule: str):
        self.funder, self.kind, self.rule = funder, kind, rule
        self.keys: set[str] = set()
        self.raised = 0

    def __call__(self, subject, severity, title, message='', data=None):
        k = key(self.funder, self.rule, subject)
        self.keys.add(k)
        _al, created = raise_alert(self.funder, self.kind, severity, k, title, message, data)
        self.raised += int(created)

    def finish(self) -> dict:
        resolved = auto_resolve(self.funder, self.kind, self.keys, key_prefix=prefix(self.funder, self.rule))
        return {'open': len(self.keys), 'raised': self.raised, 'resolved': resolved}


# ---------------------------------------------------------------------------
# Book checks
# ---------------------------------------------------------------------------

def _util_severity(util: Decimal, threshold: Decimal) -> str | None:
    if util >= 1:
        return 'RED'
    if util >= threshold:
        return 'AMBER'
    return None


def _latest_transporter_grades(company_ids) -> dict:
    from core.models import CapitalScore
    out = {}
    for s in (CapitalScore.objects.filter(kind='TRANSPORTER', company_id__in=list(company_ids))
              .order_by('-created_at', '-id').only('company_id', 'grade')):
        out.setdefault(s.company_id, s.grade)
    return out


def check_limits(funder, st) -> dict:
    from core.capital import book as bookmod
    from core.models import Company, Facility
    p = st.policy
    threshold = p.dec('limit_alert_utilisation')
    r = _Raiser(funder, 'LIMIT', 'limit')

    if st.pot > 0:
        u = _pct(st.committed, st.pot)
        sev = _util_severity(u, threshold)
        if sev:
            r('pot', sev, f'Pot {_fmt_pct(u)} used',
              f'{_r(st.committed)} committed of the {_r(st.pot)} pot.',
              {'scope': 'pot', 'used': st.committed, 'cap': st.pot, 'utilisation': u})

    for did, amt in st.by_debtor.items():
        if did is None or amt <= 0:
            continue
        m = st.debtor_meta.get(did, {})
        cap, source = bookmod.debtor_cap(st, did, m.get('grade'), bool(m.get('cold_start')))
        if source == 'hold':
            continue  # deliberate: no new exposure, the existing book runs off
        u = _pct(amt, cap) if cap > 0 else D('99')
        sev = _util_severity(u, threshold)
        if sev:
            reg = m.get('registration_number') or f'#{did}'
            r(f'debtor:{did}', sev, f'Debtor {reg} at {_fmt_pct(u) if cap > 0 else "over its cap"}',
              f'{_r(amt)} committed against a debtor cap of {_r(cap)} ({source}).',
              {'scope': 'debtor', 'debtor_id': did, 'used': amt, 'cap': cap, 'source': source,
               'utilisation': u if cap > 0 else None})

    lines = {f.company_id: f for f in Facility.objects.filter(funder=funder, status='ACTIVE')}
    grades = _latest_transporter_grades(st.by_company.keys())
    names = dict(Company.objects.filter(pk__in=[c for c in st.by_company if c]).values_list('pk', 'company_name'))
    for cid, amt in st.by_company.items():
        line = lines.get(cid)
        if cid is None or amt <= 0 or line is None:
            continue
        cap, source = bookmod.transporter_cap(st, cid, line.limit, grades.get(cid))
        if source == 'hold':
            continue
        u = _pct(amt, cap) if cap > 0 else D('99')
        sev = _util_severity(u, threshold)
        if sev:
            r(f'transporter:{cid}', sev,
              f'Transporter {names.get(cid, cid)} at {_fmt_pct(u) if cap > 0 else "over its cap"}',
              f'{_r(amt)} committed against a line cap of {_r(cap)} ({source}).',
              {'scope': 'transporter', 'company_id': cid, 'used': amt, 'cap': cap, 'source': source,
               'utilisation': u if cap > 0 else None})

    for sector, amt in st.by_sector.items():
        if amt <= 0:
            continue
        cap, source = bookmod.sector_cap(st, sector)
        if source == 'hold':
            continue
        u = _pct(amt, cap) if cap > 0 else D('99')
        sev = _util_severity(u, threshold)
        if sev:
            r(f'sector:{sector}', sev, f'Sector {bookmod.sector_label(sector)} at '
              f'{_fmt_pct(u) if cap > 0 else "over its cap"}',
              f'{_r(amt)} committed against a sector cap of {_r(cap)} ({source}).',
              {'scope': 'sector', 'sector': sector, 'used': amt, 'cap': cap, 'source': source})
    return r.finish()


def check_concentration(funder, st) -> dict:
    p = st.policy
    r = _Raiser(funder, 'CONCENTRATION', 'conc')
    if not st.small_book and st.committed > 0:
        if st.top10_share > p.dec('top10_hard_pct'):
            r('top10', 'RED', f'Top-10 debtors are {_fmt_pct(st.top10_share)} of the book',
              f'Above the {_fmt_pct(p.dec("top10_hard_pct"))} hard stop: new exposure to top-10 names is blocked.',
              {'top10_share': st.top10_share, 'band': 'hard'})
        elif st.top10_share > p.dec('top10_soft_pct'):
            r('top10', 'AMBER', f'Top-10 debtors are {_fmt_pct(st.top10_share)} of the book',
              f'Above the {_fmt_pct(p.dec("top10_soft_pct"))} soft limit: top-10 names are braked.',
              {'top10_share': st.top10_share, 'band': 'soft'})
        if st.n_eff is not None and st.n_eff < D(p.int('neff_alert')):
            r('neff', 'AMBER', f'Effective number of debtors is {st.n_eff}',
              f'Below the alert level of {p.int("neff_alert")}: the book depends on few names.',
              {'n_eff': st.n_eff, 'hhi': st.hhi})
    return r.finish()


def check_risk_index(funder, st, risk) -> dict:
    r = _Raiser(funder, 'RISK_INDEX', 'risk')
    ri = risk.get('risk_index') or {}
    band = ri.get('band')
    if band in ('amber', 'red'):
        sev = 'RED' if band == 'red' else 'AMBER'
        extra = ' New advances are referred to a person.' if band == 'red' else ''
        r('index', sev, f'Book Risk Index {ri.get("value")} ({band})',
          'Summary of expected loss, concentration and stress.' + extra,
          {'value': ri.get('value'), 'band': band, 'components': ri.get('components')})
    return r.finish()


def check_reconciliation(funder, result: dict | None = None) -> dict:
    from core.capital import ledger
    result = result if result is not None else ledger.reconcile(funder)
    r = _Raiser(funder, 'RECONCILIATION', 'recon')
    if not result.get('ok', True):
        n = len(result.get('breaks') or [])
        r('ledger', 'RED', f'Ledger reconciliation: {n} break(s)',
          'Ledger-derived balances differ from the cached facility or advance figures. '
          'Investigate before approving more advances (manage.py capital_reconcile).',
          {'breaks': (result.get('breaks') or [])[:50], 'checked': result.get('checked')})
    out = r.finish()
    out['ok'] = bool(result.get('ok', True))
    return out


def run_book_checks(funder) -> dict:
    """Limits, concentration, Book Risk Index and ledger reconciliation."""
    from core.capital import book as bookmod
    st = bookmod.load_state(funder)
    risk = bookmod.risk_summary(st)
    return {
        'limits': check_limits(funder, st),
        'concentration': check_concentration(funder, st),
        'risk_index': check_risk_index(funder, st, risk),
        'reconciliation': check_reconciliation(funder),
    }


# ---------------------------------------------------------------------------
# Early warnings (debtors and transporters with exposure)
# ---------------------------------------------------------------------------

def _network_features(debtor, as_of):
    try:
        from core.capital.scoring.debtor import network_features
    except ImportError:
        return None
    return network_features(debtor, as_of=as_of)


def _d(v):
    if v is None or v == '':
        return None
    try:
        return D(str(v))
    except Exception:
        return None


def dtp_drift_thresholds(policy, as_of: date) -> tuple[Decimal, Decimal]:
    amber, red = D(policy.int('dtp_drift_amber_days')), D(policy.int('dtp_drift_red_days'))
    if as_of.month in (12, 1):
        amber += DEC_JAN_WIDEN_DAYS
        red += DEC_JAN_WIDEN_DAYS
    return amber, red


def dilution_3m(company, as_of: date) -> dict:
    """Credit notes plus disputed invoices over invoiced, excl. VAT, last 3 months."""
    from core.models import CreditNote, Invoice
    start = as_of - timedelta(days=91)
    inv = (Invoice.objects.filter(company=company, issue_date__gt=start, issue_date__lte=as_of)
           .exclude(status__in=('DRAFT', 'CANCELLED')))
    invoiced = inv.aggregate(t=Sum('subtotal'))['t'] or ZERO
    disputed = inv.filter(status='DISPUTED').aggregate(t=Sum('subtotal'))['t'] or ZERO
    credits = (CreditNote.objects.filter(company=company, status=CreditNote.ISSUED, issue_date__gt=start,
                                         issue_date__lte=as_of).aggregate(t=Sum('subtotal'))['t'] or ZERO)
    pct = ((credits + disputed) / invoiced) if invoiced > 0 else None
    return {'invoiced_excl_vat': invoiced, 'credits_excl_vat': credits, 'disputed_excl_vat': disputed,
            'ratio': pct.quantize(D('0.0001')) if pct is not None else None}


def run_early_warnings(funder, *, as_of: date | None = None) -> dict:
    from core.capital import book as bookmod
    from core.models import Company, DebtorIdentity
    as_of = as_of or timezone.localdate()
    st = bookmod.load_state(funder)
    p = st.policy
    rd = _Raiser(funder, 'DEBTOR_WARNING', 'ew-debtor')
    holds = 0
    amber_days, red_days = dtp_drift_thresholds(p, as_of)
    debtor_ids = [d for d, a in st.by_debtor.items() if d is not None and a > 0]
    for debtor in DebtorIdentity.objects.filter(pk__in=debtor_ids):
        reg = debtor.registration_number or debtor.vat_number or f'#{debtor.pk}'
        exposure = st.by_debtor.get(debtor.pk, ZERO)
        if debtor.cipc_status in HARD_CIPC:
            status_label = debtor.get_cipc_status_display()
            rd(f'cipc:{debtor.pk}', 'RED', f'Debtor {reg}: CIPC status {status_label}',
               f'{_r(exposure)} committed. Debtor put on hold (no new exposure); analyst review needed.',
               {'debtor_id': debtor.pk, 'cipc_status': debtor.cipc_status, 'exposure': exposure})
            if not debtor.on_hold:
                debtor.on_hold = True
                debtor.hold_reason = (f'Automatic hold {as_of.isoformat()}: CIPC status {status_label}. '
                                      'Review before releasing.')
                debtor.save(update_fields=['on_hold', 'hold_reason', 'updated_at'])
                holds += 1
        feats = _network_features(debtor, as_of)
        if feats:
            d60, d12 = _d(feats.get('dtp_60d')), _d(feats.get('dtp_12m'))
            paid_n = int(feats.get('paid_n') or 0)
            if d60 is not None and d12 is not None and paid_n >= MIN_PAID_FOR_DRIFT:
                drift = d60 - d12
                sev = 'RED' if drift >= red_days else ('AMBER' if drift >= amber_days else None)
                if sev:
                    rd(f'dtp:{debtor.pk}', sev, f'Debtor {reg} paying {drift} days slower',
                       f'60-day mean days-to-pay {d60} vs 12-month baseline {d12} (trigger +{amber_days} amber, '
                       f'+{red_days} red). Amber: freeze the limit; red: cut it to current exposure.',
                       {'debtor_id': debtor.pk, 'dtp_60d': d60, 'dtp_12m': d12, 'drift': drift,
                        'exposure': exposure})
    debtor_out = rd.finish()
    debtor_out['holds_set'] = holds

    rt = _Raiser(funder, 'TRANSPORTER_WARNING', 'ew-transporter')
    threshold = p.dec('dilution_alert_pct')
    company_ids = [c for c, a in st.by_company.items() if c is not None and a > 0]
    for co in Company.objects.filter(pk__in=company_ids):
        exposure = st.by_company.get(co.pk, ZERO)
        dil = dilution_3m(co, as_of)
        if dil['ratio'] is not None and dil['ratio'] > threshold:
            rt(f'dilution:{co.pk}', 'AMBER', f'{co.company_name}: dilution {_fmt_pct(dil["ratio"])} over 3 months',
               f'Credit notes and disputes above {_fmt_pct(threshold)} of invoicing. Consider -5pp on the advance '
               'rate; above 8% stop new advances.', dict(dil, company_id=co.pk, exposure=exposure))
        status = (getattr(co, 'subscription_status', '') or '').lower()
        if status == 'grace_period':
            rt(f'subscription:{co.pk}', 'AMBER', f'{co.company_name}: subscription in grace period',
               'A subscription charge failed. Review the line.', {'company_id': co.pk, 'status': status,
                                                                    'exposure': exposure})
        elif status in ('suspended', 'cancelled'):
            rt(f'subscription:{co.pk}', 'RED', f'{co.company_name}: subscription {status}',
               'The transporter is not in good standing. Review the line before any new advance.',
               {'company_id': co.pk, 'status': status, 'exposure': exposure})
    return {'debtors': debtor_out, 'transporters': rt.finish()}


# ---------------------------------------------------------------------------
# Funded invoices: overdue, paid awaiting settlement
# ---------------------------------------------------------------------------

def run_overdue_checks(funder, *, as_of: date | None = None) -> dict:
    from core.capital.policy import policy_for_funder
    from core.models import AdvanceRequest
    as_of = as_of or timezone.localdate()
    days_limit = policy_for_funder(funder).int('overdue_alert_days')
    ro = _Raiser(funder, 'OVERDUE', 'overdue')
    rs = _Raiser(funder, 'SETTLEMENT', 'settle')
    advances = (AdvanceRequest.objects.filter(Q(funder=funder) | Q(facility__funder=funder), status='DISBURSED')
                .select_related('invoice', 'facility__company').distinct())
    for adv in advances:
        inv = adv.invoice
        ref = f'FP-{adv.pk:06d}'
        base = {'advance_id': adv.pk, 'reference': ref, 'invoice_id': inv.pk, 'invoice_number': inv.invoice_number,
                'company_id': adv.facility.company_id, 'amount': adv.amount}
        if inv.status == 'PAID' or (inv.balance is not None and D(inv.balance) <= 0
                                    and inv.status not in ('CANCELLED', 'CREDITED')):
            rs(adv.pk, 'INFO', f'{ref} paid, awaiting settlement by the capital desk',
               f'Invoice {inv.invoice_number} is paid in full; advance {_r(adv.amount)} is still disbursed. '
               'Settle it against the payment evidence.', base)
            continue
        if inv.due_date and (as_of - inv.due_date).days > days_limit:
            late = (as_of - inv.due_date).days
            sev = 'RED' if late > OVERDUE_RED_DAYS else 'AMBER'
            ro(adv.pk, sev, f'{ref} is {late} days past due',
               f'Invoice {inv.invoice_number} ({_r(inv.balance)} outstanding) was due {inv.due_date:%d %b %Y}.',
               dict(base, days_past_due=late, balance=inv.balance, due_date=inv.due_date))
    return {'overdue': ro.finish(), 'settlement': rs.finish()}


# ---------------------------------------------------------------------------
# Daily snapshot
# ---------------------------------------------------------------------------

def snapshot(funder, as_of: date | None = None):
    """Write (or refresh) today's ``BookSnapshot`` for ``funder``."""
    from core.capital import book as bookmod
    from core.models import BookSnapshot
    as_of = as_of or timezone.localdate()
    st = bookmod.load_state(funder)
    risk = bookmod.risk_summary(st)
    ri = risk['risk_index']
    metrics = jsonsafe({
        'pot': st.pot, 'committed': st.committed, 'outstanding': st.outstanding, 'reserved': st.reserved,
        'headroom': st.headroom, 'utilisation': _pct(st.committed, st.pot).quantize(D('0.0001')) if st.pot else None,
        'small_book': st.small_book, 'hhi': st.hhi.quantize(D('0.0001')), 'n_eff': st.n_eff,
        'top1_share': st.top1_share.quantize(D('0.0001')), 'top10_share': st.top10_share.quantize(D('0.0001')),
        'top10_band': st.top10_band, 'expected_loss': risk['expected_loss'], 'book_el_pct': risk['book_el_pct'],
        'protection': risk['protection'], 'risk_index': ri, 'stress': risk['stress'],
        'debtors': sum(1 for d, a in st.by_debtor.items() if d is not None and a > 0),
        'transporters': sum(1 for c, a in st.by_company.items() if c is not None and a > 0),
        'policy_version': st.policy.version,
    })
    row, _ = BookSnapshot.objects.update_or_create(
        funder=funder, as_of=as_of,
        defaults={'metrics': metrics, 'risk_index': ri.get('value'), 'band': ri.get('band') or ''})
    return row
