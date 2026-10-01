"""Capital Facility model for managing credit facilities."""

from django.db import models
from django.db.models import F, Q
from django.core.validators import MinValueValidator
from decimal import Decimal
from .company import Company
from core.formatting import format_zar


class Facility(models.Model):
    """
    Capital Facility for early payment advances.

    Represents a credit facility that allows a company to request
    advances on their invoices for improved cash flow.
    """

    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('SUSPENDED', 'Suspended'),
        ('CLOSED', 'Closed'),
    ]

    # Relationships
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name='facilities',
        help_text='Company that owns this facility'
    )

    # Fast Pay (0139): a facility is now a transporter's *line* under a
    # funder's pot. Null only for facilities created outside the capital
    # flow (old tests, admin); such a line has no funder-level limits.
    funder = models.ForeignKey(
        'Funder',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='lines',
        help_text='Funder whose pot this transporter line draws on'
    )

    # Facility limits
    limit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Transporter line limit in ZAR'
    )
    outstanding = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Current outstanding advances in ZAR'
    )
    # Capacity held by advances that are requested/approved but not yet paid
    # out. Without this, any number of approvals could each pass the
    # "available" check and then all be disbursed beyond the limit. Only
    # core.services.facility_ledger writes outstanding/reserved.
    reserved = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Capacity reserved by requested/approved (undisbursed) advances in ZAR'
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='ACTIVE',
        db_index=True,
        help_text='Current facility status'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'facilities'
        ordering = ['-created_at']
        verbose_name_plural = 'Facilities'
        indexes = [
            models.Index(fields=['company', 'status']),
            models.Index(fields=['-created_at']),
        ]
        # Last line of defence under the ledger's conditional updates: even a
        # buggy caller or a raw .update() cannot over-commit a facility.
        constraints = [
            models.CheckConstraint(
                condition=Q(outstanding__gte=0), name='facility_outstanding_non_negative'),
            models.CheckConstraint(
                condition=Q(reserved__gte=0), name='facility_reserved_non_negative'),
            models.CheckConstraint(
                condition=Q(limit__gte=F('outstanding') + F('reserved')),
                name='facility_committed_within_limit'),
        ]

    def __str__(self) -> str:
        return f"Facility {self.id} - {self.company.company_name} (ZA{format_zar(self.limit)})"

    @property
    def available(self) -> Decimal:
        """Capacity not yet paid out or held by a pending advance."""
        return self.limit - self.outstanding - self.reserved

    @property
    def utilization_percent(self) -> Decimal:
        """
        Calculate facility utilization percentage.

        Returns:
            Decimal: Utilization percentage (0-100)
        """
        if self.limit == 0:
            return Decimal('0.00')

        utilization = ((self.outstanding + self.reserved) / self.limit) * Decimal('100')
        return round(utilization, 2)

    @property
    def is_active(self) -> bool:
        """Check if facility is active."""
        return self.status == 'ACTIVE'

    @property
    def has_available_capacity(self) -> bool:
        """Check if facility has available capacity."""
        return self.is_active and self.available > 0

    def can_advance(self, amount: Decimal) -> tuple[bool, str]:
        """
        Check if an advance of given amount is possible.

        Args:
            amount: Requested advance amount

        Returns:
            tuple: (is_possible, reason_if_not)
        """
        if not self.is_active:
            return False, f"Facility is {self.get_status_display()}"

        if amount <= 0:
            return False, "Amount must be positive"

        if self.available < amount:
            return False, f"Insufficient available capacity (ZA{format_zar(self.available)} available)"

        return True, ""

    def reserve_amount(self, amount: Decimal) -> None:
        """Add a disbursed amount to outstanding (locked, conditional update).

        Kept for callers outside the advance lifecycle; advances go through
        core.services.facility_ledger directly.
        """
        from core.services.facility_ledger import add_outstanding
        add_outstanding(self, amount)

    def release_amount(self, amount: Decimal) -> None:
        """Remove a repaid amount from outstanding (locked, conditional update)."""
        from core.services.facility_ledger import release_outstanding
        release_outstanding(self, amount)

    def clean(self) -> None:
        """Validate model fields."""
        from django.core.exceptions import ValidationError

        if self.outstanding + self.reserved > self.limit:
            raise ValidationError({
                'limit': 'Facility limit cannot be below outstanding plus reserved'
            })

    def save(self, *args, **kwargs) -> None:
        """Override save to run validation."""
        self.full_clean()
        super().save(*args, **kwargs)
