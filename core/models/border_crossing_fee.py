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
    # Every published schedule behind these fees is banded — Namibia charges per
    # axle, Botswana by GVM band, Lesotho and eSwatini by "foreign 4+ axle". A
    # single row per corridor meant a 15t rigid paid a 7-axle interlink's rate.
    # The quote's own weight picks the band; a corridor with one row still
    # behaves exactly as before.
    min_weight_kg = models.PositiveIntegerField(
        default=0,
        help_text='Lowest load weight this row applies to, in kg (inclusive).',
    )
    max_weight_kg = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Highest load weight this row applies to, in kg (inclusive). '
                  'Blank means no upper limit.',
    )
    notes = models.CharField(
        # Widened from 200 (see migration 0121_widen_border_crossing_fee_notes):
        # real corridor entries cite the actual regulation (a Statutory
        # Instrument, a Decreto, a published RFA/SORCA rate) rather than a
        # one-line guess, and the longest of those on record is 318 chars.
        # 500 leaves headroom for the next country's citation without being
        # unbounded.
        max_length=500,
        blank=True,
        help_text='e.g. includes COMESA transit docs',
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'border_crossing_fees'
        unique_together = [('from_country', 'to_country', 'min_weight_kg')]
        ordering = ['from_country', 'to_country', 'min_weight_kg']

    def __str__(self) -> str:
        return f"{self.from_country} → {self.to_country}: R{self.fee_zar:.2f}"

    @classmethod
    def rows_for(cls, from_country: str, to_country: str):
        return cls.objects.filter(
            from_country=from_country.upper(),
            to_country=to_country.upper(),
            is_active=True,
        ).order_by('min_weight_kg')

    @classmethod
    def get_fee_for_weight(cls, from_country: str, to_country: str, weight_kg: float = 0):
        """Fee for this corridor at this load weight.

        Returns (fee, row, exact) where `exact` is False when the weight fell
        outside every band and the lightest row on file was used instead — the
        caller surfaces that, because charging an interlink's rate to a rigid
        is a real over-charge and should not be silent.
        """
        rows = list(cls.rows_for(from_country, to_country))
        if not rows:
            return Decimal('0'), None, True
        w = int(weight_kg or 0)
        for row in rows:
            if w >= row.min_weight_kg and (row.max_weight_kg is None or w <= row.max_weight_kg):
                return row.fee_zar, row, True
        # No band covers this weight (only heavier bands exist) — use the
        # lightest one on file and tell the caller it is not a real match.
        row = rows[0]
        return row.fee_zar, row, False

    @classmethod
    def get_fee(cls, from_country: str, to_country: str, weight_kg: float = 0) -> Decimal:
        """Back-compatible wrapper — fee only, band mismatch not reported."""
        fee, _row, _exact = cls.get_fee_for_weight(from_country, to_country, weight_kg)
        return fee
