"""Credit notes: the only way to correct an issued invoice."""
from decimal import Decimal

from django.conf import settings
from django.db import models

from core.revenue_types import REVENUE_TYPE_CHOICES, FREIGHT
from core.tax_codes import TAX_CODE_CHOICES, STANDARD


class CreditNote(models.Model):
    """A full or partial reversal of an issued invoice, with its own
    sequential number and VAT per line. An ISSUED credit note reduces the
    invoice balance (core.services.ledger) and reverses revenue and output
    VAT in the period of its own issue_date."""
    ISSUED = 'ISSUED'
    VOID = 'VOID'
    STATUS_CHOICES = [(ISSUED, 'Issued'), (VOID, 'Void')]

    SOURCE_CHOICES = [
        ('MANUAL', 'Entered in TruckWys'),
        ('XERO', 'Xero'),
        ('QBO', 'QuickBooks Online'),
    ]

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='credit_notes')
    credit_note_number = models.CharField(max_length=100, db_index=True)
    invoice = models.ForeignKey('Invoice', on_delete=models.PROTECT, related_name='credit_notes')
    customer = models.ForeignKey('Customer', on_delete=models.PROTECT, related_name='credit_notes')
    issue_date = models.DateField()
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=ISSUED, db_index=True)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='MANUAL')
    external_id = models.CharField(max_length=100, blank=True, default='')

    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='credit_notes_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'credit_notes'
        ordering = ['-issue_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['company', 'credit_note_number'],
                                    name='uniq_credit_note_number_per_company'),
            models.UniqueConstraint(fields=['company', 'source', 'external_id'],
                                    condition=~models.Q(external_id=''),
                                    name='uniq_credit_note_external_id'),
        ]
        indexes = [models.Index(fields=['company', 'issue_date'])]

    def __str__(self):
        return f'{self.credit_note_number} ({self.invoice_id})'


class CreditNoteLine(models.Model):
    credit_note = models.ForeignKey(CreditNote, on_delete=models.CASCADE, related_name='lines')
    invoice_line = models.ForeignKey('InvoiceLine', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='credit_note_lines')
    position = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('1'))
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)
    tax_code = models.CharField(max_length=20, choices=TAX_CODE_CHOICES, default=STANDARD)
    # Mirrors the credited invoice line's type (same income account).
    revenue_type = models.CharField(max_length=20, choices=REVENUE_TYPE_CHOICES, default=FREIGHT)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15.00'))
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = 'credit_note_lines'
        ordering = ['credit_note_id', 'position', 'id']
