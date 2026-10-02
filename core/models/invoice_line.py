"""Typed invoice lines: the source of every invoice's totals."""
from decimal import Decimal

from django.db import models

from core.revenue_types import REVENUE_TYPE_CHOICES, FREIGHT
from core.tax_codes import TAX_CODE_CHOICES, STANDARD


class InvoiceLine(models.Model):
    """One line of an invoice. net/vat/total are stored as computed by
    core.tax_codes.compute_line at the time the line was written, so an
    issued invoice never changes if a rate or rule changes later."""
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE, related_name='lines')
    position = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('1'))
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)
    # Discount in rand, EXCLUDING VAT, taken before VAT is calculated.
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    # The percent the user entered, if any (discount_amount is derived from it).
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    tax_code = models.CharField(max_length=20, choices=TAX_CODE_CHOICES, default=STANDARD)
    # What the line charges for; decides the income account in Xero/QBO.
    revenue_type = models.CharField(max_length=20, choices=REVENUE_TYPE_CHOICES, default=FREIGHT)
    # Rate actually applied, as a percent (15.00), frozen at write time.
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15.00'))
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    # The load this line bills (multi-load invoices); invoice.load stays the primary.
    load = models.ForeignKey('Load', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_lines')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'invoice_lines'
        ordering = ['invoice_id', 'position', 'id']

    def __str__(self):
        return f'{self.invoice_id}#{self.position} {self.description[:40]}'
