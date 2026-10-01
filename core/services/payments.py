"""Shared payment recording logic.

Used by both PaymentFinanceViewSet.create and the Copilot propose/execute path so
the invoice-balance invariant lives in exactly one place.
"""
import random
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.formatting import format_zar


class PaymentError(Exception):
    """Validation failure with a user-friendly message. `payload` (optional)
    is the full error body for the API (code, provider, ...)."""
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    def as_response_body(self):
        return dict(self.payload) if self.payload else {'error': str(self)}


def _refuse_if_managed(company, invoice=None):
    """Manual payments are refused while Xero/QBO manages payments."""
    from core.accounting.guards import payments_managed_error
    body = payments_managed_error(company, invoice)
    if body:
        raise PaymentError(body['error'], status_code=409, payload=body)


def _parse_amount(value):
    try:
        amount = Decimal(str(value if value is not None else '0'))
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentError('Payment amount is not a valid number')
    if amount <= 0:
        raise PaymentError('Payment amount must be greater than zero')
    return amount.quantize(Decimal('0.01'))


def record_payment(company, user, data, *, allow_overpayment=False):
    """Validate and record a payment against an invoice, then re-derive the
    invoice ledger (core.services.ledger.recalculate_invoice).

    data: dict with at least {invoice, amount}; customer/payment_number are
    auto-filled; 'reference' maps to reference_number. Optional source
    (MANUAL/XERO/QBO/BANK) + external_id make the call idempotent: the same
    (company, source, external_id) returns the payment already recorded.
    Locks the invoice row so two concurrent payments can't both pass the
    balance check. Overpayment is refused unless allow_overpayment (accounting
    and bank syncs record what actually arrived; the excess shows as a
    customer credit).

    Returns the bound PaymentSerializer (access .instance / .data).
    Raises PaymentError with a friendly message on any validation failure.
    """
    from core.models import Invoice, Payment
    from core.serializers import PaymentSerializer
    from core.services.ledger import recalculate_invoice

    if company is None:
        raise PaymentError('Your account is not linked to a company', status_code=403)
    amount = _parse_amount(data.get('amount', '0'))
    source = (data.get('source') or 'MANUAL').upper()
    if source not in dict(Payment.SOURCE_CHOICES):
        raise PaymentError(f'Unknown payment source {source!r}')
    external_id = str(data.get('external_id') or '').strip()

    with transaction.atomic():
        try:
            invoice = Invoice.objects.select_for_update().get(
                id=data.get('invoice'), company=company
            )
        except (Invoice.DoesNotExist, ValueError, TypeError):
            raise PaymentError('Invoice not found', status_code=404)

        if external_id:
            existing = Payment.objects.filter(company=company, source=source, external_id=external_id).first()
            if existing is not None:
                return PaymentSerializer(existing, context={'company': company})
        if source == 'MANUAL':
            _refuse_if_managed(company, invoice)

        if invoice.status == 'CANCELLED':
            raise PaymentError('This invoice is void; a payment can\'t be recorded against it')
        if invoice.status == 'DRAFT':
            # A draft isn't owed yet (and isn't revenue); cash against it would
            # show in cash reports with no invoice behind it.
            raise PaymentError('Send the invoice before recording a payment against it')
        if amount > invoice.balance and not allow_overpayment:
            raise PaymentError(
                f'Payment amount ({format_zar(amount)}) exceeds invoice balance ({format_zar(invoice.balance)})'
            )

        # Fill in what the caller shouldn't have to: customer (from the
        # invoice), an auto payment_number, and accept the UI's 'reference'.
        payload = {k: v for k, v in data.items() if k not in ('source', 'external_id', 'company')}
        payload['invoice'] = invoice.id
        payload['amount'] = str(amount)
        payload.setdefault('customer', invoice.customer_id)
        if not payload.get('payment_number'):
            payload['payment_number'] = _payment_number()
        if payload.get('reference') and not payload.get('reference_number'):
            payload['reference_number'] = payload['reference']

        # context company: scopes invoice/customer ids to this tenant (a
        # caller-supplied customer from another company is rejected); company
        # itself is read-only on the serializer, so it is set on save.
        serializer = PaymentSerializer(data=payload, context={
            'company': company,
            # The invoice's own customer is server-derived and always valid,
            # even for a legacy customer row that predates company backfill.
            'allow_relation_ids': {'customer': invoice.customer_id},
        })
        if not serializer.is_valid():
            first_field, msgs = next(iter(serializer.errors.items()))
            raise PaymentError(f"{first_field}: {msgs[0] if isinstance(msgs, list) else msgs}")
        if serializer.validated_data.get('customer') and serializer.validated_data['customer'].pk != invoice.customer_id:
            raise PaymentError('customer: must be the invoice\'s customer')
        serializer.save(company=company, source=source, external_id=external_id)

        # Read by the invoice.paid signal handler to exclude whoever recorded
        # this payment — same convention as booking/quote/advance actor exclusion.
        invoice = recalculate_invoice(invoice, actor_id=getattr(user, 'id', None))
        fully_paid = invoice.balance <= 0

    # Full payments fire invoice.paid via the invoice post_save signal, so only
    # the partial case emits payment.received here.
    if not fully_paid:
        try:
            from core.services.notify import notify_company
            from core.services.notify_copy import customer_name, money, join_parts
            detail = join_parts(
                invoice.invoice_number, customer_name(invoice),
                f'{money(amount)} received · {money(invoice.balance)} outstanding',
            )
            notify_company(
                invoice.company_id, 'INFO', '💰 Payment received',
                detail,
                link=f"/finance/invoices/{invoice.id}", event='payment.received',
                exclude_user_id=getattr(user, 'id', None),
            )
        except Exception:
            pass

    return serializer


