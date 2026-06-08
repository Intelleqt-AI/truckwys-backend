from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    ROLE_CHOICES = [
        ('ADMIN', 'Admin'),
        ('MANAGER', 'Manager'),
        ('OPERATOR', 'Operator'),
        ('DISPATCHER', 'Dispatcher'),
        ('VIEWER', 'Viewer'),
        ('DRIVER', 'Driver'),
        ('CUSTOMER', 'Customer'),
        ('PARTNER', 'Partner'),
    ]
    
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('PENDING', 'Pending'),
    ]
    
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="users")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='ADMIN')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    job_title = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=100, default='Africa/Johannesburg')
    language = models.CharField(max_length=10, default='en')
    date_format = models.CharField(max_length=20, default='DD/MM/YYYY')
    notification_settings = models.JSONField(default=dict)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'users'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.username} - {self.role}"
