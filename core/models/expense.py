from django.db import models
from django.conf import settings
from django.utils import timezone
from .vehicle import Vehicle
from .driver import Driver


class Expense(models.Model):
    CATEGORY_CHOICES = [
        ('FUEL', 'Fuel'),
        ('TOLLS', 'Tolls'),
        ('MAINTENANCE', 'Maintenance'),
        ('DRIVER', 'Driver Expenses'),
        ('INSURANCE', 'Insurance'),
        ('OVERHEAD', 'Overhead'),
        ('OTHER', 'Other'),
    ]

    expense_number = models.CharField(max_length=100, unique=True, db_index=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, db_index=True)
    description = models.TextField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')

    # NEW: Link to Trip
    trip = models.ForeignKey(
        'Trip',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses',
        help_text='Trip this expense is associated with'
    )

    expense_date = models.DateField(db_index=True)
    vendor = models.CharField(max_length=200, blank=True)
    receipt_number = models.CharField(max_length=100, blank=True)

    # NEW: Receipt file upload
    receipt_file = models.FileField(
        upload_to='receipts/%Y/%m/',
        null=True,
        blank=True,
        help_text='Scanned receipt or invoice'
    )

    # NEW: Approval workflow
    approved = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Whether this expense has been approved'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses_approved',
        help_text='User who approved this expense'
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When this expense was approved'
    )

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='expenses_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'expenses'
        ordering = ['-expense_date']
        indexes = [
            models.Index(fields=['category']),
            models.Index(fields=['approved']),
            models.Index(fields=['-expense_date']),
        ]

    def __str__(self):
        return f"{self.category} - {self.amount} - {self.expense_date}"

    def approve(self, user):
        """Approve this expense."""
        if not self.approved:
            self.approved = True
            self.approved_by = user
            self.approved_at = timezone.now()
            self.save()
