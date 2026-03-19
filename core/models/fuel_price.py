from django.db import models


class FuelPrice(models.Model):
    """Monthly South African fuel prices sourced from SAPIA/FIASA/DOE."""

    date = models.DateField(unique=True, help_text='First day of the price month')
    diesel_inland = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Diesel 50ppm inland retail price (ZAR/litre)',
    )
    diesel_coastal = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Diesel 50ppm coastal retail price (ZAR/litre)',
    )
    petrol_95 = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Petrol 95 ULP inland retail price (ZAR/litre)',
    )
    petrol_93 = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Petrol 93 ULP inland retail price (ZAR/litre)',
    )
    source = models.CharField(
        max_length=100,
        default='SAPIA',
        help_text='Data source identifier',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fuel_prices'
        ordering = ['-date']

    def __str__(self):
        return (
            f"FuelPrice {self.date:%Y-%m} | "
            f"Diesel inland R{self.diesel_inland} coastal R{self.diesel_coastal}"
        )
