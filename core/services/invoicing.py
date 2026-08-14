"""Invoice creation from loads — the shared spine for both the manual
'convert to invoice' button and the automatic delivery → invoice flow.

Keeping one function means the manual and automatic paths can never drift
(same VAT, terms, numbering, fast-pay eligibility).
"""
import logging
import random
from datetime import date, timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)


def _unique_invoice_number() -> str:
    from core.models.invoice import Invoice
    today = date.today()
    num = f'INV-{today.strftime("%Y%m%d")}-{random.randint(10000, 99999):05d}'
    while Invoice.objects.filter(invoice_number=num).exists():
        num = f'INV-{today.strftime("%Y%m%d")}-{random.randint(10000, 99999):05d}'
    return num


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

    today = date.today()
    vat = (Decimal(str(subtotal)) * Decimal('0.15')).quantize(Decimal('0.01'))
    total = Decimal(str(subtotal)) + vat

    invoice = Invoice.objects.create(
        invoice_number=_unique_invoice_number(),
        company=company or getattr(load, 'company', None),
        customer=load.customer,
        load=load,
        issue_date=today,
        due_date=today + timedelta(days=30),
        subtotal=subtotal,
        vat_amount=vat,
        tax_amount=vat,
        total_amount=total,
        paid_amount=Decimal('0'),
        balance=total,
        status='SENT' if mark_sent else 'DRAFT',
        payment_terms='NET30',
        notes=f'Auto-generated from Load {load.load_number}',
        early_pay_eligible=True,
    )

    # Flip the load to INVOICED without re-firing the Load post_save signal
    # (we may be called from inside that very signal — avoid re-entrancy).
    from core.models import Load
    Load.objects.filter(pk=load.pk).update(status='INVOICED')

    return invoice, True
