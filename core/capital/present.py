"""JSON shapes for the Fast Pay APIs (see docs/capital/IMPLEMENTATION.md, API section).

Two audiences, never mixed: ``offer``/``advance_row`` are transporter-safe
(transporter wording only, no scores of other parties); ``desk_*`` add desk
reasons, grades and scores for the capital desk and the funder.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from core.capital.reasons import for_transporter

STATUS_LABELS = {
    'ELIGIBLE': 'Eligible',
    'QUEUED': 'Waiting for capacity',
    'REQUESTED': 'Awaiting approval',
    'SCORING': 'Awaiting approval',
    'APPROVED': 'Approved, paying out',
    'DENIED': 'Not approved',
    'DISBURSED': 'Paid out',
    'SETTLED': 'Repaid by your customer',
    'CANCELLED': 'Cancelled',
    'BOUGHT_BACK': 'Bought back',
    'WRITTEN_OFF': 'Written off',
}


def num(v):
    if v is None:
        return None
    return float(v) if isinstance(v, Decimal) else v


def jsonable(obj):
    """Decimals to numbers, dates to ISO strings, recursively."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {(k if isinstance(k, str) else str(k)): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def iso(v):
    return v.isoformat() if v else None


def reference(advance) -> str:
    return f'FP-{advance.pk:06d}'


def _advance_stub(adv):
    if adv is None:
        return None
    return {'id': adv.pk, 'status': adv.status, 'status_label': STATUS_LABELS.get(adv.status, adv.status)}


def offer(src, *, persisted=None, advance=None) -> dict:
    """Transporter-safe offer from an Evaluation (preview) or an InvoiceAssessment."""
    inv = src.invoice
    is_eval = not hasattr(src, 'content_hash')  # an Evaluation, not an InvoiceAssessment row
    row = persisted or (None if is_eval else src)
    reasons = src.reasons if is_eval else src.reason_codes
    demo = bool(getattr(inv.company, 'is_demo', False))
    return {
        'offer_id': row.pk if row is not None else None,
        'invoice_id': inv.pk,
        'invoice_number': inv.invoice_number,
        'customer_name': getattr(inv.customer, 'name', ''),
        'issue_date': iso(inv.issue_date),
        'due_date': iso(inv.due_date),
        'invoice_total': num(src.invoice_total),
        'invoice_balance': num(src.invoice_balance),
        'decision': src.decision,
        'eligible': bool(src.eligible),
        'advance_rate_pct': num(src.advance_rate_pct),
        'eligible_amount': num(src.eligible_amount),
        'fundable_amount': num(src.fundable_amount),
        'queued_amount': num(src.queued_amount),
        'fee_pct': num(src.fee_pct),
        'fee_amount': num(src.fee_amount),
        'fee_vat_amount': num(src.fee_vat_amount),
        'net_payout': num(src.net_payout),
        'holdback_amount': num(src.holdback_amount),
        'expected_payment_date': iso(src.expected_payment_date),
        'verification_tier': src.verification_tier,
        'reasons': for_transporter(reasons),
        'explanation': src.explanation,
        'valid_until': iso(row.valid_until) if row is not None else None,
        'advance': _advance_stub(advance if advance is not None else getattr(src, 'live_advance', None)),
        'demo': demo,
    }


def _timeline(adv) -> list:
    items = [(adv.queued_at, 'Queued for capacity'), (adv.requested_at, 'Sent for approval'),
             (adv.approved_at, 'Approved by the finance provider'), (adv.disbursed_at, 'Paid out'),
             (adv.settled_at, STATUS_LABELS.get(adv.status, 'Closed') if adv.status in (
                 'SETTLED', 'BOUGHT_BACK', 'WRITTEN_OFF') else None)]
    if adv.status in ('DENIED', 'CANCELLED'):
        items.append((adv.updated_at, STATUS_LABELS[adv.status]))
    return [{'at': iso(at), 'label': label} for at, label in items if at and label]


def advance_row(adv) -> dict:
    from core.capital.queue import queue_position
    a = adv.assessment
    inv = adv.invoice
    # net = amount - fee - VAT on the platform part, so the VAT is what is left.
    fee_vat = max(Decimal('0'), (adv.amount or 0) - (adv.fee_amount or 0) - (adv.net_amount or 0)) \
        if adv.status != 'QUEUED' else Decimal('0')
    return {
        'id': adv.pk,
        'reference': reference(adv),
        'invoice_id': inv.pk,
        'invoice_number': inv.invoice_number,
        'customer_name': getattr(inv.customer, 'name', ''),
        'status': adv.status,
        'status_label': STATUS_LABELS.get(adv.status, adv.status),
        'amount': num(adv.amount),
        'fee_amount': num(adv.fee_amount),
        'fee_vat_amount': num(fee_vat),
        'net_amount': num(adv.net_amount),
        'holdback_amount': num(adv.holdback_amount),
        'topup_pending': num(adv.topup_pending),
        'queue_position': queue_position(adv),
        'requested_at': iso(adv.requested_at), 'approved_at': iso(adv.approved_at),
        'disbursed_at': iso(adv.disbursed_at), 'settled_at': iso(adv.settled_at),
        'queued_at': iso(adv.queued_at),
        'denial_reason': adv.denial_reason or None,
        'reasons': for_transporter(a.reason_codes) if a is not None else [],
        'timeline': _timeline(adv),
        'can_cancel': adv.status in ('QUEUED', 'REQUESTED', 'SCORING'),
    }


