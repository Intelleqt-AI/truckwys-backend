"""PaymentOutcome model for ML training and risk analysis."""

from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal
from .invoice import Invoice
from .advance_request import AdvanceRequest


class PaymentOutcome(models.Model):
    """
    Payment outcome tracking for ML training.

    Records actual payment behavior for invoices/advances to train
    the ML risk model. Captures feature snapshot at scoring time
    to enable accurate training on historical behavior.
    """

    # Relationships
    invoice = models.OneToOneField(
        Invoice,
        on_delete=models.PROTECT,
        related_name='payment_outcome',
        null=True,
        blank=True,
        help_text='Invoice this outcome is for (null for synthetic training data)'
    )
    advance = models.ForeignKey(
        AdvanceRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='payment_outcomes',
        help_text='Advance request if applicable'
    )

    # Payment timeline
    expected_payment_date = models.DateField(
        help_text='Expected payment date (usually invoice due date)'
    )
    actual_payment_date = models.DateField(
        null=True,
        blank=True,
        help_text='Actual date payment was received'
    )
    days_late = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Number of days late (0 if on-time or early)'
    )

    # Payment details
    payment_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Amount paid in ZAR'
    )

    # Outcome flags
    defaulted = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Whether payment defaulted (>90 days late or collection required)'
    )
    partial_payment = models.BooleanField(
        default=False,
        help_text='Whether payment was partial'
    )
    dispute_raised = models.BooleanField(
        default=False,
        help_text='Whether customer raised a dispute'
    )
    collection_required = models.BooleanField(
        default=False,
        help_text='Whether collection process was required'
    )

    # ML training data
    feature_snapshot = models.JSONField(
        default=dict,
        help_text='Feature values at time of scoring (for training)'
    )
    risk_score_at_time = models.IntegerField(
        null=True,
        blank=True,
        help_text='Risk score calculated at time of advance'
    )
    risk_tier_at_time = models.CharField(
        max_length=20,
        default='UNKNOWN',
        help_text='Risk tier at time of scoring'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payment_outcomes'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['defaulted']),
            models.Index(fields=['days_late']),
            models.Index(fields=['expected_payment_date']),
            models.Index(fields=['actual_payment_date']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self) -> str:
        status = "DEFAULT" if self.defaulted else f"{self.days_late}d late"
        return f"PaymentOutcome {self.invoice.invoice_number} - {status}"

    @property
    def is_high_risk(self) -> bool:
        """Check if outcome indicates high risk (>30 days late or defaulted)."""
        return self.defaulted or self.days_late > 30

    @property
    def payment_category(self) -> str:
        """Categorize payment outcome."""
        if self.defaulted:
            return 'DEFAULT'
        elif self.days_late == 0:
            return 'ON_TIME'
        elif self.days_late <= 7:
            return 'MINOR_LATE'
        elif self.days_late <= 30:
            return 'LATE'
        else:
            return 'VERY_LATE'

    @property
    def has_complete_data(self) -> bool:
        """Check if outcome has complete data for training."""
        return (
            self.actual_payment_date is not None and
            self.payment_amount > 0 and
            len(self.feature_snapshot) > 0
        )

    def calculate_days_late(self) -> None:
        """Calculate days late based on expected and actual payment dates."""
        if self.actual_payment_date and self.expected_payment_date:
            delta = (self.actual_payment_date - self.expected_payment_date).days
            self.days_late = max(0, delta)

            # Auto-mark as default if >90 days late
            if self.days_late > 90:
                self.defaulted = True

    def save(self, *args, **kwargs) -> None:
        """Override save to auto-calculate fields."""
        # Auto-calculate days_late if dates are set
        if self.actual_payment_date and self.expected_payment_date:
            self.calculate_days_late()

        # Ensure defaulted flag is set if collection required
        if self.collection_required:
            self.defaulted = True

        super().save(*args, **kwargs)
