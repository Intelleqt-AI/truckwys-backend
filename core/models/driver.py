from django.db import models
from django.conf import settings

class Driver(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='driver_profile')
    license_number = models.CharField(max_length=100, unique=True)
    license_expiry = models.DateField()
    license_state = models.CharField(max_length=50)
    medical_card_expiry = models.DateField(null=True, blank=True)
    hire_date = models.DateField()
    status = models.CharField(max_length=50, default='ACTIVE')
    emergency_contact = models.CharField(max_length=200, blank=True)
    emergency_phone = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'drivers'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.get_full_name()} - {self.license_number}"
