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
        ('rigid_8t', 'Rigid 8-Ton'),
        ('rigid_16t', 'Rigid 16-Ton'),
        ('horse_trailer', 'Horse & Trailer'),
        ('interlink', 'Interlink'),
        ('abnormal', 'Abnormal Load'),
    ]

    truck_type = models.CharField(
        max_length=20,
        choices=TRUCK_TYPE_CHOICES,
        help_text='SA truck category (RFA classification)'
    )
    fuel_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        validators=[MinValueValidator(0)],
        help_text="Fuel cost per kilometer in ZAR"
    )
    tyre_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        validators=[MinValueValidator(0)],
        help_text="Tyre wear cost per kilometer in ZAR"
    )
    maintenance_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=4,
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
        default=False,
        help_text='True if this is a company override (not an RFA default)'
    )
    company = models.ForeignKey(
        'Company',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='cost_profiles',
        help_text='Null for RFA defaults; set for company overrides'
    )
    source = models.CharField(
        max_length=100,
        help_text='Data source identifier, e.g. "RFA VCI 2024"'
    )
    effective_date = models.DateField(
        help_text='Date from which this profile is effective'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'vehicle_cost_profiles'
        ordering = ['-effective_date']
        verbose_name = 'Vehicle Cost Profile'
        verbose_name_plural = 'Vehicle Cost Profiles'
        indexes = [
            models.Index(fields=['truck_type', 'company'], name='idx_vcp_type_company'),
        ]

    def __str__(self) -> str:
        truck_display = self.get_truck_type_display()
        if not self.is_custom and self.company is None:
            return f"{truck_display} - RFA Default ({self.source})"
        elif self.company:
            return f"{self.company.company_name}: {truck_display} - {self.source}"
        else:
            return f"{truck_display} - Fuel CPK: R{self.fuel_cpk}"

    def total_cpk(self) -> float:
        """
        Calculate total cost per kilometer (excluding driver daily cost).

        Returns:
            float: Total cost per km in ZAR
        """
        return float(self.fuel_cpk + self.tyre_cpk + self.maintenance_cpk)


# Module-level export for test compatibility
TRUCK_TYPE_CHOICES = VehicleCostProfile.TRUCK_TYPE_CHOICES
