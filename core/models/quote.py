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
    ]
    
    CONFIDENCE_CHOICES = [
        ('HIGH', 'High'),
        ('MEDIUM', 'Medium'),
        ('LOW', 'Low'),
    ]
    
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="quotes")
    
    quote_number = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='quotes')
    
    pickup_location = models.CharField(max_length=500)
    delivery_location = models.CharField(max_length=500)
    
    origin = models.CharField(max_length=50, blank=True)  # e.g., "JHB", "CPT"
    destination = models.CharField(max_length=50, blank=True)  # e.g., "DUR", "PE"
    
    cargo_description = models.TextField()
    weight = models.DecimalField(max_digits=10, decimal_places=2)
    distance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vehicle_type = models.CharField(max_length=50, blank=True, default='')
    
    sla_hours = models.IntegerField(default=48, help_text="Service Level Agreement in hours")
    
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
