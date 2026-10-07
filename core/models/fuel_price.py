from django.db import models
from django.utils import timezone


class FuelPrice(models.Model):
    """South African fuel prices, one row per calendar month (`date` is the 1st).

    Diesel has no regulated retail price in SA: the published figure is the
    *wholesale list price*, for Gauteng (inland reference) and Coast. Rows
    written by the FIASA feed from 2026-09 onwards store the 50ppm (0.005%)
    grade in diesel_inland/diesel_coastal (``diesel_grade='50ppm'``) and keep
    the 500ppm (0.05%) figures alongside. Older rows have ``diesel_grade``
    NULL: FIASA rows from before that change hold the 500ppm figure.
    """

    DIESEL_GRADE_CHOICES = [('50ppm', 'Diesel 50ppm (0.005% S)'), ('500ppm', 'Diesel 500ppm (0.05% S)')]

    date = models.DateField(
        unique=True,
        help_text='Calendar month this row belongs to (always the 1st). The '
                  'moment the stored price took effect is effective_from.',
    )
    diesel_inland = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Diesel inland (Gauteng) wholesale list price (ZAR/litre); grade in diesel_grade',
    )
    diesel_coastal = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Diesel coastal wholesale list price (ZAR/litre); grade in diesel_grade',
    )
    diesel_grade = models.CharField(
        max_length=10, choices=DIESEL_GRADE_CHOICES, null=True, blank=True,
        help_text='Grade of diesel_inland/diesel_coastal. NULL = not recorded '
                  '(legacy rows, fallback table, manual entry, regex scrapers).',
    )
    diesel_500ppm_inland = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Diesel 500ppm (0.05% S) inland wholesale list price (ZAR/litre), when known',
    )
    diesel_500ppm_coastal = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Diesel 500ppm (0.05% S) coastal wholesale list price (ZAR/litre), when known',
    )
    effective_from = models.DateTimeField(
        null=True, blank=True,
        help_text='When the stored price took effect (SA adjustments: first '
                  'Wednesday of the month, 00:01 SAST). NULL on legacy/fallback rows.',
    )
    petrol_95 = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Petrol 95 ULP inland (Gauteng) retail price (ZAR/litre); NULL when not published',
        null=True,
        blank=True,
    )
    petrol_93 = models.DecimalField(
        max_digits=8, decimal_places=4,
        help_text='Petrol 93 ULP inland (Gauteng) retail price (ZAR/litre); NULL when not published',
        null=True,
        blank=True,
    )
    petrol_95_coastal = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Petrol 95 ULP coastal retail price (ZAR/litre); NULL when not published',
    )
    petrol_93_coastal = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Petrol 93 ULP coastal retail price (ZAR/litre); NULL when not published '
                  '(93 is normally sold inland only)',
    )
    source = models.CharField(
        max_length=100,
        default='SAPIA',
        help_text='Data source identifier (FIASA, globalpetrolprices.com, etc)',
    )
    fetched_at = models.DateTimeField(
        default=timezone.now,
        help_text='When the stored price was last confirmed by a check that succeeded '
                  '(or, for fallback rows, last attempted)',
    )
    fetch_failed_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set when the most recent refresh failed and the stored price was '
                  'kept rather than overwritten; cleared by the next successful refresh.',
    )
    is_stale = models.BooleanField(default=False, help_text='True if price is older than 7 days')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fuel_prices'
        ordering = ['-date']

    def __str__(self):
        return (
            f"FuelPrice {self.date:%Y-%m} | "
            f"Diesel inland R{self.diesel_inland} coastal R{self.diesel_coastal}"
        )
