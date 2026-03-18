"""Toll plaza model for SANRAL toll road costs in South Africa."""

from django.db import models
from django.core.validators import MinValueValidator


class TollPlaza(models.Model):
    """
    Stores SANRAL toll plaza information and costs by vehicle class.

    Vehicle classes:
    - Class 2: Light motor vehicles
    - Class 3: Medium vehicles
    - Class 4: Heavy vehicles (2-axle)
    - Class 5: Heavy vehicles (3+ axle, including semi-trailers)
    """

    DIRECTION_CHOICES = [
        ('N', 'North'),
        ('S', 'South'),
        ('E', 'East'),
        ('W', 'West'),
        ('NE', 'Northeast'),
        ('NW', 'Northwest'),
        ('SE', 'Southeast'),
        ('SW', 'Southwest'),
    ]

    name = models.CharField(
        max_length=200,
        help_text="Name of the toll plaza"
    )
    route = models.CharField(
        max_length=50,
        db_index=True,
        help_text="Route identifier (e.g., N1, N2, N3, N4)"
    )
    province = models.CharField(
        max_length=100,
        help_text="South African province where the toll plaza is located"
    )
    direction = models.CharField(
        max_length=2,
        choices=DIRECTION_CHOICES,
        help_text="Primary direction of travel"
    )
    location_km = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Distance in km from route start point"
    )
    class2_cost = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Cost in ZAR for Class 2 (light motor vehicles)"
    )
    class3_cost = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Cost in ZAR for Class 3 (medium vehicles)"
    )
    class4_cost = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Cost in ZAR for Class 4 (heavy 2-axle)"
    )
    class5_cost = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Cost in ZAR for Class 5 (heavy 3+ axle, semis)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['route', 'location_km']
        verbose_name = "Toll Plaza"
        verbose_name_plural = "Toll Plazas"
        indexes = [
            models.Index(fields=['route']),
            models.Index(fields=['route', 'location_km']),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.route}) - Class 5: R{self.class5_cost}"

    def get_cost_for_class(self, vehicle_class: int) -> float:
        """
        Get toll cost for a specific vehicle class.

        Args:
            vehicle_class: Vehicle class (2, 3, 4, or 5)

        Returns:
            float: Toll cost in ZAR

        Raises:
            ValueError: If vehicle_class is not 2, 3, 4, or 5
        """
        cost_map = {
            2: self.class2_cost,
            3: self.class3_cost,
            4: self.class4_cost,
            5: self.class5_cost,
        }

        if vehicle_class not in cost_map:
            raise ValueError(f"Invalid vehicle class: {vehicle_class}. Must be 2, 3, 4, or 5.")

        return float(cost_map[vehicle_class])
