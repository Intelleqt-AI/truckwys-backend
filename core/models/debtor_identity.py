"""Global (cross-tenant) debtor identity, keyed on CIPC / VAT number."""
from django.db import models


class DebtorIdentity(models.Model):
    """The legal entity behind tenant Customer rows.

    NOT tenant data and never serialised to a tenant: a tenant only ever sees
    its own Customer. It exists so Capital (Fast Pay) can recognise the same
    debtor across transporters for concentration limits and network payment
    behaviour (docs/capital-risk/03-design.md §2.1), under the funder's
    POPIA basis. Holds identifiers only, no tenant-specific data.

    Fast Pay (docs/capital/IMPLEMENTATION.md) adds the legal entity's own
    public attributes: legal name, sector, government flag, CIPC status, and
    capital-desk holds. They describe the debtor, not any tenant's dealings
    with it, and are still never serialised to a tenant.
    """
    SECTOR_CHOICES = [
        ('RETAIL_FMCG', 'Retail / FMCG'),
        ('MINING', 'Mining'),
        ('AGRI', 'Agriculture'),
        ('CONSTRUCTION', 'Construction'),
        ('MANUFACTURING', 'Manufacturing'),
        ('FUEL', 'Fuel'),
        ('LOGISTICS', 'Logistics'),
        ('GOVERNMENT', 'Government / SOE'),
        ('OTHER', 'Other'),
        ('UNKNOWN', 'Unknown'),
    ]
    CIPC_STATUS_CHOICES = [
        ('UNKNOWN', 'Not checked'),
        ('IN_BUSINESS', 'In business'),
        ('DEREGISTRATION', 'Deregistration process'),
        ('DEREGISTERED', 'Deregistered'),
        ('BUSINESS_RESCUE', 'Business rescue'),
        ('LIQUIDATION', 'Liquidation'),
    ]
    CESSION_CHOICES = [
        ('UNKNOWN', 'Unknown'),
        ('CLEARED', 'Cleared'),
        ('ACKNOWLEDGED', 'Acknowledgement on file'),
        ('PROHIBITED', 'Cession prohibited'),
    ]
    registration_number = models.CharField(max_length=20, null=True, blank=True, unique=True)
    vat_number = models.CharField(max_length=20, null=True, blank=True, unique=True)
    legal_name_key = models.CharField(max_length=200, db_index=True, blank=True, default='')
    country = models.CharField(max_length=2, default='ZA')
    legal_name = models.CharField(max_length=200, blank=True, default='')
    sector = models.CharField(max_length=20, choices=SECTOR_CHOICES, default='UNKNOWN', db_index=True)
    is_government = models.BooleanField(default=False)
    cipc_status = models.CharField(max_length=20, choices=CIPC_STATUS_CHOICES, default='UNKNOWN')
    cipc_checked_at = models.DateTimeField(null=True, blank=True)
    incorporation_date = models.DateField(null=True, blank=True)
    cession_status = models.CharField(max_length=20, choices=CESSION_CHOICES, default='UNKNOWN')
    # Capital-desk hold: no new Fast Pay exposure while set. Existing advances run off.
    on_hold = models.BooleanField(default=False)
    hold_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'debtor_identities'
        verbose_name_plural = 'Debtor identities'

    def __str__(self):
        return self.registration_number or self.vat_number or f'Debtor {self.pk}'

    @property
    def is_foreign(self) -> bool:
        return (self.country or 'ZA').upper() != 'ZA'

    @property
    def display_name(self) -> str:
        return self.legal_name or self.legal_name_key or str(self)
