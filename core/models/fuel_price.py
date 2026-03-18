"""Fuel price model for tracking diesel and petrol prices in South Africa."""

from django.db import models
from django.core.validators import MinValueValidator


class FuelPrice(models.Model):
    """
    Stores fuel price records for South Africa.

    Tracks diesel (inland and coastal) and petrol prices, typically updated monthly
    based on official FIASA announcements.
    """

    date = models.DateField(
        db_index=True,
        help_text="Effective date of these fuel prices"
    )
    diesel_inland = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Diesel price per liter in ZAR for inland regions"
    )
    diesel_coastal = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Diesel price per liter in ZAR for coastal regions"
    )
    petrol_95 = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Petrol 95 Octane price per liter in ZAR"
    )
    petrol_93 = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="Petrol 93 Octane price per liter in ZAR"
    )
    source = models.CharField(
        max_length=100,
        default="FIASA",
        help_text="Source of the price data (e.g., FIASA, manual entry, scraped)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fuel_prices'
        ordering = ['-date']
        verbose_name = "Fuel Price"
        verbose_name_plural = "Fuel Prices"
        indexes = [
            models.Index(fields=['-date']),
        ]

    def __str__(self) -> str:
        return f"Fuel Prices {self.date} - Diesel Inland: R{self.diesel_inland}"
