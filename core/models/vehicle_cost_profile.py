from django.db import models


# ──────────────────────────────────────────────────────────────────────────────
# Truck-type choices — aligned with Road Freight Association (RFA) categories
# ──────────────────────────────────────────────────────────────────────────────

TRUCK_TYPE_CHOICES: list[tuple[str, str]] = [
    ('rigid_8t', 'Rigid 8-Ton'),
    ('rigid_16t', 'Rigid 16-Ton'),
    ('horse_trailer', 'Horse & Trailer'),
    ('interlink', 'Interlink'),
    ('abnormal', 'Abnormal Load'),
]


class VehicleCostProfile(models.Model):
    """
    Stores cost-per-km (CPK) benchmark data for a given truck type.

    When *company* is ``None`` the record represents the RFA/VCI industry
    default.  A company-specific override has ``is_custom=True`` and a
    non-null *company* FK.
    """

    truck_type = models.CharField(
        max_length=20,
        choices=TRUCK_TYPE_CHOICES,
        help_text='SA truck category (RFA classification)',
    )
    fuel_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        help_text='Fuel cost per km in ZAR',
    )
    tyre_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        help_text='Tyre cost per km in ZAR',
    )
    maintenance_cpk = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        help_text='Maintenance cost per km in ZAR',
    )
    driver_cost_per_day = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text='Daily driver cost in ZAR',
    )
    is_custom = models.BooleanField(
        default=False,
        help_text='True if this is a company override (not an RFA default)',
    )
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='cost_profiles',
        help_text='Null for RFA defaults; set for company overrides',
    )
    source = models.CharField(
        max_length=100,
        help_text='Data source identifier, e.g. "RFA VCI 2024"',
    )
    effective_date = models.DateField(
        help_text='Date from which this profile is effective',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'vehicle_cost_profiles'
        ordering = ['-effective_date']
        indexes = [
            models.Index(fields=['truck_type', 'company'], name='idx_vcp_type_company'),
        ]

    def __str__(self) -> str:
        label = dict(TRUCK_TYPE_CHOICES).get(self.truck_type, self.truck_type)
        owner = self.company.company_name if self.company else 'RFA Default'
        return f"{label} — {owner} ({self.source})"
