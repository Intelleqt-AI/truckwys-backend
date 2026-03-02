from django.db import models
from .driver import Driver
from .load import Load

class Settlement(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('PAID', 'Paid'),
    ]
    
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="%(class)ss")
    
    settlement_number = models.CharField(max_length=100, unique=True)
    driver = models.ForeignKey(Driver, on_delete=models.PROTECT, related_name='settlements')
    
    start_date = models.DateField()
    end_date = models.DateField()
    
    total_miles = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    driver_pay = models.DecimalField(max_digits=10, decimal_places=2)
    deductions = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_pay = models.DecimalField(max_digits=10, decimal_places=2)
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    payment_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'settlements'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Settlement {self.settlement_number} - {self.driver}"
