from django.db import models
from django.conf import settings
from .customer import Customer
from .vehicle import Vehicle
from .driver import Driver

class Load(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ASSIGNED', 'Assigned'),
        ('LOADING', 'Loading'),
        ('IN_TRANSIT', 'In Transit'),
        ('DELIVERED', 'Delivered'),
        ('INVOICED', 'Invoiced'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="loads")
    
    load_number = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='loads')
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='loads')
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='loads')
    quote = models.ForeignKey('core.Quote', on_delete=models.SET_NULL, null=True, blank=True, related_name='loads')
    
    pickup_location = models.CharField(max_length=500)
    pickup_city = models.CharField(max_length=100)
    pickup_state = models.CharField(max_length=50)
    pickup_zip = models.CharField(max_length=20)
    pickup_date = models.DateTimeField()
    
    delivery_location = models.CharField(max_length=500)
    delivery_city = models.CharField(max_length=100)
    delivery_state = models.CharField(max_length=50)
    delivery_zip = models.CharField(max_length=20)
    delivery_date = models.DateTimeField()
    
    cargo_description = models.TextField()
    weight = models.DecimalField(max_digits=10, decimal_places=2)
    distance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    fuel_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    additional_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    notes = models.TextField(blank=True)
    
    pod_signature = models.TextField(blank=True)  # Proof of delivery signature
    pod_received_by = models.CharField(max_length=200, blank=True)
    pod_document = models.FileField(upload_to='pod/', blank=True, null=True)
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='loads_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'loads'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['load_number']),
            models.Index(fields=['status']),
            models.Index(fields=['pickup_date']),
        ]
    
    def __str__(self):
        return f"Load {self.load_number} - {self.status}"
