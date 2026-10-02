"""Capital guards shared by invoicing, capital and lender code.

An invoice is *financed* once an advance on it has been approved or paid out:
from that point a funder is relying on its amount, debtor and dates, so the
invoice must not be edited (audit §6 #6). Invoice serializers/views import
``assert_invoice_not_financed`` to enforce the lock; capital code uses
``invoice_is_fundable_status`` / ``load_has_pod_evidence`` so every create path
applies the same rules.
"""

from __future__ import annotations

# Statuses where a funder has committed money (or is about to pay it out).
FINANCED_ADVANCE_STATUSES = ('APPROVED', 'DISBURSED')

# Invoice statuses an advance may be raised against: issued to the debtor and
# still collectable. DRAFT was never sent, PAID/CANCELLED have nothing to
# collect, DISPUTED is exactly the invoice a funder must not buy.
FUNDABLE_INVOICE_STATUSES = ('SENT', 'VIEWED', 'OVERDUE', 'PARTIALLY_PAID')


class InvoiceFinancedError(Exception):
    """Raised when a financed (advanced) invoice would be modified."""

    def __init__(self, invoice, message: str | None = None):
        self.invoice = invoice
        super().__init__(message or (
            f'Invoice {getattr(invoice, "invoice_number", invoice)} is financed by a capital '
            'advance and cannot be changed. Contact the capital desk.'
        ))


def _invoice_is_financed(invoice) -> bool:
    if invoice is None or getattr(invoice, 'pk', None) is None:
        return False
    from core.models import AdvanceRequest
    return AdvanceRequest.objects.filter(
        invoice_id=invoice.pk, status__in=FINANCED_ADVANCE_STATUSES,
    ).exists()


def is_invoice_financed(invoice) -> bool:
    """True if an APPROVED or DISBURSED advance exists on this invoice."""
    return _invoice_is_financed(invoice)


def assert_invoice_not_financed(invoice) -> None:
    """Raise InvoiceFinancedError if the invoice is financed."""
    if _invoice_is_financed(invoice):
        raise InvoiceFinancedError(invoice)


def invoice_is_fundable_status(invoice) -> bool:
    return getattr(invoice, 'status', None) in FUNDABLE_INVOICE_STATUSES


def load_has_pod_evidence(load) -> bool:
    """A load counts as proven delivered only with a stored POD file or a
    captured signature. The upload endpoint no longer fakes a signature, so a
    PATCHed text field can't stand in for evidence."""
    if load is None:
        return False
    return bool(getattr(load, 'pod_document', None) or getattr(load, 'pod_signature', ''))


def financing_block_reason(invoice) -> str | None:
    """Why this invoice cannot be advanced against, or None.

    Covers the structural rules every create path shares (status, delivered
    load with POD). Risk/credit rules stay in the risk engine.
    """
    if not invoice_is_fundable_status(invoice):
        return f'Invoice status {invoice.status} is not eligible for an advance'
    load = getattr(invoice, 'load', None)
    if load is None:
        return 'Invoice has no load; only delivered loads can be financed'
    if not load_has_pod_evidence(load):
        return 'No proof of delivery on file for this load'
    return None
