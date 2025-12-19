from django.db import models
from django.conf import settings

class Vehicle(models.Model):
    vin = models.CharField(max_length=100, unique=True)
    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    year = models.IntegerField()
    plate = models.CharField(max_length=50)
    type = models.CharField(max_length=50)
    capacity = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=50, default='AVAILABLE')
    fuel_type = models.CharField(max_length=50)
    mileage = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    last_maintenance_date = models.DateField(null=True, blank=True)
    next_maintenance_due = models.DateField(null=True, blank=True)
    insurance_expiry = models.DateField(null=True, blank=True)
    registration_expiry = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'vehicles'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.make} {self.model} - {self.plate}"


class VehicleLog(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='logs')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    log_type = models.CharField(max_length=50)
    description = models.TextField()
    mileage = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'vehicle_logs'
        ordering = ['-date']
    
    def __str__(self):
        return f"{self.vehicle} - {self.log_type} - {self.date}"
