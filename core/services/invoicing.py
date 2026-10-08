"""Invoice creation from loads — the shared spine for both the manual
'convert to invoice' button and the automatic delivery → invoice flow.

Keeping one function means the manual and automatic paths can never drift
(same VAT, terms, numbering, fast-pay eligibility).
"""
import logging
from datetime import date
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)


def _unique_invoice_number() -> str:
    """A provisional number for a new draft. The sequential number is
    allocated when the invoice is issued (core.services.numbering)."""
    from core.services.numbering import provisional_number
    return provisional_number()


def create_invoice_for_load(load, *, company=None, mark_sent: bool = False):
    """Create an invoice for a delivered load. Idempotent and defensive.

    Returns (invoice, created). If an invoice already exists for the load it is
    returned with created=False. Returns (None, False) when the load isn't
    invoiceable (no customer, no value, or cancelled).

    Deliberately does NOT gate on subscription_status here — this function is
    shared by two callers that must behave differently: the automatic
    delivery signal (core.signals._auto_invoice_on_delivery), which should
    keep raising real invoices for the carrier's own customer even if
    TruckWys's own subscription has lapsed, and the manual
    LoadViewSet.convert_to_invoice action, which enforces the
    suspended/cancelled block itself before ever calling this. Putting the
    check here would have silently blocked both.
    """
    from core.models.invoice import Invoice

    existing = Invoice.objects.filter(load=load).first()
    if existing:
        return existing, False

    # Guard: only invoice loads that can actually produce a valid invoice.
    if getattr(load, 'status', None) == 'CANCELLED':
        return None, False
    if not getattr(load, 'customer', None):
        return None, False
    subtotal = load.total_amount or Decimal('0')
    if subtotal <= 0:
        return None, False

    from core.services.invoice_lines import apply_lines, customer_terms, due_date_for, load_tax_code
    from django.db import transaction

    company = company or getattr(load, 'company', None)
    today = date.today()
    terms = customer_terms(load.customer)
    with transaction.atomic():
        invoice = Invoice(
            invoice_number=_unique_invoice_number(),
            company=company,
            customer=load.customer,
            load=load,
            issue_date=today,
            # The customer's own terms, not a hard-coded NET30.
            payment_terms=terms,
            due_date=due_date_for(today, terms),
            subtotal=Decimal('0'), vat_amount=Decimal('0'), total_amount=Decimal('0'),
            paid_amount=Decimal('0'), balance=Decimal('0'),
            status='DRAFT',
            notes=f'Auto-generated from Load {load.load_number}',
            early_pay_eligible=True,
        )
        from core.services.invoice_lines import terms_days_for
        invoice.terms_days = terms_days_for(terms)
        lines = [{
            'description': _load_line_description(load),
            'quantity': 1,
            'unit_price': Decimal(str(subtotal)),
            # The company's default code (STANDARD for a VAT vendor, NO_VAT
            # otherwise); an international load is zero-rated (s11(2)(a)),
            # matching the VAT 0% its quote showed the customer.
            'tax_code': load_tax_code(load, company),
            'load': load.pk,
        }]
        # Fuel price clause (core.services.fuel_surcharge): when the load's
        # quote went out with the clause and the official price on the trip
        # date moved past the threshold, add "Fuel price adjustment (diesel
        # R 32,80 → R 34,10/L)" (up) or discount the freight line (down).
        from core.services.fuel_surcharge import apply_to_invoice_lines
        apply_to_invoice_lines(load, lines)
        apply_lines(invoice, lines)
        if mark_sent:
            invoice.status = 'SENT'
            invoice.sent_at = timezone.now()
            invoice.save()

    # Flip the load to INVOICED without re-firing the Load post_save signal
    # (we may be called from inside that very signal — avoid re-entrancy).
    from core.models import Load
    Load.objects.filter(pk=load.pk).update(status='INVOICED')

    return invoice, True


def _load_line_description(load) -> str:
    route = ' → '.join(p for p in (getattr(load, 'pickup_city', '') or '', getattr(load, 'delivery_city', '') or '') if p)
    return f'Transport: load {load.load_number}' + (f' ({route})' if route else '')


def email_invoice_to_customer(invoice, additional_recipients=None) -> bool:
    """Email the invoice (PDF + view link) to its customer. On success the
    invoice is SENT with sent_at stamped (InvoiceEmailService). Shared by the
    Send action and auto-email on delivery. Raises if the PDF can't be built;
    returns False if the email didn't go out."""
    import secrets
    from core.services.email_service import InvoiceEmailService
    from core.services.pdf_generator import InvoicePDFGenerator

    # Issue it first so the PDF and email carry the real sequential number.
    if invoice.status == 'DRAFT':
        invoice.status = 'SENT'
        invoice.sent_at = timezone.now()
        invoice.save()
    if not invoice.pdf_file:
        invoice.pdf_file = InvoicePDFGenerator.generate_pdf(invoice)
        invoice.save()
    if not invoice.view_token:
        invoice.view_token = secrets.token_urlsafe(32)
        invoice.save(update_fields=['view_token'])
    return InvoiceEmailService.send_invoice(
        invoice=invoice, pdf_path=str(invoice.pdf_file), additional_recipients=additional_recipients or [],
    )
