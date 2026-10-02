"""Monthly funder data room (design §3.7): loan tape, ledger, exposures, decisions, alerts, summary.

``generate(funder, period)`` writes the files to default storage under
``settings.CAPITAL_DATA_ROOM_PREFIX/<funder.code>/<period>/`` and records a
``DataRoomExport`` with a sha256 over the files. Regenerating a period
overwrites the files and records a new export row (history kept).

The funder sees the debtor's *registration number* (its legal identity),
never a tenant's customer name: tenant data stays with the tenant (POPIA).
Files carry no generation timestamp, so the hash is stable for unchanged data.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

D = Decimal
ZERO = D('0.00')
PERIOD_RE = re.compile(r'^(\d{4})-(\d{2})$')

LOAN_TAPE_COLUMNS = [
    'reference', 'advance_id', 'transporter_id', 'transporter_name', 'debtor_id', 'debtor_registration_number',
    'sector', 'debtor_grade_at_decision', 'debtor_pd_at_decision', 'transporter_grade_at_decision',
    'transporter_pd_at_decision', 'invoice_number', 'invoice_face_incl_vat', 'invoice_face_excl_vat',
    'advance_amount', 'fee_amount', 'fee_vat_amount', 'holdback_amount', 'decision', 'decision_date',
    'approved_date', 'disbursed_date', 'expected_paid_date', 'actual_paid_date', 'collected_amount',
    'dilution_amount_incl_vat', 'status', 'pod_tier', 'policy_version', 'model_version',
]
LEDGER_COLUMNS = [
    'id', 'created_at', 'entry_type', 'amount', 'reserved_delta', 'outstanding_delta', 'advance_reference',
    'transporter_id', 'debtor_id', 'debtor_registration_number', 'invoice_number', 'actor', 'reference', 'memo',
]
EXPOSURE_COLUMNS = ['dimension', 'key', 'label', 'reserved', 'outstanding', 'committed']
DECISION_COLUMNS = [
    'assessment_id', 'created_at', 'purpose', 'invoice_number', 'transporter_id', 'debtor_registration_number',
    'decision', 'eligible', 'invoice_grade', 'verification_tier', 'fundable_amount', 'queued_amount',
    'fee_amount', 'binding_limit', 'reason_codes', 'policy_version', 'model_version', 'content_hash',
]
ALERT_COLUMNS = ['id', 'kind', 'severity', 'title', 'opened_at', 'resolved_at', 'dedupe_key']
FILES = ('loan_tape.csv', 'ledger.csv', 'exposures.csv', 'decisions.csv', 'alerts.csv', 'summary.json')


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------

def previous_period(today: date | None = None) -> str:
    today = today or timezone.localdate()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f'{y:04d}-{m:02d}'


def parse_period(period: str | None) -> tuple[str, date, date]:
    """``'YYYY-MM'`` (default: previous month) -> (period, first day, first day of next month).
    Raises ValueError for a malformed or future period."""
    period = (period or '').strip() or previous_period()
    m = PERIOD_RE.match(period)
    if not m:
        raise ValueError('Period must look like YYYY-MM')
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12 or y < 2000:
        raise ValueError('Period must look like YYYY-MM with a month from 01 to 12')
    start = date(y, mo, 1)
    today = timezone.localdate()
    if start > today.replace(day=1):
        raise ValueError('Period is in the future')
    end = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
    return period, start, end


def _aware(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _cell(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, datetime):
        return timezone.localtime(v).isoformat() if timezone.is_aware(v) else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def _day(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return timezone.localtime(v).date() if timezone.is_aware(v) else v.date()
    return v


def _csv(columns, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue().encode('utf-8')


def _json_default(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _q(v) -> Decimal:
    return D(str(v or 0)).quantize(D('0.01'))


def _reg(debtor) -> str:
    if debtor is None:
        return ''
    return debtor.registration_number or debtor.vat_number or f'DEBTOR-{debtor.pk}'


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _ledger_qs(funder):
    from core.models import CapitalLedgerEntry
    return CapitalLedgerEntry.objects.filter(Q(funder=funder) | Q(facility__funder=funder)).distinct()


def _advance_ids(funder, start_dt, end_dt) -> list[int]:
    from core.models import AdvanceRequest
    qs = AdvanceRequest.objects.filter(Q(funder=funder) | Q(facility__funder=funder))
    ids = set(qs.filter(created_at__gte=start_dt, created_at__lt=end_dt).values_list('pk', flat=True))
    led = _ledger_qs(funder).filter(advance__isnull=False)
    ids |= set(led.filter(created_at__gte=start_dt, created_at__lt=end_dt).values_list('advance_id', flat=True))
    # still on the book when the period opened
    for row in (led.filter(created_at__lt=start_dt).values('advance_id')
                .annotate(r=Sum('reserved_delta'), o=Sum('outstanding_delta'))):
        if _q(row['r']) + _q(row['o']) != 0:
            ids.add(row['advance_id'])
    return sorted(ids)


def loan_tape(funder, start_dt, end_dt) -> list[dict]:
    from core.models import AdvanceRequest, CreditNote
    ids = _advance_ids(funder, start_dt, end_dt)
    advances = (AdvanceRequest.objects.filter(pk__in=ids)
                .select_related('invoice', 'facility__company', 'debtor', 'assessment',
                                'assessment__debtor_score', 'assessment__transporter_score',
                                'invoice__customer__debtor_identity')
                .order_by('pk'))
    collected = {r['advance_id']: _q(r['t']) for r in
                 _ledger_qs(funder).filter(advance_id__in=ids, entry_type='COLLECTION', created_at__lt=end_dt)
                 .values('advance_id').annotate(t=Sum('amount'))}
    invoice_ids = [a.invoice_id for a in advances]
    dilution = defaultdict(lambda: ZERO)
    for r in (CreditNote.objects.filter(invoice_id__in=invoice_ids, status=CreditNote.ISSUED,
                                        issue_date__lt=_day(end_dt))
              .values('invoice_id').annotate(t=Sum('total_amount'))):
        dilution[r['invoice_id']] = _q(r['t'])
    rows = []
    for a in advances:
        inv = a.invoice
        debtor = a.debtor or getattr(getattr(inv, 'customer', None), 'debtor_identity', None)
        asm = a.assessment
        ds = getattr(asm, 'debtor_score', None)
        ts = getattr(asm, 'transporter_score', None)
        paid_on = _day(getattr(inv, 'paid_at', None)) if inv.status == 'PAID' else None
        if paid_on is not None and paid_on >= _day(end_dt):
            paid_on = None  # paid after the period: not known at period end
        rows.append({
            'reference': f'FP-{a.pk:06d}', 'advance_id': a.pk, 'invoice_id': inv.pk,
            'transporter_id': a.facility.company_id, 'transporter_name': a.facility.company.company_name,
            'debtor_id': getattr(debtor, 'pk', None), 'debtor_registration_number': _reg(debtor),
            'sector': getattr(debtor, 'sector', '') or 'UNKNOWN',
            'debtor_grade_at_decision': getattr(ds, 'grade', None), 'debtor_pd_at_decision': getattr(ds, 'pd_12m', None),
            'transporter_grade_at_decision': getattr(ts, 'grade', None),
            'transporter_pd_at_decision': getattr(ts, 'pd_12m', None),
            'invoice_number': inv.invoice_number, 'invoice_face_incl_vat': _q(inv.total_amount),
            'invoice_face_excl_vat': _q(_q(inv.total_amount) - _q(inv.vat_amount)),
            'advance_amount': _q(a.amount), 'fee_amount': _q(a.fee_amount),
            'fee_vat_amount': _q(asm.fee_vat_amount) if asm else None, 'holdback_amount': _q(a.holdback_amount),
            'decision': asm.decision if asm else None, 'decision_date': _day(asm.created_at) if asm else None,
            'approved_date': _day(a.approved_at), 'disbursed_date': _day(a.disbursed_at),
            'expected_paid_date': asm.expected_payment_date if asm else None, 'actual_paid_date': paid_on,
            'collected_amount': collected.get(a.pk, ZERO), 'dilution_amount_incl_vat': dilution[inv.pk],
            'status': a.status, 'pod_tier': asm.verification_tier if asm else None,
            'policy_version': asm.policy_version if asm else None, 'model_version': asm.model_version if asm else None,
        })
    return rows


def ledger_rows(funder, start_dt, end_dt) -> list[dict]:
    qs = (_ledger_qs(funder).filter(created_at__gte=start_dt, created_at__lt=end_dt)
          .select_related('debtor', 'invoice', 'actor').order_by('id'))
    return [{
        'id': e.pk, 'created_at': e.created_at, 'entry_type': e.entry_type, 'amount': e.amount,
        'reserved_delta': e.reserved_delta, 'outstanding_delta': e.outstanding_delta,
        'advance_reference': f'FP-{e.advance_id:06d}' if e.advance_id else '', 'transporter_id': e.company_id,
        'debtor_id': e.debtor_id, 'debtor_registration_number': _reg(e.debtor) if e.debtor_id else '',
        'invoice_number': getattr(e.invoice, 'invoice_number', ''),
        'actor': e.actor_label or getattr(e.actor, 'username', ''), 'reference': e.reference, 'memo': e.memo,
    } for e in qs]


def exposures(funder, end_dt) -> list[dict]:
    from core.models import Company, DebtorIdentity
    qs = _ledger_qs(funder).filter(created_at__lt=end_dt)
    rows = []

    def group(field):
        out = {}
        for r in qs.values(field).annotate(r=Sum('reserved_delta'), o=Sum('outstanding_delta')):
            res, out_ = _q(r['r']), _q(r['o'])
            if res + out_ != 0:
                out[r[field]] = (res, out_)
        return out

    by_debtor = group('debtor')
    debtors = {d.pk: d for d in DebtorIdentity.objects.filter(pk__in=[k for k in by_debtor if k])}
    for k, (res, out_) in sorted(by_debtor.items(), key=lambda x: -(x[1][0] + x[1][1])):
        rows.append({'dimension': 'debtor', 'key': k if k is not None else '',
                     'label': _reg(debtors.get(k)) if k else 'UNIDENTIFIED',
                     'reserved': res, 'outstanding': out_, 'committed': res + out_})
    by_company = group('company')
    names = dict(Company.objects.filter(pk__in=[k for k in by_company if k]).values_list('pk', 'company_name'))
    for k, (res, out_) in sorted(by_company.items(), key=lambda x: -(x[1][0] + x[1][1])):
        rows.append({'dimension': 'transporter', 'key': k if k is not None else '', 'label': names.get(k, ''),
                     'reserved': res, 'outstanding': out_, 'committed': res + out_})
    by_sector = defaultdict(lambda: [ZERO, ZERO])
    for k, (res, out_) in by_debtor.items():
        sector = (getattr(debtors.get(k), 'sector', '') or 'UNKNOWN') if k else 'UNKNOWN'
        by_sector[sector][0] += res
        by_sector[sector][1] += out_
    for k, (res, out_) in sorted(by_sector.items(), key=lambda x: -(x[1][0] + x[1][1])):
        rows.append({'dimension': 'sector', 'key': k, 'label': k, 'reserved': res, 'outstanding': out_,
                     'committed': res + out_})
    return rows


def decisions(funder, start_dt, end_dt) -> list[dict]:
    from core.models import InvoiceAssessment
    qs = (InvoiceAssessment.objects.filter(funder=funder, created_at__gte=start_dt, created_at__lt=end_dt)
          .select_related('invoice', 'debtor').order_by('id'))
    return [{
        'assessment_id': a.pk, 'created_at': a.created_at, 'purpose': a.purpose,
        'invoice_number': a.invoice.invoice_number, 'transporter_id': a.company_id,
        'debtor_registration_number': _reg(a.debtor) if a.debtor_id else '', 'decision': a.decision,
        'eligible': a.eligible, 'invoice_grade': a.invoice_grade, 'verification_tier': a.verification_tier,
        'fundable_amount': a.fundable_amount, 'queued_amount': a.queued_amount, 'fee_amount': a.fee_amount,
        'binding_limit': a.binding_limit,
        'reason_codes': ';'.join(str(r.get('code', '')) for r in (a.reason_codes or []) if isinstance(r, dict)),
        'policy_version': a.policy_version, 'model_version': a.model_version, 'content_hash': a.content_hash,
    } for a in qs]


def alert_rows(funder, start_dt, end_dt) -> list[dict]:
    from core.models import CapitalAlert
    qs = (CapitalAlert.objects.filter(funder=funder, opened_at__lt=end_dt)
          .filter(Q(resolved_at__isnull=True) | Q(resolved_at__gte=start_dt)).order_by('opened_at', 'id'))
    return [{'id': a.pk, 'kind': a.kind, 'severity': a.severity, 'title': a.title, 'opened_at': a.opened_at,
             'resolved_at': a.resolved_at, 'dedupe_key': a.dedupe_key} for a in qs]


def _overrides(funder, start_dt, end_dt) -> list[dict]:
    from core.models import AdvanceRequest, AuditLog
    funder_advances = {str(i) for i in AdvanceRequest.objects.filter(
        Q(funder=funder) | Q(facility__funder=funder)).values_list('pk', flat=True)}
    out = []
    for row in AuditLog.objects.filter(action='OVERRIDE', created_at__gte=start_dt,
                                       created_at__lt=end_dt).order_by('id'):
        details = row.details if isinstance(row.details, dict) else {}
        mine = ((row.resource_type == 'AdvanceRequest' and row.resource_id in funder_advances)
                or details.get('funder_id') == funder.pk)
        if mine:
            out.append({'id': row.pk, 'resource': row.resource_type, 'resource_id': row.resource_id,
                        'action': details.get('action') or details.get('scope') or '',
                        'by': getattr(row.user, 'username', None) or details.get('by') or '',
                        'at': row.created_at.isoformat()})
    return out


def summary(funder, period, start, end, start_dt, end_dt, tape, ledger_list, exposure_rows, decision_rows,
            alerts) -> dict:
    from core.capital import ledger as ledgermod
    from core.models import BookSnapshot, CreditPolicy, CreditNote

    def total(entry_type):
        return sum((_q(r['amount']) for r in ledger_list if r['entry_type'] == entry_type), ZERO)

    def count(entry_type):
        return sum(1 for r in ledger_list if r['entry_type'] == entry_type)

    end_balance = ledgermod.balances(_ledger_qs(funder).filter(created_at__lt=end_dt))
    debtor_committed = sorted((r['committed'] for r in exposure_rows
                               if r['dimension'] == 'debtor' and r['key'] != '' and r['committed'] > 0),
                              reverse=True)
    committed = end_balance['committed']
    top10 = (sum(debtor_committed[:10], ZERO) / committed).quantize(D('0.0001')) if committed > 0 else None
    snap = BookSnapshot.objects.filter(funder=funder, as_of__lt=end).order_by('-as_of').first()
    period_dilution = (CreditNote.objects.filter(invoice_id__in=[t['invoice_id'] for t in tape],
                                                 status=CreditNote.ISSUED, issue_date__gte=start, issue_date__lt=end)
                       .aggregate(t=Sum('total_amount'))['t'] or ZERO)
    recon = ledgermod.reconcile(funder)
    by_decision = defaultdict(int)
    for d in decision_rows:
        by_decision[d['decision']] += 1
    approved_policies = list(CreditPolicy.objects.filter(funder=funder, approved_by_funder_at__gte=start_dt,
                                                         approved_by_funder_at__lt=end_dt)
                             .order_by('version').values_list('version', flat=True))
    return {
        'funder': funder.code, 'period': period, 'period_start': start.isoformat(),
        'period_end': (end - timedelta(days=1)).isoformat(),
        'advances': {'in_loan_tape': len(tape),
                     'opened_in_period': sum(1 for t in tape if t['decision_date'] and start <= t['decision_date'] < end),
                     'by_status_at_export': dict(sorted(_count_by(tape, 'status').items()))},
        'disbursed': {'count': count('DISBURSE'), 'amount': total('DISBURSE')},
        'collected': {'count': count('COLLECTION'), 'amount': total('COLLECTION')},
        'fees': total('FEE'),
        'holdback_released': total('RELEASE_HOLDBACK'),
        'dilution_incl_vat': _q(period_dilution),
        'write_offs': {'count': count('WRITE_OFF'), 'amount': total('WRITE_OFF')},
        'buy_backs': {'count': count('BUYBACK'), 'amount': total('BUYBACK')},
        'outstanding_at_period_end': end_balance,
        'top10_share_at_period_end': top10,
        'decisions': dict(sorted(by_decision.items())),
        'reconciliation_at_export': {'ok': recon['ok'], 'breaks': len(recon['breaks']), 'checked': recon['checked']},
        'risk_index_last_snapshot': ({'as_of': snap.as_of.isoformat(), 'value': snap.risk_index, 'band': snap.band,
                                      'top10_share': (snap.metrics or {}).get('top10_share')} if snap else None),
        'alerts': {'in_period': len(alerts), 'red': sum(1 for a in alerts if a['severity'] == 'RED'),
                   'amber': sum(1 for a in alerts if a['severity'] == 'AMBER')},
        'eligibility_exceptions_and_overrides': _overrides(funder, start_dt, end_dt),
        'policy_versions_in_force': sorted({d['policy_version'] for d in decision_rows}),
        'policy_versions_approved_in_period': approved_policies,
        'files': list(FILES),
    }


def _count_by(rows, field) -> dict:
    out = defaultdict(int)
    for r in rows:
        out[r[field]] += 1
    return out


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

def content_hash(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode())
        h.update(b'\0')
        h.update(hashlib.sha256(files[name]).digest())
    return h.hexdigest()


def build(funder, period: str | None = None) -> tuple[str, dict[str, bytes], dict]:
    """The files for one period (no storage writes). Returns (period, files, summary)."""
    period, start, end = parse_period(period)
    start_dt, end_dt = _aware(start), _aware(end)
    tape = loan_tape(funder, start_dt, end_dt)
    led = ledger_rows(funder, start_dt, end_dt)
    exp = exposures(funder, end_dt)
    dec = decisions(funder, start_dt, end_dt)
    alerts = alert_rows(funder, start_dt, end_dt)
    summ = summary(funder, period, start, end, start_dt, end_dt, tape, led, exp, dec, alerts)
    files = {
        'loan_tape.csv': _csv(LOAN_TAPE_COLUMNS, tape),
        'ledger.csv': _csv(LEDGER_COLUMNS, led),
        'exposures.csv': _csv(EXPOSURE_COLUMNS, exp),
        'decisions.csv': _csv(DECISION_COLUMNS, dec),
        'alerts.csv': _csv(ALERT_COLUMNS, alerts),
        'summary.json': json.dumps(summ, default=_json_default, indent=2, sort_keys=True).encode('utf-8'),
    }
    return period, files, summ


def generate(funder, period: str | None = None, actor=None):
    """Build and store one month's data room for ``funder``; returns the ``DataRoomExport``."""
    from core.models import AuditLog, DataRoomExport
    period, files, summ = build(funder, period)
    prefix = str(getattr(settings, 'CAPITAL_DATA_ROOM_PREFIX', 'capital/data-room')).strip('/')
    paths = {}
    for name, body in files.items():
        path = f'{prefix}/{funder.code}/{period}/{name}'
        if default_storage.exists(path):
            default_storage.delete(path)
        paths[name] = default_storage.save(path, ContentFile(body))
    user = actor if (actor is not None and getattr(actor, 'pk', None)
                     and type(actor).__name__ != 'LenderUser') else None
    export = DataRoomExport.objects.create(
        funder=funder, period=period, files=paths, content_hash=content_hash(files), created_by=user,
        summary=json.loads(files['summary.json']))
    try:
        AuditLog.objects.create(user=user, action='EXPORT', resource_type='DataRoomExport', resource_id=str(export.pk),
                                details={'funder_id': funder.pk, 'period': period, 'content_hash': export.content_hash})
    except Exception:
        logger.exception('audit log write failed for data room export %s', export.pk)
    return export
