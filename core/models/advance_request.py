"""Advance Request model for capital advance management."""

from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone
from decimal import Decimal
from .invoice import Invoice
from .facility import Facility
from .risk_score import RiskScore


class AdvanceRequest(models.Model):
    """
    Capital advance request for early invoice payment.

    Represents a request to receive advance payment on an invoice
    using a credit facility, based on risk assessment.
    """

    STATUS_CHOICES = [
        ('ELIGIBLE', 'Eligible'),
        ('REQUESTED', 'Requested'),
        ('SCORING', 'Scoring'),
        ('APPROVED', 'Approved'),
        ('DENIED', 'Denied'),
        ('DISBURSED', 'Disbursed'),
        ('SETTLED', 'Settled'),
        ('CANCELLED', 'Cancelled'),
    ]

    # Relationships
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.PROTECT,
        related_name='advance_requests',
        help_text='Invoice to advance'
    )
    facility = models.ForeignKey(
        Facility,
        on_delete=models.PROTECT,
        related_name='advance_requests',
        help_text='Facility used for this advance'
    )
    risk_score = models.ForeignKey(
        RiskScore,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='advance_requests',
        help_text='Risk assessment for this advance'
    )

    # Financial details
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text='Requested advance amount (before fees) in ZAR'
    )
    fee_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Fee amount in ZAR'
    )
    fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=3,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Fee percentage applied'
    )
    net_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Net amount disbursed (amount - fee) in ZAR'
    )

    # Status tracking
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='ELIGIBLE',
        db_index=True,
        help_text='Current status of the advance request'
    )

    # Timestamps for workflow
    requested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the advance was requested'
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the advance was approved'
    )
    disbursed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When funds were disbursed'
    )
    settled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the advance was settled (invoice paid)'
    )

    # Additional information
    denial_reason = models.TextField(
        null=True,
        blank=True,
        help_text='Reason for denial if status is DENIED'
    )
    notes = models.TextField(
        blank=True,
        help_text='Additional notes about this advance'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'advance_requests'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['invoice']),
            models.Index(fields=['facility']),
            models.Index(fields=['-created_at']),
            models.Index(fields=['disbursed_at']),
        ]

    def __str__(self) -> str:
        return f"AdvanceRequest {self.id} - {self.invoice.invoice_number} (ZAR {self.amount:,.2f})"

    @property
    def is_active(self) -> bool:
        """Check if advance is in an active state."""
        active_statuses = ['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        return self.status in active_statuses

    @property
    def is_settled(self) -> bool:
        """Check if advance has been settled."""
        return self.status == 'SETTLED'

    @property
    def days_to_settlement(self) -> int | None:
        """Calculate days from disbursement to settlement."""
        if self.disbursed_at and self.settled_at:
            delta = self.settled_at - self.disbursed_at
            return delta.days
        return None

    def calculate_fee(self, fee_percent: Decimal) -> None:
        """
        Calculate fee amount and net amount based on fee percentage.

        Args:
            fee_percent: Fee percentage to apply
        """
        self.fee_percent = fee_percent
        self.fee_amount = (self.amount * fee_percent / Decimal('100')).quantize(Decimal('0.01'))
        self.net_amount = self.amount - self.fee_amount

    def request(self) -> None:
        """Mark advance as requested."""
        if self.status != 'ELIGIBLE':
            raise ValueError(f"Cannot request advance in status {self.status}")

        self.status = 'REQUESTED'
        self.requested_at = timezone.now()
        self.save()

    def start_scoring(self) -> None:
        """Mark advance as being scored."""
        if self.status != 'REQUESTED':
            raise ValueError(f"Cannot start scoring in status {self.status}")

        self.status = 'SCORING'
        self.save()

    def approve(self) -> None:
        """Approve the advance request."""
        if self.status not in ['SCORING', 'REQUESTED']:
            raise ValueError(f"Cannot approve advance in status {self.status}")

        self.status = 'APPROVED'
        self.approved_at = timezone.now()
        self.save()

    def deny(self, reason: str) -> None:
        """
        Deny the advance request.

        Args:
            reason: Reason for denial
        """
        if self.status not in ['SCORING', 'REQUESTED']:
            raise ValueError(f"Cannot deny advance in status {self.status}")

        self.status = 'DENIED'
        self.denial_reason = reason
        self.save()

    def disburse(self) -> None:
        """Mark advance as disbursed and reserve facility amount."""
        if self.status != 'APPROVED':
            raise ValueError(f"Cannot disburse advance in status {self.status}")

        # Reserve amount in facility
        self.facility.reserve_amount(self.amount)

        self.status = 'DISBURSED'
        self.disbursed_at = timezone.now()
        self.save()

    def settle(self) -> None:
        """Mark advance as settled and release facility amount."""
        if self.status != 'DISBURSED':
            raise ValueError(f"Cannot settle advance in status {self.status}")

        # Release amount from facility
        self.facility.release_amount(self.amount)

        self.status = 'SETTLED'
        self.settled_at = timezone.now()
        self.save()

    def cancel(self) -> None:
        """Cancel the advance request."""
        if self.status in ['SETTLED', 'DISBURSED']:
            raise ValueError(f"Cannot cancel advance in status {self.status}")

        # If was disbursed, release the facility amount
        if self.status == 'DISBURSED':
            self.facility.release_amount(self.amount)

        self.status = 'CANCELLED'
        self.save()

    def clean(self) -> None:
        """Validate model fields."""
        from django.core.exceptions import ValidationError

        # Validate amount doesn't exceed invoice total
        if self.invoice and self.amount > self.invoice.total_amount:
            raise ValidationError({
                'amount': 'Advance amount cannot exceed invoice total'
            })

        # Validate facility has capacity
        if self.facility and self.status == 'APPROVED':
            can_advance, reason = self.facility.can_advance(self.amount)
            if not can_advance:
                raise ValidationError({
                    'facility': reason
                })

    def save(self, *args, **kwargs) -> None:
        """Override save to run validation."""
        self.full_clean()
        super().save(*args, **kwargs)
