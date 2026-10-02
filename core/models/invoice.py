from django.db import models
from django.utils import timezone
from datetime import date
from decimal import Decimal
from .customer import Customer
from .load import Load


def paid_at_for(payment_date):
    """When an invoice counts as paid, from the payment that settled it.

    The payment's own date, not the moment it was recorded: an EFT from last
    week recorded today is last week's revenue (the frontend ledgers date cash
    by payment_date too). Noon local time keeps the day intact in UTC. Today
    or no date: now."""
    from datetime import datetime, time
    today = timezone.localdate()
    if payment_date is None or payment_date >= today:
        return timezone.now()
    return timezone.make_aware(datetime.combine(payment_date, time(12, 0)))


class Invoice(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SENT', 'Sent'),
        ('VIEWED', 'Viewed'),
        ('PAID', 'Paid'),
        ('PARTIALLY_PAID', 'Partially Paid'),
        ('OVERDUE', 'Overdue'),
        # Shown as "Void": an issued invoice is never deleted, it is voided
        # (only when nothing has been paid or credited against it).
        ('CANCELLED', 'Void'),
        ('DISPUTED', 'Disputed'),
        # Fully reversed by credit notes with nothing paid.
        ('CREDITED', 'Credited'),
    ]

    # Statuses in which the invoice is a issued document (counts as revenue
    # on the accrual basis and appears in debtors). Draft and void do not.
    ISSUED_STATUSES = ('SENT', 'VIEWED', 'PAID', 'PARTIALLY_PAID', 'OVERDUE', 'DISPUTED', 'CREDITED')

    # Same list the customer record offers (NET7..NET90). The invoice used to
    # accept only 30/60/90, so a NET45 customer was invoiced at NET30.
    PAYMENT_TERMS_CHOICES = Customer.PAYMENT_TERMS_CHOICES

    TOTALS_SOURCE_CHOICES = [
        # Totals are the sum of typed InvoiceLine rows (tax per line,
        # discount before VAT). Every invoice created after the foundation
        # release.
        ('LINES', 'Calculated from lines'),
        # Pre-foundation invoice: its stored subtotal/VAT/total are the
        # issued figures and are never recalculated (lines were backfilled
        # from the old JSON for display only).
        ('LEGACY', 'Legacy stored totals'),
    ]

    # Core fields
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="invoices")
    # Unique per company (constraint below), not globally: every company's
    # sequence starts at {prefix}00001. Drafts carry a provisional
    # DRAFT-xxxx number; the sequential number is allocated when the invoice
    # is issued, so issued numbers are gap-free (core.services.numbering).
    invoice_number = models.CharField(max_length=100, db_index=True)
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

    terms_days = models.PositiveIntegerField(
        default=30, help_text='Days from issue date to due date, from the payment terms')

    # Financial amounts. All EXCLUDING VAT unless named otherwise:
    #   subtotal      = sum of line net amounts (after discount, excl. VAT)
    #   discount      = sum of line discounts (excl. VAT) - informational
    #   vat_amount    = sum of line VAT
    #   total_amount  = subtotal + vat_amount (incl. VAT)
    #   balance       = total - paid - credited (server-derived ledger)
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
    credited_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text='Sum of issued credit notes against this invoice (incl. VAT)')
    balance = models.DecimalField(max_digits=10, decimal_places=2)

    totals_source = models.CharField(
        max_length=10, choices=TOTALS_SOURCE_CHOICES, default='LINES')

    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True, default='')

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

    # DEPRECATED read mirror of the typed InvoiceLine rows (written by
    # core.services.invoice_lines.apply_lines). Kept so older readers (PDF,
    # Xero push, mobile app) keep working; never read it to compute money.
    line_items = models.JSONField(
        default=list,
        blank=True,
        help_text='Deprecated mirror of InvoiceLine rows'
    )

    # Notes
    notes = models.TextField(blank=True)

    # Public view token — generated on first send, used for the customer-facing link
    view_token = models.CharField(max_length=64, blank=True, default='')

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
    last_reminder_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the last payment reminder was sent (collections/dunning)'
    )
    reminder_count = models.IntegerField(
        default=0,
        help_text='How many payment reminders have been sent for this invoice'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'invoices'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['company', 'invoice_number'],
                                    name='uniq_invoice_number_per_company'),
        ]
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
        """Check if invoice is overdue. A draft is never overdue: it isn't
        owed until it's sent (the nightly sweep skips drafts the same way)."""
        return date.today() > self.due_date and self.status not in ['DRAFT', 'PAID', 'CANCELLED']

    @property
    def days_until_due(self) -> int:
        """Calculate days until due (negative if overdue)."""
        return (self.due_date - date.today()).days

    # company FK is now a real field (migration 0023) — removed old property

    @property
    def has_provisional_number(self) -> bool:
        from core.services.numbering import is_provisional_number
        return is_provisional_number(self.invoice_number)

    @property
    def is_locked(self) -> bool:
        """Issued (sent or later) invoices are immutable documents: their
        financial fields change only through credit notes or void."""
        return self.status != 'DRAFT'

    @property
    def is_financed(self) -> bool:
        if not self.pk:
            return False
        from core.services.capital_guard import is_invoice_financed
        return is_invoice_financed(self)

    def calculate_total(self) -> Decimal:
        """LEGACY totals only: the pre-foundation formula (discount taken
        after VAT). LINES invoices take their totals from the lines."""
        return (self.subtotal + self.vat_amount - self.discount).quantize(Decimal('0.01'))

    def mark_as_sent(self) -> None:
        """Mark invoice as sent (issues it: allocates the sequential number)."""
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
        """Settle the outstanding balance by recording a payment for it (today,
        bank transfer), so a PAID status always has a payment behind it.
        Prefer core.services.payments.record_payment, which validates input."""
        if self.status in ('PAID', 'CANCELLED', 'DRAFT') or self.balance <= 0:
            return
        from core.models import Payment
        from core.services.ledger import recalculate_invoice
        from core.services.payments import _payment_number
        Payment.objects.create(
            company=self.company, invoice=self, customer=self.customer, amount=self.balance,
            payment_date=timezone.localdate(), payment_method='BANK_TRANSFER',
            payment_number=_payment_number(), notes='Marked as paid',
        )
        fresh = recalculate_invoice(self)
        for f in ('paid_amount', 'credited_amount', 'balance', 'status', 'paid_at', 'updated_at'):
            setattr(self, f, getattr(fresh, f))

    def save(self, *args, **kwargs) -> None:
        """Keep the derived fields consistent. No VAT is ever invented here:
        VAT comes from the lines (tax code per line) or, for LEGACY rows, is
        whatever was stored when the invoice was issued."""
        if self.subtotal is None:
            self.subtotal = Decimal('0.00')
        if self.vat_amount is None:
            self.vat_amount = Decimal('0.00')
        self.tax_amount = self.vat_amount
        if self.totals_source == 'LINES':
            self.total_amount = (self.subtotal + self.vat_amount).quantize(Decimal('0.01'))
        else:
            self.total_amount = self.calculate_total()

        from core.services.ledger import apply_ledger_status
        apply_ledger_status(self)

        # Issuing a draft allocates its gap-free sequential number. Allocation
        # and the save share one transaction, so a failed save rolls the
        # counter back instead of burning a number.
        if self.status != 'DRAFT' and self.has_provisional_number and self.company_id:
            from django.db import transaction
            from core.services.numbering import allocate_invoice_number
            with transaction.atomic():
                self.invoice_number = allocate_invoice_number(self.company)
                # A PDF rendered while it was a draft shows the provisional number.
                self.pdf_file = None
                update_fields = kwargs.get('update_fields')
                if update_fields is not None:
                    kwargs['update_fields'] = set(update_fields) | {'invoice_number', 'pdf_file'}
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)
