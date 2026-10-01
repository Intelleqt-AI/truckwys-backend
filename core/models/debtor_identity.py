"""Global (cross-tenant) debtor identity, keyed on CIPC / VAT number."""
from django.db import models


class DebtorIdentity(models.Model):
    """The legal entity behind tenant Customer rows.

    NOT tenant data and never serialised to a tenant: a tenant only ever sees
    its own Customer. It exists so Capital (Fast Pay) can recognise the same
    debtor across transporters for concentration limits and network payment
    behaviour (docs/capital-risk/03-design.md §2.1), under the funder's
    POPIA basis. Holds identifiers only, no tenant-specific data.
    """
    registration_number = models.CharField(max_length=20, null=True, blank=True, unique=True)
    vat_number = models.CharField(max_length=20, null=True, blank=True, unique=True)
    legal_name_key = models.CharField(max_length=200, db_index=True, blank=True, default='')
    country = models.CharField(max_length=2, default='ZA')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'debtor_identities'
        verbose_name_plural = 'Debtor identities'

    def __str__(self):
        return self.registration_number or self.vat_number or f'Debtor {self.pk}'
