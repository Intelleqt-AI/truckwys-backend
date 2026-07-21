"""Shared payment recording logic.

Used by both PaymentFinanceViewSet.create and the Copilot propose/execute path so
the invoice-balance invariant lives in exactly one place.
"""
import random
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone


class PaymentError(Exception):
    """Validation failure with a user-friendly message."""
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def record_payment(company, user, data):
    """Validate and record a payment against an invoice, updating its balance/status.

    data: dict with at least {invoice, amount}; customer/payment_number are
    auto-filled; 'reference' maps to reference_number. Locks the invoice row so
    two concurrent payments can't both pass the balance check and overpay.

    Returns the bound PaymentSerializer (access .instance / .data).
    Raises PaymentError with a friendly message on any validation failure.
    """
    from core.models import Invoice
    from core.serializers import PaymentSerializer

    try:
        amount = Decimal(str(data.get('amount', '0')))
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentError('Payment amount is not a valid number')

    if amount <= 0:
        raise PaymentError('Payment amount must be greater than zero')

    with transaction.atomic():
        try:
            invoice = Invoice.objects.select_for_update().get(
                id=data.get('invoice'), company=company
            )
        except Invoice.DoesNotExist:
            raise PaymentError('Invoice not found', status_code=404)

        if amount > invoice.balance:
            raise PaymentError(
                f'Payment amount (R {amount}) exceeds invoice balance (R {invoice.balance})'
            )

        # Fill in what the caller shouldn't have to: customer (from the
        # invoice), an auto payment_number, and accept the UI's 'reference'.
        payload = {k: v for k, v in data.items()}
        payload['invoice'] = invoice.id
        payload['company'] = company.id
        payload.setdefault('customer', invoice.customer_id)
        if not payload.get('payment_number'):
            payload['payment_number'] = f"PAY-{timezone.now():%Y%m%d}-{random.randint(1000, 9999)}"
        if payload.get('reference') and not payload.get('reference_number'):
            payload['reference_number'] = payload['reference']

        serializer = PaymentSerializer(data=payload)
        if not serializer.is_valid():
            first_field, msgs = next(iter(serializer.errors.items()))
            raise PaymentError(f"{first_field}: {msgs[0] if isinstance(msgs, list) else msgs}")
        serializer.save()

        invoice.paid_amount += amount
        invoice.balance -= amount
        if invoice.balance == 0:
            invoice.status = 'PAID'
            invoice.paid_at = timezone.now()
        elif invoice.paid_amount > 0:
            invoice.status = 'PARTIALLY_PAID'
        invoice.save()

    return serializer


def reverse_payment(company, payment):
    """Delete a payment and restore the invoice's paid/balance/status. Locked."""
    from core.models import Invoice

    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(id=payment.invoice_id, company=company)
        invoice.paid_amount -= payment.amount
        invoice.balance += payment.amount
        if invoice.paid_amount <= 0:
            invoice.paid_amount = max(invoice.paid_amount, Decimal('0'))
            invoice.status = 'SENT' if invoice.sent_at else 'DRAFT'
            invoice.paid_at = None
        else:
            invoice.status = 'PARTIALLY_PAID'
        invoice.save()
        payment.delete()
