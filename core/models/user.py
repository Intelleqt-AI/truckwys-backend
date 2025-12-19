from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    ROLE_CHOICES = [
        ('ADMIN', 'Admin'),
        ('DISPATCHER', 'Dispatcher'),
        ('DRIVER', 'Driver'),
        ('CUSTOMER', 'Customer'),
    ]
    
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='ADMIN')
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'users'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.username} - {self.role}"
