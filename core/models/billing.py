from django.db import models


class BillingTransaction(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('complete', 'Complete'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    company = models.ForeignKey(
        'Company',
        on_delete=models.CASCADE,
        related_name='billing_transactions',
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_id = models.CharField(
        max_length=200,
        blank=True,
        help_text='Our m_payment_id sent to PayFast',
    )
    payfast_payment_id = models.CharField(
        max_length=200,
        blank=True,
        help_text='PayFast pf_payment_id returned in ITN',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
    )
    plan = models.CharField(
        max_length=20,
        blank=True,
        help_text='Plan key at time of transaction',
    )
    payment_status = models.CharField(
        max_length=50,
        blank=True,
        help_text='Raw payment_status from PayFast ITN',
    )
    raw_itn_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Full ITN payload for audit trail',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'billing_transactions'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.company} - {self.plan} - R{self.amount} ({self.status})"
