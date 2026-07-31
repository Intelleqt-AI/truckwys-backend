from decimal import Decimal

from django.db import models


class DeliveryFeeCharge(models.Model):
    """The 0.25% take-rate charge on a delivered load's invoice.

    One row per invoice — created when the load is auto-invoiced on delivery
    and charged ad-hoc against the company's Paystack card-on-file authorization
    (core/services/paystack.py::charge_authorization). Distinct from
    BillingTransaction, which tracks the flat monthly subscription fee.
    """

    # Per TruckWys_Fee_Billing_Spec.pdf: the grace period is tracked at the
    # COMPANY level (Company.grace_period_expires_at), not per charge — a
    # failed charge just stays 'failed' and keeps being retried daily as
    # long as the company is in 'active'/'grace_period'; once the company
    # is suspended/cancelled, the retry loop skips it (nothing to gain
    # retrying without a valid card) rather than relabelling the charge.
    STATUS_CHOICES = [
        ('pending', 'Pending'),  # not yet attempted (e.g. no card on file yet)
        ('charged', 'Charged'),  # successfully charged
        ('failed', 'Failed'),    # attempted and failed; retried daily while the company isn't suspended/cancelled
    ]

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='delivery_fee_charges')
    invoice = models.OneToOneField('Invoice', on_delete=models.CASCADE, related_name='delivery_fee_charge')

    rate_pct = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.25'))
    base_amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Invoice total_amount the rate was applied to")
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="base_amount * rate_pct / 100")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    gateway_response = models.JSONField(default=dict, blank=True, help_text="Raw response from the last charge_authorization call")
    attempt_count = models.IntegerField(default=0)
    first_attempted_at = models.DateTimeField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    charged_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'delivery_fee_charges'
        ordering = ['-created_at']

    def __str__(self):
        return f"DeliveryFeeCharge {self.id} - Invoice {self.invoice_id} - R{self.amount} ({self.status})"
