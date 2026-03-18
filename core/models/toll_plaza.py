"""Toll plaza model for SANRAL toll road costs in South Africa."""

from django.db import models
from django.core.validators import MinValueValidator


class TollPlaza(models.Model):
    """
    Stores SANRAL toll plaza information and costs by vehicle class.
    DB schema defined by migration 0034_tollplaza.

    Vehicle classes:
    - Class 2: Light motor vehicles
    - Class 3: Medium vehicles
    - Class 4: Heavy vehicles (2-axle)
    - Class 5: Heavy vehicles (3+ axle, including semi-trailers)
    """

    name = models.CharField(max_length=100, help_text="Name of the toll plaza")
    route = models.CharField(max_length=10, db_index=True, help_text="Route (e.g. N1, N3)")
    direction = models.CharField(max_length=100, help_text="Direction of travel on route")
    location_km = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    tariff_class_2 = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    tariff_class_3 = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    tariff_class_4 = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    tariff_class_5 = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    tariff_year = models.PositiveSmallIntegerField(default=2026)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'toll_plazas'
        unique_together = [('name', 'route')]
        ordering = ['route', 'location_km']
        verbose_name = "Toll Plaza"
        verbose_name_plural = "Toll Plazas"
        indexes = [
            models.Index(fields=['route']),
            models.Index(fields=['route', 'location_km']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.route}) - Class 5: R{self.tariff_class_5}"

    # Backwards-compat aliases
    @property
    def class2_cost(self):
        return self.tariff_class_2

    @property
    def class3_cost(self):
        return self.tariff_class_3

    @property
    def class4_cost(self):
        return self.tariff_class_4

    @property
    def class5_cost(self):
        return self.tariff_class_5

    def get_cost_for_class(self, vehicle_class: int) -> float:
        cost_map = {2: self.tariff_class_2, 3: self.tariff_class_3,
                    4: self.tariff_class_4, 5: self.tariff_class_5}
        if vehicle_class not in cost_map:
            raise ValueError(f"Invalid vehicle_class: {vehicle_class}. Must be 2-5.")
        return float(cost_map[vehicle_class])

    def get_tariff(self, vehicle_class: int):
        """Get tariff as Decimal for a vehicle class."""
        cost_map = {2: self.tariff_class_2, 3: self.tariff_class_3,
                    4: self.tariff_class_4, 5: self.tariff_class_5}
        if vehicle_class not in cost_map:
            raise ValueError(f"Invalid vehicle_class: {vehicle_class}. Must be 2-5.")
        return cost_map[vehicle_class]
