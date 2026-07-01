from decimal import Decimal

from django.db import models


class TollPlaza(models.Model):
    """
    SANRAL toll plaza with per-vehicle-class tariffs (ZAR).

    Gauteng Urban Network (GFIP / e-toll) is EXCLUDED — scrapped April 2024.

    Vehicle classes follow SANRAL classification:
        Class 2 — light motor vehicles (passenger, LDV, minibus ≤3.5 t GVM)
        Class 3 — medium motor vehicles (2-axle truck/bus, 3.5–11 t GVM)
        Class 4 — heavy motor vehicles (3+ axle single unit, >11 t GVM)
        Class 5 — multi-unit combinations (truck + trailer / semi-truck)
    """

    ROUTE_CHOICES = [
        ('N1',  'N1 — Cape Town to Johannesburg / Polokwane'),
        ('N2',  'N2 — Cape Town to Durban (coastal)'),
        ('N3',  'N3 — Johannesburg to Durban'),
        ('N4',  'N4 — Pretoria to Maputo'),
        ('N14', 'N14 — Johannesburg to Springbok'),
        ('N17', 'N17 — Johannesburg to Ermelo / Swaziland'),
        ('R30', 'R30/R730/R34 — Bloemfontein region'),
    ]

    name = models.CharField(max_length=100, help_text='Official SANRAL plaza name')
    route = models.CharField(max_length=10, choices=ROUTE_CHOICES, db_index=True)
    direction = models.CharField(
        max_length=100,
        help_text='Route description e.g. "Cape Town → Johannesburg"',
    )
    location_km = models.DecimalField(
        max_digits=7, decimal_places=1,
        help_text='Distance from route origin (km)',
    )
    tariff_class_2 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Light motor vehicle tariff (ZAR)',
    )
    tariff_class_3 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Medium motor vehicle tariff (ZAR)',
    )
    tariff_class_4 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Heavy motor vehicle tariff (ZAR)',
    )
    tariff_class_5 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Multi-unit combination tariff (ZAR)',
    )
    tariff_year = models.PositiveSmallIntegerField(
        default=2024,
        help_text='Tariff revision year (SANRAL announces increases annually)',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'toll_plazas'
        ordering = ['route', 'location_km']
        unique_together = [('name', 'route')]

    def __str__(self):
        return f"{self.name} ({self.route}) km {self.location_km}"

    def get_tariff(self, vehicle_class: int) -> Decimal:
        """Return tariff for SANRAL vehicle class 2–5."""
        mapping = {
            2: self.tariff_class_2,
            3: self.tariff_class_3,
            4: self.tariff_class_4,
            5: self.tariff_class_5,
        }
        if vehicle_class not in mapping:
            raise ValueError(f"Vehicle class must be 2–5, got {vehicle_class}")
        return mapping[vehicle_class]
