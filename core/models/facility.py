"""Capital Facility model for managing credit facilities."""

from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal
from .company import Company


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

    # Facility limits
    limit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Total facility limit in ZAR'
    )
    outstanding = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Current outstanding advances in ZAR'
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

    def __str__(self) -> str:
        return f"Facility {self.id} - {self.company.name} (ZAR {self.limit:,.2f})"

    @property
    def available(self) -> Decimal:
        """Calculate available facility amount."""
        return self.limit - self.outstanding

    @property
    def utilization_percent(self) -> Decimal:
        """
        Calculate facility utilization percentage.

        Returns:
            Decimal: Utilization percentage (0-100)
        """
        if self.limit == 0:
            return Decimal('0.00')

        utilization = (self.outstanding / self.limit) * Decimal('100')
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
            return False, f"Insufficient available capacity (ZAR {self.available:,.2f} available)"

        return True, ""

    def reserve_amount(self, amount: Decimal) -> None:
        """
        Reserve an amount from the facility.

        Args:
            amount: Amount to reserve

        Raises:
            ValueError: If amount cannot be reserved
        """
        can_reserve, reason = self.can_advance(amount)
        if not can_reserve:
            raise ValueError(f"Cannot reserve amount: {reason}")

        self.outstanding += amount
        self.save()

    def release_amount(self, amount: Decimal) -> None:
        """
        Release a reserved amount back to the facility.

        Args:
            amount: Amount to release

        Raises:
            ValueError: If amount is invalid
        """
        if amount <= 0:
            raise ValueError("Amount must be positive")

        if amount > self.outstanding:
            raise ValueError(f"Cannot release more than outstanding (ZAR {self.outstanding:,.2f})")

        self.outstanding -= amount
        self.save()

    def clean(self) -> None:
        """Validate model fields."""
        from django.core.exceptions import ValidationError

        if self.outstanding > self.limit:
            raise ValidationError({
                'outstanding': 'Outstanding amount cannot exceed facility limit'
            })

    def save(self, *args, **kwargs) -> None:
        """Override save to run validation."""
        self.full_clean()
        super().save(*args, **kwargs)
