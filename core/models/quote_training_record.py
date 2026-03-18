"""Quote training record model for ML training data (internal use only, not exposed via API)."""

from django.db import models


class QuoteTrainingRecord(models.Model):
    """
    Internal model for storing synthetic quote training data for ML models.

    NOT exposed via API - used only for training ML models.
    Contains all 22 ML features plus acceptance status and margin data.
    """

    origin = models.CharField(max_length=100)
    destination = models.CharField(max_length=100)
    distance_km = models.FloatField()
    truck_type = models.CharField(max_length=50)
    load_type = models.CharField(max_length=50)
    quote_price = models.DecimalField(max_digits=10, decimal_places=2)
    diesel_price = models.DecimalField(max_digits=6, decimal_places=2)
    toll_cost = models.DecimalField(max_digits=10, decimal_places=2)
    fuel_cost = models.DecimalField(max_digits=10, decimal_places=2)
    driver_cost = models.DecimalField(max_digits=10, decimal_places=2)
    maintenance_cost = models.DecimalField(max_digits=10, decimal_places=2)
    tyre_cost = models.DecimalField(max_digits=10, decimal_places=2)
    deadhead_cost = models.DecimalField(max_digits=10, decimal_places=2)
    has_return_load = models.BooleanField(default=False)
    client_segment = models.CharField(max_length=50)
    quoted_margin_pct = models.FloatField()
    actual_margin_pct = models.FloatField()
    cost_per_km = models.DecimalField(max_digits=8, decimal_places=2)
    revenue_per_km = models.DecimalField(max_digits=8, decimal_places=2)
    route_category = models.CharField(max_length=50)
    season = models.CharField(max_length=20)
    day_of_week = models.CharField(max_length=20)
    month = models.IntegerField()

    accepted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Quote Training Record"
        verbose_name_plural = "Quote Training Records"
        indexes = [
            models.Index(fields=['accepted']),
            models.Index(fields=['client_segment']),
            models.Index(fields=['truck_type']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self) -> str:
        status = "Accepted" if self.accepted else "Rejected"
        return f"{self.origin} → {self.destination} - {status} - Margin: {self.quoted_margin_pct:.1f}%"
