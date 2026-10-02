"""Suppliers (creditors) per company."""
from django.db import models


class Supplier(models.Model):
    """Who an expense was paid to. Per company (never shared across
    tenants). vat_number decides whether input VAT can be claimed on its
    receipts by default."""
    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='suppliers')
    name = models.CharField(max_length=200)
    # Lower-cased, punctuation- and suffix-free name; used to match the old
    # free-text Expense.vendor and to stop near-duplicate suppliers.
    name_key = models.CharField(max_length=200, db_index=True, blank=True, default='')
    vat_number = models.CharField(max_length=20, blank=True, default='')
    registration_number = models.CharField(max_length=20, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=30, blank=True, default='')
    category = models.CharField(max_length=50, blank=True, default='')
    is_active = models.BooleanField(default=True)
    source = models.CharField(max_length=10, default='MANUAL')
    external_id = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'suppliers'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['company', 'name_key'], name='uniq_supplier_name_per_company'),
        ]

    def save(self, *args, **kwargs):
        from core.services.identity import legal_name_key
        self.name_key = legal_name_key(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
