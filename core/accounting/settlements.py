"""Payments flow back: mirror what settled an invoice at the provider.

For a linked invoice, the provider's settlements are the truth:

    TruckWys payments with source=<provider> on the invoice
        ==  provider payments on it
          + overpayment / prepayment allocations to it
          (+ credit-note allocations from credit notes raised IN the provider,
             imported as TruckWys credit notes when they map cleanly)

mirror_invoice() makes TruckWys equal to that set, idempotently:
  new at the provider       -> Payment created (record_payment, source+external_id)
  changed amount / date     -> Payment updated
  gone (deleted / reversed) -> Payment removed
and every change ends in core.services.ledger.recalculate_invoice, so
paid_amount, balance and status always come from the rows.

Allocations of credit notes that TruckWys pushed are ignored here: TruckWys
already counts its own credit note (credited_amount).

Payment.external_id formats (unique per company + source):
    <PaymentID>                         a payment
    OVP:<overpayment id>:<allocation>   an overpayment allocated to the invoice
    PRE:<prepayment id>:<allocation>    a prepayment allocated to the invoice
    OVPREM:<overpayment id>             the unallocated remainder of an
                                        overpayment TruckWys created in backfill
                                        (kept on the invoice it came from)
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.accounting.events import log_event

logger = logging.getLogger(__name__)

PREFIX = {'OVERPAYMENT': 'OVP', 'PREPAYMENT': 'PRE'}
METHOD_NOTE = {'PAYMENT': 'Payment', 'OVERPAYMENT': 'Overpayment allocated', 'PREPAYMENT': 'Prepayment allocated'}


def settlement_key(s) -> str:
    if s.kind == 'PAYMENT':
        return s.external_id
    return f'{PREFIX[s.kind]}:{s.source_id}:{s.external_id}'[:100]


def _our_credit_note_ids(connection):
    from core.models import ExternalLink
    return set(ExternalLink.objects.filter(connection=connection, object_type='CREDIT_NOTE')
               .exclude(external_id='').values_list('external_id', flat=True))


def mirror_invoice(connection, invoice, state, *, adapter=None) -> dict:
    """Make the invoice's provider-sourced payments equal the provider's
    settlements. Returns counts {created, updated, removed, credit_notes}."""
    from core.models import Invoice, Payment
    from core.services.ledger import recalculate_invoice
    from core.services.payments import PaymentError, record_payment

    source = connection.provider
    provider = connection.get_provider_display()
    ours = _our_credit_note_ids(connection)
    desired = {}
    foreign_credit_notes = []
    for s in state.settlements:
        if s.kind == 'CREDIT_NOTE':
            if s.source_id not in ours:
                foreign_credit_notes.append(s)
            continue
        if s.amount <= 0:
            continue
        key = settlement_key(s)
        if key in desired:   # same allocation listed twice: add up
            prev = desired[key]
            desired[key] = prev.__class__(**{**prev.__dict__, 'amount': prev.amount + s.amount})
        else:
            desired[key] = s

    counts = {'created': 0, 'updated': 0, 'removed': 0, 'credit_notes': 0}
    with transaction.atomic():
        inv = Invoice.objects.select_for_update().get(pk=invoice.pk)
        existing = {p.external_id: p for p in Payment.objects.select_for_update().filter(invoice=inv, source=source)
                    if not p.external_id.startswith('OVPREM:')}
        changed = False
        for key, p in existing.items():
            if key not in desired:
                p.delete()
                counts['removed'] += 1
                changed = True
                log_event(connection, 'payment_removed',
                          f'{inv.invoice_number}: payment of R{p.amount} removed in {provider}',
                          level='WARNING', object_type='INVOICE', local_id=inv.pk, label=inv.invoice_number)
        for key, s in desired.items():
            p = existing.get(key)
            if p is not None:
                if p.amount != s.amount or (s.date and p.payment_date != s.date):
                    p.amount = s.amount
                    if s.date:
                        p.payment_date = s.date
                    p.save(update_fields=['amount', 'payment_date', 'updated_at'])
                    counts['updated'] += 1
                    changed = True
                continue
            if inv.status in ('DRAFT', 'CANCELLED'):
                log_event(connection, 'pull_payments',
                          f'{inv.invoice_number} is {inv.get_status_display().lower()} in TruckWys but has a payment '
                          f'in {provider}', level='ERROR', object_type='INVOICE', local_id=inv.pk,
                          label=inv.invoice_number)
                continue
            if changed:
                recalculate_invoice(inv)
                changed = False
            try:
                with transaction.atomic():
                    record_payment(inv.company, None, {
                        'invoice': inv.pk, 'amount': str(s.amount),
                        'payment_date': (s.date or timezone.localdate()).isoformat(),
                        'payment_method': 'EFT', 'source': source, 'external_id': key,
                        'reference_number': (s.reference or s.source_number or '')[:100],
                        'notes': f'{METHOD_NOTE.get(s.kind, "Payment")} in {provider}',
                    }, allow_overpayment=True)
                counts['created'] += 1
            except IntegrityError:
                pass   # a concurrent mirror recorded it first
            except PaymentError as exc:
                log_event(connection, 'pull_payments', f'{inv.invoice_number}: {exc}', level='ERROR',
                          object_type='INVOICE', local_id=inv.pk, label=inv.invoice_number)
        if changed:
            recalculate_invoice(inv)

    for s in foreign_credit_notes:
        if import_foreign_credit_note(connection, invoice, s, adapter=adapter, invoice_external_id=state.external_id):
            counts['credit_notes'] += 1
    if any(counts.values()):
        log_event(connection, 'pull_payments',
                  f'{invoice.invoice_number}: {counts["created"]} new, {counts["updated"]} changed, '
                  f'{counts["removed"]} removed' + (f', {counts["credit_notes"]} credit notes' if counts['credit_notes'] else ''),
                  object_type='INVOICE', local_id=invoice.pk, label=invoice.invoice_number)
    return counts


def import_foreign_credit_note(connection, invoice, settlement, *, adapter=None, invoice_external_id='') -> bool:
    """A credit note raised in the provider and allocated to a TruckWys
    invoice. Imported as a TruckWys credit note only when it is wholly
    allocated to this invoice and every line's tax rate maps back to one
    TruckWys tax code; otherwise it is reported (raise credit notes in
    TruckWys instead)."""
    from core.models import CreditNote, ExternalLink
    from core.accounting import mapping
    from core.accounting.registry import get_adapter
    from core.services.credit_notes import CreditNoteError, create_credit_note

    if CreditNote.objects.filter(company_id=connection.company_id, source=connection.provider,
                                 external_id=settlement.source_id).exists():
        return False
    provider = connection.get_provider_display()
    adapter = adapter or get_adapter(connection)
    try:
        detail = adapter.get_credit_note_detail(settlement.source_id)
    except Exception as exc:
        log_event(connection, 'pull_credit_notes', f'Could not read credit note {settlement.source_number}: {exc}',
                  level='ERROR', object_type='INVOICE', local_id=invoice.pk, label=invoice.invoice_number)
        return False
    reverse = mapping.reverse_sales_tax(connection)
    problem = ''
    allocated_here = sum((a['amount'] for a in detail['allocations']
                          if a['invoice_id'] == invoice_external_id), Decimal('0.00'))
    if detail['remaining'] != 0 or allocated_here != detail['total'] or \
            any(a['invoice_id'] != invoice_external_id for a in detail['allocations']):
        problem = 'it is not allocated in full to this one invoice'
    lines = []
    for l in detail['lines']:
        code = reverse.get(l['tax_code'])
        if code is None:
            problem = problem or f'its tax rate {l["tax_code"]} doesn\'t map to one TruckWys tax code'
        lines.append({'description': l['description'] or 'Credit', 'quantity': '1', 'unit_price': str(l['net']),
                      'tax_code': code or 'STANDARD'})
    if problem:
        log_event(connection, 'pull_credit_notes',
                  f'Credit note {settlement.source_number} in {provider} reduces {invoice.invoice_number} but '
                  f'wasn\'t imported: {problem}. Raise credit notes in TruckWys so both books agree.',
                  level='ERROR', object_type='INVOICE', local_id=invoice.pk, label=invoice.invoice_number)
        return False
    try:
        cn = create_credit_note(invoice, user=None, reason=f'Credit note {settlement.source_number} raised in {provider}',
                                lines=lines, issue_date=detail['date'] or settlement.date,
                                source=connection.provider, external_id=settlement.source_id)
    except CreditNoteError as exc:
        log_event(connection, 'pull_credit_notes', f'Credit note {settlement.source_number}: {exc}', level='ERROR',
                  object_type='INVOICE', local_id=invoice.pk, label=invoice.invoice_number)
        return False
    if cn.total_amount != detail['total']:
        log_event(connection, 'pull_credit_notes',
                  f'Imported {settlement.source_number} as {cn.credit_note_number} but TruckWys VAT gives '
                  f'{cn.total_amount} vs {detail["total"]} in {provider}', level='WARNING',
                  object_type='CREDIT_NOTE', local_id=cn.pk, label=cn.credit_note_number)
    ExternalLink.objects.update_or_create(
        connection=connection, object_type='CREDIT_NOTE', local_id=cn.pk,
        defaults={'company_id': connection.company_id, 'provider': connection.provider,
                  'external_id': settlement.source_id, 'external_number': settlement.source_number,
                  'status': 'SYNCED', 'last_synced_at': timezone.now(),
                  # 'allocated' as for pushed credit notes, so reconciliation
                  # counts it as credit (not money) on the invoice.
                  'meta': {'imported': True, 'allocated': str(allocated_here)}})
    log_event(connection, 'pull_credit_notes', f'Imported {settlement.source_number} as {cn.credit_note_number}',
              object_type='CREDIT_NOTE', local_id=cn.pk, label=cn.credit_note_number)
    return True


def refresh_overpayment_remainders(connection, credits=None, adapter=None) -> int:
    """Overpayments created in backfill sit, unallocated, on the invoice the
    excess came from (Payment external_id OVPREM:<id>). Keep their amount
    equal to the remaining credit at the provider; remove them once fully
    allocated elsewhere (the allocation arrives as an OVP: payment there)."""
    from core.models import ExternalLink, Payment
    from core.accounting.registry import get_adapter
    from core.services.ledger import recalculate_invoice
    links = list(ExternalLink.objects.filter(connection=connection, object_type='OVERPAYMENT', status='SYNCED'))
    if not links:
        return 0
    if credits is None:
        credits = (adapter or get_adapter(connection)).list_unallocated_credits()
    remaining = {c.external_id: c.remaining for c in credits if c.kind == 'OVERPAYMENT'}
    changed = 0
    for link in links:
        left = remaining.get(link.external_id, Decimal('0.00'))
        with transaction.atomic():
            p = Payment.objects.select_for_update().filter(pk=link.local_id).first()
            if p is None:
                continue
            if left <= 0:
                inv = p.invoice
                p.delete()
                recalculate_invoice(inv)
                link.status = 'VOIDED'
                link.save(update_fields=['status', 'updated_at'])
                changed += 1
            elif p.amount != left:
                p.amount = left
                p.save(update_fields=['amount', 'updated_at'])
                recalculate_invoice(p.invoice_id)
                changed += 1
    return changed
