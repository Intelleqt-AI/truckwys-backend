from django.db import models
from django.conf import settings

class Driver(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='driver_profile')
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="drivers")
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

    # Performance tracking fields (optional)
    # total_trips = models.IntegerField(default=0)
    # on_time_trips = models.IntegerField(default=0)
    # safety_incidents = models.IntegerField(default=0)

    # Risk engine fields
    violation_count = models.IntegerField(
        default=0,
        help_text='Number of traffic violations'
    )
    accident_history = models.IntegerField(
        default=0,
        help_text='Number of accidents in history'
    )
    experience_years = models.IntegerField(
        default=3,
        help_text='Years of driving experience'
    )
    
    class Meta:
        db_table = 'drivers'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.get_full_name()} - {self.license_number}"