def _payment_number():
    from core.models import Payment
    for _ in range(20):
        num = f"PAY-{timezone.now():%Y%m%d}-{random.randint(10000, 99999)}"
        if not Payment.objects.filter(payment_number=num).exists():
            return num
    return f"PAY-{timezone.now():%Y%m%d%H%M%S%f}"


EDITABLE_FIELDS = ('amount', 'payment_date', 'payment_method', 'reference_number', 'notes')


def update_payment(company, user, payment, data, *, allow_overpayment=False):
    """Edit a MANUAL payment's amount/date/method/reference/notes and
    re-derive its invoice. The invoice and customer can't change (re-pointing
    a payment is a delete + new payment), and synced payments are edited in
    the system they came from."""
    from core.models import Invoice, Payment
    from core.serializers import PaymentSerializer
    from core.services.ledger import recalculate_invoice

    for forbidden in ('invoice', 'customer', 'company', 'source', 'external_id'):
        if forbidden in data and str(data[forbidden]) != str(getattr(payment, f'{forbidden}_id', getattr(payment, forbidden, None))):
            raise PaymentError(f'{forbidden}: can\'t be changed on a recorded payment')
    if payment.source != 'MANUAL':
        raise PaymentError(f'This payment was synced from {payment.get_source_display()}; change it there')
    _refuse_if_managed(company, payment.invoice)

    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id, company=company)
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        changes = {k: data[k] for k in EDITABLE_FIELDS if k in data}
        if 'amount' in changes:
            new_amount = _parse_amount(changes['amount'])
            headroom = invoice.balance + payment.amount
            if new_amount > headroom and not allow_overpayment:
                raise PaymentError(
                    f'Payment amount ({format_zar(new_amount)}) exceeds invoice balance ({format_zar(headroom)})')
            changes['amount'] = str(new_amount)
        serializer = PaymentSerializer(payment, data=changes, partial=True, context={'company': company})
        if not serializer.is_valid():
            first_field, msgs = next(iter(serializer.errors.items()))
            raise PaymentError(f"{first_field}: {msgs[0] if isinstance(msgs, list) else msgs}")
        serializer.save()
        recalculate_invoice(invoice, actor_id=getattr(user, 'id', None))
    return serializer


def reverse_payment(company, payment, user=None):
    """Delete a MANUAL payment and re-derive its invoice. Locked."""
    from core.models import Invoice
    from core.services.ledger import recalculate_invoice

    if payment.source != 'MANUAL':
        raise PaymentError(f'This payment was synced from {payment.get_source_display()}; remove it there')
    _refuse_if_managed(company, payment.invoice)
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(id=payment.invoice_id, company=company)
        payment.delete()
        recalculate_invoice(invoice, actor_id=getattr(user, 'id', None))
