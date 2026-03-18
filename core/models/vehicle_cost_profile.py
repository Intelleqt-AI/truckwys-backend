"""Vehicle cost profile model for RFA benchmarks and company-specific overrides."""

from django.db import models
from django.core.validators import MinValueValidator


class VehicleCostProfile(models.Model):
    """
    Stores vehicle operating cost profiles based on RFA (Road Freight Association) benchmarks.

    Companies can override RFA baselines with their own cost profiles.
    All cost-per-km values are in ZAR.
    """

    TRUCK_TYPE_CHOICES = [
        ('semi_34t', 'Semi-trailer 34 ton'),
        ('rigid_8t', 'Rigid 8 ton'),
        ('flatbed', 'Flatbed truck'),
        ('tipper', 'Tipper truck'),
        ('reefer', 'Refrigerated truck'),
        ('tanker', 'Tanker truck'),
        ('interlink', 'Interlink'),
    ]

    truck_type = models.CharField(
        max_length=50,
        choices=TRUCK_TYPE_CHOICES,
        db_index=True,
        help_text="Type of truck for this cost profile"
    )
    fuel_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Fuel cost per kilometer in ZAR"
    )
    tyre_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Tyre wear cost per kilometer in ZAR"
    )
    maintenance_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Maintenance cost per kilometer in ZAR"
    )
    driver_cost_per_day = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Driver cost per day in ZAR (includes salary, accommodation, meals)"
    )
    is_custom = models.BooleanField(
        default=True,
        db_index=True,
        help_text="False if this is an RFA baseline, True if company override"
    )
    company = models.ForeignKey(
        'Company',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='vehicle_cost_profiles',
        help_text="Company that owns this profile (null for RFA baselines)"
    )
    source = models.CharField(
        max_length=100,
        default='Manual Entry',
        help_text="Source of this cost profile data (e.g., 'RFA VCI 2024', 'Company Override')"
    )
    effective_date = models.DateField(
        help_text="Date from which this profile is effective"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Legacy field for backwards compatibility
    @property
    def is_rfa_baseline(self):
        """Backwards compatibility: is_rfa_baseline = not is_custom"""
        return not self.is_custom

    class Meta:
        ordering = ['truck_type', '-is_rfa_baseline']
        verbose_name = "Vehicle Cost Profile"
        verbose_name_plural = "Vehicle Cost Profiles"
        indexes = [
            models.Index(fields=['truck_type', 'is_rfa_baseline']),
            models.Index(fields=['truck_type', 'company']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['truck_type', 'company'],
                name='unique_truck_type_per_company'
            ),
            models.UniqueConstraint(
                fields=['truck_type'],
                condition=models.Q(is_rfa_baseline=True, company__isnull=True),
                name='unique_rfa_baseline_per_truck_type'
            ),
        ]

    def __str__(self) -> str:
        if self.is_rfa_baseline:
            return f"RFA Baseline: {self.get_truck_type_display()} - Fuel CPK: R{self.fuel_cpk}"
        elif self.company:
            return f"{self.company.company_name}: {self.get_truck_type_display()} - Fuel CPK: R{self.fuel_cpk}"
        else:
            return f"{self.get_truck_type_display()} - Fuel CPK: R{self.fuel_cpk}"

    def total_cpk(self) -> float:
        """
        Calculate total cost per kilometer (excluding driver daily cost).

        Returns:
            float: Total cost per km in ZAR
        """
        return float(self.fuel_cpk + self.tyre_cpk + self.maintenance_cpk)


# Module-level export for test compatibility
TRUCK_TYPE_CHOICES = VehicleCostProfile.TRUCK_TYPE_CHOICES
