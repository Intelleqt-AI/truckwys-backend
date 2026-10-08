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
    from core.services.tonnage_jobs import AWAITING_WEIGHBRIDGE, load_billing
    billing = load_billing(load)
    subtotal = Decimal(str(billing['amount'])) if billing else (load.total_amount or Decimal('0'))
    if subtotal <= 0:
        return None, False

    from core.services.invoice_lines import apply_lines, customer_terms, due_date_for
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
        if billing and billing['awaiting_weighbridge']:
            # Per tonne without the weighbridge figure: the planned tonnes,
            # flagged (never silently), and never sent as it stands.
            invoice.notes = f'{invoice.notes}\n{AWAITING_WEIGHBRIDGE}: invoiced on planned tonnes.'
            mark_sent = False
        apply_lines(invoice, invoice_lines_for_load(load, company))
        if mark_sent:
            invoice.status = 'SENT'
            invoice.sent_at = timezone.now()
            invoice.save()

    # Flip the load to INVOICED without re-firing the Load post_save signal
    # (we may be called from inside that very signal — avoid re-entrancy).
    from core.models import Load
    Load.objects.filter(pk=load.pk).update(status='INVOICED')
    load.status = 'INVOICED'      # the caller's instance (and its API response) shows the truth

    return invoice, True


def invoice_lines_for_load(load, company=None):
    """The invoice line(s) a load is billed on: ONE place, shared by the
    delivery auto-invoice, the manual convert and the booking preview, so the
    preview is exactly what gets raised.

    Per-tonne loads: quantity = max(weighbridge tonnes, minimum) (planned
    tonnes while awaiting the weighbridge) and unit_price = rate per tonne;
    everything downstream (VAT, totals, preview) follows."""
    from core.services.invoice_lines import load_tax_code
    from core.services.fuel_surcharge import apply_to_invoice_lines
    from core.services.tonnage_jobs import invoice_line_for_load, load_billing
    company = company or getattr(load, 'company', None)
    if load_billing(load) is not None:
        # Per tonne: quantity = max(weighbridge tonnes, minimum), else the
        # planned tonnes (flagged "Awaiting weighbridge tonnes").
        lines = [{**invoice_line_for_load(load, _load_line_description(load)),
                  'tax_code': load_tax_code(load, company), 'load': load.pk}]
    else:
        lines = [{
            'description': _load_line_description(load),
            'quantity': 1,
            'unit_price': Decimal(str(load.total_amount or 0)),
            # The company's default code (STANDARD for a VAT vendor, NO_VAT
            # otherwise); an international load is zero-rated (s11(2)(a)),
            # matching the VAT 0% its quote showed the customer.
            'tax_code': load_tax_code(load, company),
            'load': load.pk,
        }]
    # Fuel price clause (core.services.fuel_surcharge): when the load's quote
    # went out with the clause and the official price on the trip date moved
    # past the threshold, add "Fuel price adjustment (diesel R 32,80 →
    # R 34,10/L)" (up) or discount the freight line (down). Per tonne: the
    # litres follow the load's billed tonnes. Here, so the booking preview,
    # the manual convert, the delivery auto-invoice and a weighbridge
    # re-price all carry the same adjustment.
    apply_to_invoice_lines(load, lines)
    return lines


def invoice_preview(load):
    """What the delivery auto-invoice will be for this load (or the invoice
    already raised). Never writes."""
    from django.conf import settings
    from core.models.invoice import Invoice
    from core.services.invoice_lines import build_lines, customer_terms, terms_days_for, totals_of
    existing = (Invoice.objects.filter(load=load).exclude(status='CANCELLED').order_by('-id').first()
                if load.pk is not None else None)
    if existing is not None:
        return {'state': 'raised', 'invoice_id': existing.pk, 'invoice_number': existing.invoice_number,
                'status': existing.status, 'subtotal': float(existing.subtotal),
                'vat_amount': float(existing.vat_amount), 'total': float(existing.total_amount),
                'mismatch': getattr(load, 'invoice_mismatch', None) or None}
    company = getattr(load, 'company', None)
    auto = bool(getattr(settings, 'AUTO_INVOICE_ON_DELIVERY', True))
    if load.status == 'CANCELLED' or not (load.total_amount and load.total_amount > 0):
        return {'state': 'not_invoiceable', 'auto_on_delivery': auto,
                'reason': 'cancelled' if load.status == 'CANCELLED' else 'no_amount'}
    raw = invoice_lines_for_load(load, company)
    lines = build_lines([{**ln, 'load': None} for ln in raw], company=company, on_date=date.today())
    t = totals_of(lines)
    terms = customer_terms(load.customer)
    return {
        'state': 'on_delivery' if auto else 'manual',
        'auto_on_delivery': auto,
        'auto_email': bool(getattr(company, 'auto_email_invoices', False)),
        'lines': [{'description': ln['description'], 'quantity': float(ln['quantity']),
                   'unit_price': float(ln['unit_price']), 'tax_code': ln['tax_code'],
                   'tax_rate': float(ln['tax_rate']), 'net_amount': float(ln['net_amount']),
                   'vat_amount': float(ln['vat_amount']), 'total': float(ln['total_amount'])} for ln in lines],
        'subtotal': float(t['subtotal']), 'vat_amount': float(t['vat_amount']), 'total': float(t['total_amount']),
        'payment_terms': terms, 'terms_days': terms_days_for(terms),
        'billing_basis': 'per_load',     # the per-tonne branch sets 'per_tonne'
    }


def _load_line_description(load) -> str:
    route = ' → '.join(p for p in (getattr(load, 'pickup_city', '') or '', getattr(load, 'delivery_city', '') or '') if p)
    head = f'Transport: load {load.load_number}' if load.load_number else 'Transport (load number on booking)'
    return head + (f' ({route})' if route else '')


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
