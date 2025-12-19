from django.db import models
from django.conf import settings
from .vehicle import Vehicle
from .driver import Driver

class Expense(models.Model):
    CATEGORY_CHOICES = [
        ('FUEL', 'Fuel'),
        ('MAINTENANCE', 'Maintenance'),
        ('INSURANCE', 'Insurance'),
        ('TOLLS', 'Tolls'),
        ('PERMITS', 'Permits'),
        ('SALARY', 'Salary'),
        ('OTHER', 'Other'),
    ]
    
    expense_number = models.CharField(max_length=100, unique=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    description = models.TextField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')
    
    expense_date = models.DateField()
    vendor = models.CharField(max_length=200, blank=True)
    receipt_number = models.CharField(max_length=100, blank=True)
    
    notes = models.TextField(blank=True)
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='expenses_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'expenses'
        ordering = ['-expense_date']
    
    def __str__(self):
        return f"{self.category} - {self.amount} - {self.expense_date}"
