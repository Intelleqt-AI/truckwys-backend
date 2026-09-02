from django.db import models
from django.conf import settings
import secrets
from .customer import Customer

class Quote(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SENT', 'Sent'),
        ('ACCEPTED', 'Accepted'),
        ('DECLINED', 'Declined'),
        ('IT', 'In-Transit'),
        ('COMPLETED', 'Completed'),
        ('EXPIRED', 'Expired'),
    ]

    CONFIDENCE_CHOICES = [
        ('HIGH', 'High'),
        ('MEDIUM', 'Medium'),
        ('LOW', 'Low'),
    ]

    OUTCOME_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    ]

    TRIP_TYPE_CHOICES = [
        ('ONE_WAY', 'One Way'),
        ('ROUND_TRIP', 'Round Trip'),
    ]
    
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="quotes")
    
    quote_number = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='quotes')
    
    pickup_location = models.CharField(max_length=500)
    delivery_location = models.CharField(max_length=500)
    pickup_lat = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)
    pickup_lng = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)
    delivery_lat = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)
    delivery_lng = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)

    # Intermediate stops between pickup and delivery, in order — e.g.
    # [{"location": "Bloemfontein, FS", "lat": -29.12, "lon": 26.21}, ...].
    # Was previously UI-only in QuoteBuilder (used for live route pricing,
    # then discarded) — this is what makes them a real, saved part of the
    # quote, carried through to the converted Load and shown on every route
    # view (Quote Detail, the quotes table, the customer share link, Order
    # detail).
    stops = models.JSONField(default=list, blank=True)

    # The exact selected route's path, as [{"lat": ..., "lon": ...}, ...] —
    # same "calculated live, never saved" gap as stops had. Without this,
    # any map showing this quote/order later has to re-run its own routing
    # call, which can return a materially different road path than the one
    # actually priced and shown to the customer.
    route_geometry = models.JSONField(default=list, blank=True)

    origin = models.CharField(max_length=50, blank=True)  # e.g., "JHB", "CPT"
    destination = models.CharField(max_length=50, blank=True)  # e.g., "DUR", "PE"
    
    cargo_description = models.TextField()
    weight = models.DecimalField(max_digits=10, decimal_places=2)
    distance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vehicle_type = models.CharField(max_length=50, blank=True, default='')

    # Estimated collection & delivery dates shown to the customer on the quote
    pickup_date = models.DateField(null=True, blank=True, help_text="Estimated collection date")
    delivery_date = models.DateField(null=True, blank=True, help_text="Estimated delivery date")
    
    sla_hours = models.IntegerField(default=48, help_text="Service Level Agreement in hours")
    estimated_duration_minutes = models.IntegerField(null=True, blank=True, help_text="Travel time of the chosen TomTom route at quote creation (minutes)")

    base_rate = models.DecimalField(max_digits=10, decimal_places=2)
    fuel_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    toll_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    driver_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    additional_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    
    margin_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Profit margin %")
    
    confidence = models.CharField(max_length=20, choices=CONFIDENCE_CHOICES, default='MEDIUM')

    valid_until = models.DateField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='DRAFT')
    notes = models.TextField(blank=True)

    token = models.CharField(max_length=64, unique=True, blank=True)

    # Sprint 1: Quote Feedback Loop
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, default='pending', help_text="Quote outcome for ML training")
    rejection_reason = models.TextField(max_length=256, blank=True, null=True, help_text="Reason for rejection if applicable")
    accepted_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when quote was accepted")
    rejected_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when quote was rejected")
    fuel_price_at_creation = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True, help_text="Fuel price snapshot at quote creation time")
    win_probability = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, help_text="Predicted win probability (0-100)")

    # Round trip support
    trip_type = models.CharField(max_length=20, choices=TRIP_TYPE_CHOICES, default='ONE_WAY')
    # Return leg fields — only used when trip_type = ROUND_TRIP
    return_location = models.CharField(max_length=500, blank=True)
    return_lat = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)
    return_lng = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)
    return_cargo = models.TextField(blank=True)  # empty = truck returns empty
    return_distance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    return_date = models.DateField(null=True, blank=True)
    return_base_rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    return_notes = models.TextField(blank=True)

    vehicle = models.ForeignKey('Vehicle', on_delete=models.SET_NULL, null=True, blank=True, related_name='quotes')
    driver = models.ForeignKey('Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='quotes')

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='quotes_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'quotes'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Quote {self.quote_number} - {self.customer.name}"