def score_summary(score) -> dict | None:
    if score is None:
        return None
    return {
        'id': score.pk, 'grade': score.grade, 'points': score.points, 'pd_12m': num(score.pd_12m),
        'expected_dtp_days': num(score.expected_dtp_days), 'reasons': score.reason_codes,
        'model_version': score.model_version, 'created_at': iso(score.created_at),
        'valid_until': iso(score.valid_until), 'cold_start': score.cold_start, 'hard_stop': score.hard_stop,
    }


def desk_advance(adv, *, detail: bool = False) -> dict:
    row = advance_row(adv)
    a = adv.assessment
    company = adv.facility.company
    debtor = adv.debtor
    row.update({
        'company': {'id': company.pk, 'name': company.company_name},
        'debtor': ({'id': debtor.pk, 'name': debtor.display_name,
                    'grade': getattr(a.debtor_score, 'grade', None) if a else None} if debtor else None),
        'decision': a.decision if a else None,
        'invoice_grade': a.invoice_grade if a else '',
        'el_pct': num(a.el_pct) if a else None,
        'fraud_score': num(a.fraud_score) if a else None,
        'fee_pct': num(adv.fee_percent),
        'desk_reasons': a.reason_codes if a else [],
        'debtor_score': score_summary(a.debtor_score) if a else None,
        'transporter_score': score_summary(a.transporter_score) if a else None,
        'approved_by': getattr(adv.approved_by, 'username', None),
        'disbursed_by': getattr(adv.disbursed_by, 'username', None),
        'approver_label': adv.approver_label,
        'assessment_id': adv.assessment_id,
        'notes': adv.notes,
        'settlement_reference': adv.settlement_reference,
        'disbursement_reference': adv.disbursement_reference,
    })
    if detail and a is not None:
        row['assessment'] = assessment_dict(a)
    return row


def assessment_dict(a) -> dict:
    fields = ['id', 'purpose', 'decision', 'eligible', 'eligibility', 'checks', 'verification_tier',
              'fraud_score', 'invoice_total', 'invoice_balance', 'requested_amount', 'advance_rate_pct',
              'eligible_amount', 'fundable_amount', 'queued_amount', 'binding_limit', 'headroom',
              'expected_dtp_days', 'expected_payment_date', 'pd_horizon', 'el_pct', 'invoice_grade', 'fee_pct',
              'fee_amount', 'fee_vat_amount', 'fee_breakdown', 'net_payout', 'holdback_amount', 'reason_codes',
              'explanation', 'explanation_source', 'policy_version', 'model_version', 'content_hash',
              'actor_label', 'created_at', 'valid_until']
    out = {f: getattr(a, f) for f in fields}
    out.update({'invoice_id': a.invoice_id, 'invoice_number': a.invoice.invoice_number,
                'company_id': a.company_id, 'company_name': a.company.company_name,
                'funder_id': a.funder_id, 'debtor_id': a.debtor_id,
                'debtor_score': score_summary(a.debtor_score),
                'transporter_score': score_summary(a.transporter_score),
                'created_by': getattr(a.created_by, 'username', None)})
    return jsonable(out)


def alert_dict(al) -> dict:
    return {'id': al.pk, 'kind': al.kind, 'severity': al.severity, 'title': al.title, 'message': al.message,
            'opened_at': iso(al.opened_at), 'resolved_at': iso(al.resolved_at), 'data': jsonable(al.data),
            'funder_id': al.funder_id}


def ledger_entry(e) -> dict:
    return {
        'id': e.pk, 'created_at': iso(e.created_at), 'entry_type': e.entry_type, 'amount': num(e.amount),
        'reserved_delta': num(e.reserved_delta), 'outstanding_delta': num(e.outstanding_delta),
        'company': getattr(e.company, 'company_name', None), 'company_id': e.company_id,
        'debtor': e.debtor.display_name if e.debtor_id else None, 'debtor_id': e.debtor_id,
        'invoice_number': getattr(e.invoice, 'invoice_number', None),
        'advance_reference': f'FP-{e.advance_id:06d}' if e.advance_id else None,
        'actor': e.actor_label or getattr(e.actor, 'username', None), 'reference': e.reference, 'memo': e.memo,
    }


def limit_dict(row) -> dict:
    return {'id': row.pk, 'scope': row.scope, 'debtor_id': row.debtor_id,
            'debtor_name': row.debtor.display_name if row.debtor_id else None,
            'company_id': row.company_id, 'company_name': getattr(row.company, 'company_name', None),
            'sector': row.sector, 'amount': num(row.amount), 'hold': row.hold, 'reason': row.reason,
            'created_by': getattr(row.created_by, 'username', None), 'created_at': iso(row.created_at),
            'valid_until': iso(row.valid_until), 'funder_id': row.funder_id}


def policy_dict(row) -> dict | None:
    if row is None:
        return None
    return {'id': row.pk, 'version': row.version, 'params': row.params, 'notes': row.notes,
            'created_by': getattr(row.created_by, 'username', None), 'created_at': iso(row.created_at),
            'approved_by': getattr(row.approved_by, 'username', None),
            'approved_at': iso(row.approved_by_funder_at)}
