"""The invoice ledger: paid, credited, balance and status in one place.

Every write that moves money against an invoice (payment create / edit /
delete, credit note issue / void, invoice void) ends in
recalculate_invoice(), which re-derives the figures from the payment and
credit note rows under a row lock. Nothing adds or subtracts deltas, so an
edit or delete can never leave a stale balance behind.

    balance = total_amount - paid_amount - credited_amount

A negative balance is a credit the customer is owed (overpayment, or a
credit note against a paid invoice). It is never shown in debtors ageing;
accounting_reports reports it as customer credit.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

ZERO = Decimal('0.00')


def _base_status(invoice):
    if invoice.due_date and invoice.due_date < date.today():
        return 'OVERDUE'
    return 'VIEWED' if invoice.viewed_at else 'SENT'


def apply_ledger_status(invoice):
    """Derive balance and status from the in-memory paid/credited figures.
    Called from Invoice.save(), so it must not query anything but the
    payments (for paid_at) and must leave drafts and void invoices alone."""
    paid = invoice.paid_amount or ZERO
    credited = invoice.credited_amount or ZERO
    invoice.balance = (invoice.total_amount or ZERO) - paid - credited

    if invoice.status in ('DRAFT', 'CANCELLED'):
        return

    settled = invoice.balance <= 0 and (paid > 0 or credited > 0)
    if settled:
        if paid > 0:
            invoice.status = 'PAID'
            if invoice.paid_at is None:
                last = None
                if invoice.pk:
                    last = (invoice.payments.order_by('-payment_date')
                            .values_list('payment_date', flat=True).first())
                from core.models.invoice import paid_at_for
                invoice.paid_at = paid_at_for(last)
        else:
            invoice.status = 'CREDITED'
            invoice.paid_at = None
        return

    invoice.paid_at = None
    if invoice.status == 'DISPUTED':
        return
    if paid > 0:
        invoice.status = 'PARTIALLY_PAID'
        return
    invoice.status = _base_status(invoice)


def recalculate_invoice(invoice_or_id, *, actor_id=None):
    """Re-derive paid/credited/balance/status from the rows, under a lock.
    Returns the saved invoice. Must run inside the caller's transaction when
    the caller has just written a payment or credit note."""
    from core.models import Invoice, CreditNote

    invoice_id = getattr(invoice_or_id, 'pk', invoice_or_id)
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        paid = invoice.payments.aggregate(t=Sum('amount'))['t'] or ZERO
        credited = (CreditNote.objects.filter(invoice=invoice, status=CreditNote.ISSUED)
                    .aggregate(t=Sum('total_amount'))['t'] or ZERO)
        # A fresh look at paid_at: a removed settling payment must not leave
        # the old date behind.
        if paid != invoice.paid_amount:
            invoice.paid_at = None
        invoice.paid_amount = paid
        invoice.credited_amount = credited
        if actor_id is not None:
            invoice._notify_actor_id = actor_id
        invoice.save()
        return invoice
