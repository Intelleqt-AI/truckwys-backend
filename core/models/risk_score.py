"""Risk Score model for invoice early payment risk assessment."""

from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from .invoice import Invoice
from .customer import Customer
from .company import Company


class RiskScore(models.Model):
    """
    Risk assessment score for invoice early payment eligibility.

    Calculates a 0-100 risk score using 6 factors to determine:
    - Eligibility for early payment
    - Risk tier classification
    - Fee percentage and amount
    """

    TIER_CHOICES = [
        # New 7-pillar tier names
        ('PRIME', 'Prime (85-100)'),
        ('STANDARD', 'Standard (70-84)'),
        ('ELEVATED', 'Elevated (55-69)'),
        ('HIGH', 'High (40-54)'),
        ('INELIGIBLE', 'Ineligible (<40)'),
        # Old tiers for backward compatibility
        ('EXCELLENT', 'Excellent (85-100) - DEPRECATED'),
        ('GOOD', 'Good (70-84) - DEPRECATED'),
        ('FAIR', 'Fair (55-69) - DEPRECATED'),
    ]

    # Relationships
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='risk_scores',
        help_text='Invoice being assessed'
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name='risk_scores',
        help_text='Customer being assessed'
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name='risk_scores',
        help_text='Company requesting the score'
    )

    # Overall score
    total_score = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Total risk score (0-100)'
    )
    tier = models.CharField(
        max_length=20,
        choices=TIER_CHOICES,
        db_index=True,
        help_text='Risk tier based on total score'
    )

    # Fee calculation
    fee_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00')), MaxValueValidator(Decimal('10.00'))],
        help_text='Fee percentage (0-10%)'
    )
    fee_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text='Calculated fee amount in ZAR'
    )

    # Factor scores (6 factors - DEPRECATED, kept for backward compatibility)
    factor_payment_history = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(35)],
        help_text='DEPRECATED: Payment history score (0-35 points, 35% weight)'
    )
    factor_invoice_age = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(20)],
        help_text='DEPRECATED: Invoice age score (0-20 points, 20% weight)'
    )
    factor_pod_quality = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(15)],
        help_text='DEPRECATED: POD quality score (0-15 points, 15% weight)'
    )
    factor_credit_score = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(15)],
        help_text='DEPRECATED: Credit score factor (0-15 points, 15% weight)'
    )
    factor_relationship_length = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text='DEPRECATED: Relationship length score (0-10 points, 10% weight)'
    )
    factor_facility_ratio = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
        help_text='DEPRECATED: Facility ratio score (0-5 points, 5% weight)'
    )

    # NEW: 7-Pillar Institutional Risk Engine factors
    factor_client_identity = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Pillar 1: Client Identity & Profile raw score (0-100, 15% weight)'
    )
    factor_client_financial = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Pillar 2: Client Financial Health raw score (0-100, 20% weight)'
    )
    factor_debtor_credit = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Pillar 3: Debtor Creditworthiness raw score (0-100, 20% weight)'
    )
    factor_invoice_chars = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Pillar 4: Invoice Characteristics raw score (0-100, 15% weight)'
    )
    factor_pod_docs = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Pillar 5: POD & Documentation raw score (0-100, 10% weight)'
    )
    factor_operational = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Pillar 6: Operational & Trip Factors raw score (0-100, 10% weight)'
    )
    factor_macro_market = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Pillar 7: Macro & Market Factors raw score (0-100, 10% weight)'
    )

    # Detailed breakdown
    factors_breakdown = models.JSONField(
        default=dict,
        help_text='Detailed explanation for each factor score'
    )

    # Eligibility
    is_eligible = models.BooleanField(
        default=True,
        db_index=True,
        help_text='Whether invoice is eligible for early payment'
    )
    ineligibility_reason = models.TextField(
        null=True,
        blank=True,
        help_text='Reason for ineligibility if not eligible'
    )

    # Validity period
    calculated_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When this score was calculated'
    )
    expires_at = models.DateTimeField(
        help_text='When this score expires'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'risk_scores'
        ordering = ['-calculated_at']
        indexes = [
            models.Index(fields=['invoice', '-calculated_at']),
            models.Index(fields=['customer', '-calculated_at']),
            models.Index(fields=['tier']),
            models.Index(fields=['is_eligible']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self) -> str:
        return f"RiskScore {self.id} - {self.invoice.invoice_number} ({self.tier}, {self.total_score})"

    @property
    def is_expired(self) -> bool:
        """Check if risk score has expired."""
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if risk score is still valid."""
        return not self.is_expired

    @property
    def days_until_expiry(self) -> int:
        """Calculate days until score expires."""
        if self.is_expired:
            return 0
        delta = self.expires_at - timezone.now()
        return max(0, delta.days)

    @classmethod
    def get_tier_from_score(cls, score: int) -> str:
        """
        Determine risk tier from total score.

        Args:
            score: Total risk score (0-100)

        Returns:
            str: Risk tier
        """
        if score >= 85:
            return 'PRIME'
        elif score >= 70:
            return 'STANDARD'
        elif score >= 55:
            return 'ELEVATED'
        elif score >= 40:
            return 'HIGH'
        else:
            return 'INELIGIBLE'

    @classmethod
    def get_base_fee_for_tier(cls, tier: str) -> tuple[Decimal, Decimal]:
        """
        Get base fee range for a given tier.

        Args:
            tier: Risk tier

        Returns:
            tuple: (min_fee_percent, max_fee_percent)
        """
        fee_ranges = {
            # Canonical 7-pillar tiers
            'PRIME': (Decimal('2.0'), Decimal('2.5')),
            'STANDARD': (Decimal('2.5'), Decimal('3.0')),
            'ELEVATED': (Decimal('3.5'), Decimal('4.0')),
            'HIGH': (Decimal('4.0'), Decimal('4.5')),
            'INELIGIBLE': (Decimal('0.0'), Decimal('0.0')),
            # Deprecated aliases kept for backward compatibility
            'EXCELLENT': (Decimal('2.0'), Decimal('2.5')),
            'GOOD': (Decimal('2.5'), Decimal('3.0')),
            'FAIR': (Decimal('3.0'), Decimal('3.5')),
        }
        return fee_ranges.get(tier, (Decimal('0.0'), Decimal('0.0')))

    def calculate_total_score(self) -> int:
        """
        Calculate total score from all factors.

        Returns:
            int: Total score (0-100)
        """
        total = (
            self.factor_payment_history +
            self.factor_invoice_age +
            self.factor_pod_quality +
            self.factor_credit_score +
            self.factor_relationship_length +
            self.factor_facility_ratio
        )
        return min(100, max(0, total))

    def set_expiry(self, days: int = 7) -> None:
        """
        Set expiry date for this risk score.

        Args:
            days: Number of days until expiry (default: 7)
        """
        self.expires_at = timezone.now() + timedelta(days=days)

    def save(self, *args, **kwargs) -> None:
        """Override save to set expiry if not set."""
        if not self.expires_at:
            self.set_expiry()

        super().save(*args, **kwargs)
