from django.db import models
from django.utils import timezone
from datetime import date
from decimal import Decimal
from .customer import Customer
from .load import Load


class Invoice(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SENT', 'Sent'),
        ('VIEWED', 'Viewed'),
        ('PAID', 'Paid'),
        ('PARTIALLY_PAID', 'Partially Paid'),
        ('OVERDUE', 'Overdue'),
        ('CANCELLED', 'Cancelled'),
        ('DISPUTED', 'Disputed'),
    ]

    PAYMENT_TERMS_CHOICES = [
        ('NET30', 'Net 30 Days'),
        ('NET60', 'Net 60 Days'),
        ('NET90', 'Net 90 Days'),
    ]

    # Core fields
    invoice_number = models.CharField(max_length=100, unique=True, db_index=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='invoices')
    load = models.ForeignKey(Load, on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')

    # NEW: Link to Trip
    trip = models.ForeignKey(
        'Trip',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoices',
        help_text='Trip this invoice is for'
    )

    # Dates
    issue_date = models.DateField(default=date.today)
    due_date = models.DateField()

    # NEW: Payment terms
    payment_terms = models.CharField(
        max_length=20,
        choices=PAYMENT_TERMS_CHOICES,
        default='NET30',
        help_text='Payment terms for this invoice'
    )

    # Financial amounts
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    # NEW: Explicit VAT amount (15% for South Africa)
    vat_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text='VAT amount (15% in South Africa)'
    )

    # Keep existing tax fields for backward compatibility
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=15)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=10, decimal_places=2)

    # Status
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='DRAFT', db_index=True)

    # NEW: Early payment eligibility
    early_pay_eligible = models.BooleanField(
        default=False,
        help_text='Whether this invoice is eligible for early payment'
    )
    early_pay_offered = models.BooleanField(
        default=False,
        help_text='Whether early payment has been offered for this invoice'
    )

    # NEW: PDF file
    pdf_file = models.FileField(
        upload_to='invoices/%Y/%m/',
        null=True,
        blank=True,
        help_text='Generated invoice PDF'
    )

    # NEW: Line items JSON storage
    line_items = models.JSONField(
        default=list,
        blank=True,
        help_text='Invoice line items with descriptions, quantities, and amounts'
    )

    # Notes
    notes = models.TextField(blank=True)

    # NEW: Timestamp tracking
    sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the invoice was sent to the customer'
    )
    viewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the invoice was first viewed by customer'
    )
    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the invoice was fully paid'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'invoices'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['invoice_number']),
            models.Index(fields=['status']),
            models.Index(fields=['due_date']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f"Invoice {self.invoice_number} - {self.customer.name}"

    @property
    def age_days(self) -> int:
        """Calculate invoice age in days."""
        return (date.today() - self.issue_date).days

    @property
    def is_overdue(self) -> bool:
        """Check if invoice is overdue."""
        return date.today() > self.due_date and self.status not in ['PAID', 'CANCELLED']

    @property
    def days_until_due(self) -> int:
        """Calculate days until due (negative if overdue)."""
        return (self.due_date - date.today()).days

    def calculate_vat(self) -> Decimal:
        """Calculate VAT amount (15% for South Africa)."""
        vat_rate = Decimal('0.15')  # 15% VAT
        return (self.subtotal * vat_rate).quantize(Decimal('0.01'))

    def calculate_total(self) -> Decimal:
        """Calculate total amount including VAT and discount."""
        return (self.subtotal + self.vat_amount - self.discount).quantize(Decimal('0.01'))

    def mark_as_sent(self) -> None:
        """Mark invoice as sent."""
        if self.status == 'DRAFT':
            self.status = 'SENT'
            self.sent_at = timezone.now()
            self.save()

    def mark_as_viewed(self) -> None:
        """Mark invoice as viewed by customer."""
        if self.status == 'SENT' and not self.viewed_at:
            self.status = 'VIEWED'
            self.viewed_at = timezone.now()
            self.save()

    def mark_as_paid(self) -> None:
        """Mark invoice as fully paid."""
        if self.status not in ['PAID', 'CANCELLED']:
            self.status = 'PAID'
            self.paid_at = timezone.now()
            self.paid_amount = self.total_amount
            self.balance = Decimal('0.00')
            self.save()

    def save(self, *args, **kwargs) -> None:
        """Override save to auto-calculate amounts."""
        # Auto-calculate VAT if not set
        if self.vat_amount == 0 and self.subtotal > 0:
            self.vat_amount = self.calculate_vat()

        # Keep tax_amount in sync with vat_amount for backward compatibility
        self.tax_amount = self.vat_amount

        # Auto-calculate total
        self.total_amount = self.calculate_total()

        # Auto-calculate balance
        self.balance = self.total_amount - self.paid_amount

        # Auto-update status based on payment
        if self.balance == 0 and self.paid_amount > 0:
            self.status = 'PAID'
        elif self.paid_amount > 0 and self.balance > 0:
            self.status = 'PARTIALLY_PAID'
        elif self.is_overdue and self.status not in ['PAID', 'CANCELLED', 'DISPUTED']:
            self.status = 'OVERDUE'

        super().save(*args, **kwargs)
