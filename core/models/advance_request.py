"""Advance Request model for capital advance management."""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.core.validators import MinValueValidator
from django.utils import timezone
from decimal import Decimal
from .invoice import Invoice
from .facility import Facility
from .risk_score import RiskScore
from core.formatting import format_zar


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

    # Facility capacity this advance currently holds in Facility.reserved.
    # Tracked per advance so release/disburse move exactly what was reserved
    # (legacy rows created before reservation existed hold 0 and reserve on
    # approve/disburse instead). Written only by core.services.facility_ledger.
    capacity_reserved = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Amount of facility capacity held by this advance'
    )

    # Settlement evidence: an advance is only closed by the capital desk or a
    # system path against a real debtor payment, never by the transporter.
    settlement_reference = models.CharField(
        max_length=200,
        blank=True,
        help_text='Bank/payment reference proving the debtor paid'
    )
    settlement_payment = models.ForeignKey(
        'core.Payment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='settled_advances',
        help_text='Recorded payment on the advanced invoice that settled it'
    )
    settled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='settled_advances',
        help_text='Staff user who settled the advance (null = system)'
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
        # One live advance per invoice. The old view-level "race dupe" check
        # locked rows that did not exist yet, so two concurrent requests could
        # both insert; only the database can make this hold.
        constraints = [
            models.UniqueConstraint(
                fields=['invoice'],
                condition=Q(status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']),
                name='uniq_active_advance_per_invoice',
            ),
        ]

    def __str__(self) -> str:
        return f"AdvanceRequest {self.id} - {self.invoice.invoice_number} (ZA{format_zar(self.amount)})"

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

    # Every state change that moves facility capacity goes through
    # core.services.facility_ledger, which locks the facility and advance rows
    # and uses conditional F() updates. The methods below are thin wrappers so
    # existing callers (staff + partner views) keep their API.

    def request(self) -> None:
        """Mark advance as requested and reserve facility capacity."""
        from core.services import facility_ledger
        facility_ledger.request_advance(self)

    def start_scoring(self) -> None:
        """Mark advance as being scored."""
        if self.status != 'REQUESTED':
            raise ValueError(f"Cannot start scoring in status {self.status}")

        self.status = 'SCORING'
        self.save()

    def approve(self) -> None:
        """Approve the advance request (its reservation is kept)."""
        from core.services import facility_ledger
        facility_ledger.approve_advance(self)

    def deny(self, reason: str) -> None:
        """Deny the advance request and release its reservation."""
        from core.services import facility_ledger
        facility_ledger.deny_advance(self, reason)

    def disburse(self) -> None:
        """Pay out: move the reservation into facility outstanding."""
        from core.services import facility_ledger
        facility_ledger.disburse_advance(self)

    def settle(self, payment_reference: str, settled_by=None, payment=None) -> None:
        """Close a disbursed advance against debtor-payment evidence."""
        from core.services import facility_ledger
        facility_ledger.settle_advance(
            self, payment_reference=payment_reference, settled_by=settled_by, payment=payment)

    def cancel(self, note: str = '') -> None:
        """Cancel an undisbursed advance and release its reservation."""
        from core.services import facility_ledger
        facility_ledger.cancel_advance(self, note=note)

    def clean(self) -> None:
        """Validate model fields."""
        from django.core.exceptions import ValidationError

        # Validate amount doesn't exceed invoice total
        if self.invoice and self.amount > self.invoice.total_amount:
            raise ValidationError({
                'amount': 'Advance amount cannot exceed invoice total'
            })

        # Facility capacity is not checked here any more: an APPROVED advance
        # already holds its own reservation, so a facility.available check
        # would count it twice. facility_ledger + the Facility CheckConstraints
        # enforce capacity.

    def save(self, *args, **kwargs) -> None:
        """Override save to run validation."""
        self.full_clean()
        super().save(*args, **kwargs)
