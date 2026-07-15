from django.db import models
from .quote import Quote


class QuoteOutcome(models.Model):
    """
    Stores quote outcome data for ML model training.
    Created when a quote is marked as accepted or rejected.
    """

    OUTCOME_CHOICES = [
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
    ]

    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name='outcomes')
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name='quote_outcomes')
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, help_text="Quote outcome: accepted or rejected")
    rejection_reason = models.TextField(blank=True, null=True, help_text="Reason for rejection if applicable")

    # Snapshot of quote data at outcome time
    final_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Final agreed price")
    margin_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, help_text="Realized margin percentage")
    distance_km = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Route distance in km")
    vehicle_type = models.CharField(max_length=50, blank=True, help_text="Vehicle type for the quote")
    origin = models.CharField(max_length=50, blank=True, help_text="Origin location code")
    destination = models.CharField(max_length=50, blank=True, help_text="Destination location code")
    weight_kg = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Cargo weight in kg")
    client_tier = models.CharField(max_length=20, blank=True, help_text="Client tier: new, regular, vip")
    fuel_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True, help_text="Fuel price at quote creation")

    # Point-in-time ML feature snapshots (captured when the outcome is recorded
    # so training never has to reconstruct them later — see quote_training).
    market_rate_at_outcome = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Lane market rate when the outcome was recorded")
    market_rate_source = models.CharField(max_length=20, blank=True, help_text="Provenance of market_rate_at_outcome (platform/company/estimate/...)")
    price_ratio = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True, help_text="final_price / market_rate_at_outcome")
    route_popularity = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True, help_text="Normalized 90-day lane volume at outcome time")
    days_until_departure = models.IntegerField(null=True, blank=True, help_text="Days between quote creation and pickup date")
    quote_month = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Month the quote was created (1-12)")
    quote_dow = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Weekday the quote was created (0=Mon)")
    historical_acceptance_rate = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True, help_text="Customer's acceptance rate before this outcome")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quote_outcomes'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['quote']),
            models.Index(fields=['outcome']),
            models.Index(fields=['created_at']),
        ]
        constraints = [
            # One label per quote — repeat submissions correct, never duplicate.
            models.UniqueConstraint(fields=['quote'], name='unique_outcome_per_quote'),
        ]

    def __str__(self):
        return f"QuoteOutcome {self.id} - Quote {self.quote.quote_number} - {self.outcome}"
