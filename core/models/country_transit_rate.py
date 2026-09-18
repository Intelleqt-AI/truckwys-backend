from django.db import models


class CountryTransitRate(models.Model):
    """Per-country transit cost parameters used when calculating cross-border route costs."""

    country_code = models.CharField(max_length=3, unique=True)
    country_name = models.CharField(max_length=100)
    weighbridge_fee_zar = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text='Per-trip weighbridge fee in ZAR',
    )
    toll_rate_per_km = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        help_text='Average toll cost per km in ZAR',
    )
    sa_border_distance_km = models.DecimalField(
        max_digits=7,
        decimal_places=1,
        default=0,
        help_text='Approximate km from Johannesburg to SA border post for this country',
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'country_transit_rates'
        ordering = ['country_code']

    def __str__(self) -> str:
        return f"{self.country_code} — {self.country_name}"
