"""Issuing and voiding credit notes, and voiding invoices.

An issued invoice is never edited. To correct it:
  * credit note (full or partial) - reduces the invoice balance, reverses
    revenue and output VAT in the credit note's own period;
  * void - only while nothing has been paid or credited and it isn't
    financed; the invoice drops out of revenue entirely.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.tax_codes import ZERO, round2


class CreditNoteError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _guard_financed(invoice, user):
    """Financed invoices are fully locked for the transporter. The capital
    desk (staff) may record a credit note, which flags the advance."""
    from core.services.capital_guard import is_invoice_financed
    if is_invoice_financed(invoice):
        if not getattr(user, 'is_staff', False):
            raise CreditNoteError('This invoice is financed through Fast Pay and is locked. '
                                  'Contact the capital desk to record a credit.', status_code=409)
        return True
    return False


def _credited_per_line(invoice):
    from core.models import CreditNoteLine, CreditNote
    rows = (CreditNoteLine.objects
            .filter(credit_note__invoice=invoice, credit_note__status=CreditNote.ISSUED,
                    invoice_line__isnull=False)
            .values('invoice_line_id').annotate(net=Sum('net_amount'), vat=Sum('vat_amount')))
    return {r['invoice_line_id']: (r['net'] or ZERO, r['vat'] or ZERO) for r in rows}


def _full_credit_lines(invoice):
    """What is still uncredited, line by line. An uncredited line is mirrored
    exactly (same qty/price/discount, so net and VAT reverse to the cent);
    a partly credited line credits its remaining net and VAT."""
    if invoice.totals_source == 'LEGACY':
        remaining = invoice.total_amount - invoice.credited_amount
        if remaining <= 0:
            return []
        base = invoice.subtotal + invoice.vat_amount
        net = round2(remaining * invoice.subtotal / base) if base else remaining
        vat = remaining - net
        rate = round2(vat / net * 100) if net else Decimal('0.00')
        return [{
            'position': 0, 'description': f'Credit of invoice {invoice.invoice_number}',
            'quantity': Decimal('1'), 'unit_price': net,
            'tax_code': 'STANDARD' if vat > 0 else 'NO_VAT',
            'tax_rate': Decimal('15.00') if vat > 0 else rate,
            'net_amount': net, 'vat_amount': vat, 'total_amount': net + vat, 'invoice_line': None,
        }]
    credited = _credited_per_line(invoice)
    out = []
    for line in invoice.lines.all():
        c_net, c_vat = credited.get(line.pk, (ZERO, ZERO))
        r_net, r_vat = line.net_amount - c_net, line.vat_amount - c_vat
        if r_net <= 0 and r_vat <= 0:
            continue
        untouched = c_net == 0 and c_vat == 0
        out.append({
            'position': len(out), 'description': line.description,
            'quantity': line.quantity if untouched else Decimal('1'),
            'unit_price': line.unit_price if untouched else r_net,
            'tax_code': line.tax_code, 'tax_rate': line.tax_rate,
            'net_amount': r_net, 'vat_amount': r_vat, 'total_amount': r_net + r_vat,
            'invoice_line': line,
        })
    return out


def create_credit_note(invoice, *, user, reason, lines=None, full=False, issue_date=None,
                       source='MANUAL', external_id=''):
    """Issue a credit note against an issued invoice. Returns the CreditNote."""
    from core.models import CreditNote, CreditNoteLine, Invoice, InvoiceLine
    from core.services.invoice_lines import build_lines, LineError
    from core.services.ledger import recalculate_invoice
    from core.services.numbering import allocate_credit_note_number

    reason = (reason or '').strip()
    if not reason:
        raise CreditNoteError('A reason is required for a credit note.')
    issue_date = issue_date or timezone.localdate()
    if isinstance(issue_date, str):
        issue_date = date.fromisoformat(issue_date)

    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if external_id:
            existing = CreditNote.objects.filter(company=invoice.company, source=source,
                                                 external_id=external_id).first()
            if existing:
                return existing
        if invoice.status == 'DRAFT':
            raise CreditNoteError('A draft can simply be edited or deleted; credit notes are for issued invoices.')
        if invoice.status == 'CANCELLED':
            raise CreditNoteError('This invoice is void.')
        if issue_date < invoice.issue_date:
            raise CreditNoteError('A credit note can\'t be dated before its invoice.')
        financed = _guard_financed(invoice, user)

        if full:
            computed = _full_credit_lines(invoice)
            if not computed:
                raise CreditNoteError('This invoice has already been fully credited.')
        else:
            try:
                computed = build_lines(lines or [], company=invoice.company, on_date=issue_date)
            except LineError as e:
                raise CreditNoteError(str(e))
            credited = _credited_per_line(invoice)
            for l in computed:
                ref = l.get('invoice_line')
                if ref in (None, ''):
                    l['invoice_line'] = None
                    continue
                inv_line = InvoiceLine.objects.filter(pk=getattr(ref, 'pk', ref), invoice=invoice).first()
                if inv_line is None:
                    raise CreditNoteError(f'Line {l["position"] + 1}: that line is not on this invoice.')
                if l['tax_code'] != inv_line.tax_code:
                    raise CreditNoteError(f'Line {l["position"] + 1}: a credit must use the tax code of the '
                                          f'line it reverses ({inv_line.tax_code}).')
                c_net, c_vat = credited.get(inv_line.pk, (ZERO, ZERO))
                if c_net + l['net_amount'] > inv_line.net_amount:
                    raise CreditNoteError(f'Line {l["position"] + 1}: more than the remaining '
                                          f'{inv_line.net_amount - c_net} can\'t be credited on that line.')
                # VAT follows the line it reverses: the slice that closes a line
                # takes exactly the VAT still on it, and per-slice rounding can
                # never credit more VAT than the line carried.
                remaining_vat = inv_line.vat_amount - c_vat
                if c_net + l['net_amount'] == inv_line.net_amount or l['vat_amount'] > remaining_vat:
                    l['vat_amount'] = max(ZERO, remaining_vat)
                    l['total_amount'] = l['net_amount'] + l['vat_amount']
                credited[inv_line.pk] = (c_net + l['net_amount'], c_vat + l['vat_amount'])
                l['invoice_line'] = inv_line

        subtotal = sum((l['net_amount'] for l in computed), ZERO)
        vat = sum((l['vat_amount'] for l in computed), ZERO)
        total = subtotal + vat
        if total <= 0:
            raise CreditNoteError('A credit note must be for more than zero.')
        already = invoice.credited_amount or ZERO
        if already + total > invoice.total_amount:
            raise CreditNoteError(
                f'This would credit more than the invoice total (already credited {already}, '
                f'invoice total {invoice.total_amount}).')

        cn = CreditNote.objects.create(
            company=invoice.company, invoice=invoice, customer=invoice.customer,
            credit_note_number=allocate_credit_note_number(invoice.company),
            issue_date=issue_date, reason=reason, subtotal=subtotal, vat_amount=vat,
            total_amount=total, source=source, external_id=external_id or '',
            created_by=user if getattr(user, 'pk', None) else None,
        )
        CreditNoteLine.objects.bulk_create([CreditNoteLine(
            credit_note=cn, invoice_line=l.get('invoice_line'), position=l['position'],
            description=l['description'], quantity=l['quantity'], unit_price=l['unit_price'],
            tax_code=l['tax_code'], tax_rate=l['tax_rate'], net_amount=l['net_amount'],
            vat_amount=l['vat_amount'], total_amount=l['total_amount'],
        ) for l in computed])
        recalculate_invoice(invoice, actor_id=getattr(user, 'id', None))
        if financed:
            _flag_advance(invoice, f'Credit note {cn.credit_note_number} ({total}) issued: {reason}')
    _audit(cn, user, 'CREATE', {'invoice': invoice.invoice_number, 'total': str(total), 'reason': reason})
    return cn


def void_credit_note(cn, *, user, reason):
    from core.models import CreditNote
    from core.services.ledger import recalculate_invoice

    reason = (reason or '').strip()
    if not reason:
        raise CreditNoteError('A reason is required to void a credit note.')
    with transaction.atomic():
        cn = CreditNote.objects.select_for_update().get(pk=cn.pk)
        if cn.status == CreditNote.VOID:
            return cn
        if cn.source != 'MANUAL':
            raise CreditNoteError(f'This credit note was synced from {cn.get_source_display()}; void it there.')
        _guard_financed(cn.invoice, user)
        cn.status = CreditNote.VOID
        cn.voided_at = timezone.now()
        cn.void_reason = reason
        cn.save(update_fields=['status', 'voided_at', 'void_reason', 'updated_at'])
        recalculate_invoice(cn.invoice_id, actor_id=getattr(user, 'id', None))
    _audit(cn, user, 'UPDATE', {'status': ['ISSUED', 'VOID'], 'reason': reason})
    return cn


def void_invoice(invoice, *, user, reason):
    """Void an issued invoice nobody has paid or credited. Drafts are
    deleted instead; anything with money against it needs a credit note."""
    from core.models import Invoice, CreditNote

    reason = (reason or '').strip()
    if not reason:
        raise CreditNoteError('A reason is required to void an invoice.')
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status == 'CANCELLED':
            return invoice
        if invoice.status == 'DRAFT':
            raise CreditNoteError('Delete a draft instead of voiding it.')
        if invoice.payments.exists():
            raise CreditNoteError('Payments are recorded against this invoice; issue a credit note instead.')
        if CreditNote.objects.filter(invoice=invoice, status=CreditNote.ISSUED).exists():
            raise CreditNoteError('Credit notes exist on this invoice; credit the remainder instead.')
        from core.services.capital_guard import is_invoice_financed
        if is_invoice_financed(invoice):
            raise CreditNoteError('This invoice is financed through Fast Pay and can\'t be voided.', status_code=409)
        invoice.status = 'CANCELLED'
        invoice.voided_at = timezone.now()
        invoice.void_reason = reason
        invoice.save()
    _audit(invoice, user, 'UPDATE', {'status': ['ISSUED', 'VOID'], 'reason': reason})
    return invoice


def _flag_advance(invoice, note):
    from core.models import AdvanceRequest
    for adv in AdvanceRequest.objects.filter(invoice=invoice, status__in=['APPROVED', 'DISBURSED']):
        adv.notes = (adv.notes + '\n' if adv.notes else '') + f'[dilution] {note}'
        AdvanceRequest.objects.filter(pk=adv.pk).update(notes=adv.notes)


def _audit(obj, user, action, changes):
    try:
        from core.models import AuditLog
        if action == 'CREATE':
            AuditLog.log_create(obj, user=user if getattr(user, 'pk', None) else None)
        else:
            AuditLog.log_update(obj, user=user if getattr(user, 'pk', None) else None, changes=changes)
    except Exception:
        pass
