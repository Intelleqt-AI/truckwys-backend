from decimal import Decimal

from django.db import models


class BorderCrossingFee(models.Model):
    """One-way border crossing fee between two countries, denominated in ZAR."""

    from_country = models.CharField(
        max_length=3,
        help_text='ISO-2/3 country code e.g. SA',
    )
    to_country = models.CharField(max_length=3)
    fee_zar = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text='One-way border crossing fee in ZAR',
    )
    notes = models.CharField(
        max_length=200,
        blank=True,
        help_text='e.g. includes COMESA transit docs',
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'border_crossing_fees'
        unique_together = [('from_country', 'to_country')]
        ordering = ['from_country', 'to_country']

    def __str__(self) -> str:
        return f"{self.from_country} → {self.to_country}: R{self.fee_zar:.2f}"

    @classmethod
    def get_fee(cls, from_country: str, to_country: str) -> Decimal:
        """Return the active crossing fee for the given country pair.

        Falls back to Decimal('0') if no matching active record is found.
        """
        try:
            record = cls.objects.get(
                from_country=from_country.upper(),
                to_country=to_country.upper(),
                is_active=True,
            )
            return record.fee_zar
        except cls.DoesNotExist:
            return Decimal('0')
