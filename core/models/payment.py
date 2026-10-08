from django.db import models
from .invoice import Invoice
from .customer import Customer


class Payment(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('CASH', 'Cash'),
        ('CHEQUE', 'Cheque'),
        ('EARLY_PAY', 'Early Payment Advance'),
        ('EFT', 'Electronic Funds Transfer'),
        ('CREDIT_CARD', 'Credit Card'),
        ('ACH', 'ACH'),
    ]

    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="payments")

    payment_number = models.CharField(max_length=100, unique=True, db_index=True)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='payments')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='payments')

    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateField(db_index=True)
    payment_method = models.CharField(max_length=50, choices=PAYMENT_METHOD_CHOICES, db_index=True)

    SOURCE_CHOICES = [
        ('MANUAL', 'Entered in TruckWys'),
        ('XERO', 'Xero'),
        ('QBO', 'QuickBooks Online'),
        ('BANK', 'Bank feed'),
    ]
    # Where the payment came from, and its id there. (company, source,
    # external_id) is unique, so a re-run accounting/bank sync can't record
    # the same payment twice.
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='MANUAL', db_index=True)
    external_id = models.CharField(max_length=100, blank=True, default='')

    reference_number = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payments'
        ordering = ['-payment_date']
        constraints = [
            models.UniqueConstraint(fields=['company', 'source', 'external_id'],
                                    condition=~models.Q(external_id=''),
                                    name='uniq_payment_external_id'),
        ]
        indexes = [
            models.Index(fields=['invoice']),
            models.Index(fields=['payment_method']),
            models.Index(fields=['-payment_date']),
        ]

    def __str__(self):
        return f"Payment {self.payment_number} - {self.amount}"

    @property
    def is_early_payment(self) -> bool:
        """Check if this is an early payment advance."""
        return self.payment_method == 'EARLY_PAY'
