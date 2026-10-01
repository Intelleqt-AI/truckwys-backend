"""Stored, verified figures that the AI quote price check compares against,
and proposed changes to them (core.services.verified_rates).

The per-quote check makes no web or OpenAI calls: it reads these rows (and
the SANRAL tariffs on TollPlaza). A monthly job (refresh_verified_rates)
looks the figures up on the web and, when a figure differs from the approved
one and the source page confirms it, writes a PENDING row here. Nothing is
applied until a superuser approves it.

  kind='driver_allowance'  the approved rows ARE the stored figure (one per
                           key and effective date; history is kept). The
                           current figure is the newest approved row in force.
  kind='toll_tariff'       only proposals and their history. The current
                           tariff lives on TollPlaza (tariff_class_N plus the
                           tariff_* verification fields); approving a row
                           writes the new tariff there.
"""
from django.conf import settings
from django.db import models


class VerifiedRate(models.Model):
    KIND_TOLL_TARIFF = 'toll_tariff'
    KIND_DRIVER_ALLOWANCE = 'driver_allowance'
    KIND_CHOICES = [
        (KIND_TOLL_TARIFF, 'SANRAL toll tariff'),
        (KIND_DRIVER_ALLOWANCE, 'Driver allowance'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    # A pending proposal replaced by a newer one (or made moot by another
    # approval) before anyone reviewed it.
    STATUS_SUPERSEDED = 'superseded'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending approval'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_SUPERSEDED, 'Superseded'),
    ]

    UNIT_CHOICES = [
        ('per_passage', 'Rand per passage (one way)'),
        ('per_night', 'Rand per night away'),
    ]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    # toll_tariff: 'toll:<plaza id>:class<SANRAL class 1-4>'
    # driver_allowance: the allowance type, 'nbcrfli' or 'sars_subsistence'
    key = models.CharField(max_length=100)
    label = models.CharField(max_length=200, blank=True)
    toll_plaza = models.ForeignKey('TollPlaza', null=True, blank=True, on_delete=models.CASCADE,
                                   related_name='verified_rates')
    sanral_class = models.PositiveSmallIntegerField(null=True, blank=True,
                                                    help_text='SANRAL class 1-4 (toll tariffs only)')

    # The figure the app prices with. Tolls: EXCLUDING VAT (quotes price tolls
    # excl. VAT). Allowances: as published (no VAT).
    value = models.DecimalField(max_digits=10, decimal_places=2)
    # The figure exactly as printed on the source (tolls: incl. VAT).
    published_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # The approved value when this row was proposed (same basis as `value`).
    previous_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=20, choices=UNIT_CHOICES)
    effective_from = models.DateField()

    source_url = models.URLField(max_length=1000, blank=True, default='')
    source_name = models.CharField(max_length=300, blank=True, default='')
    # Date the figure was last confirmed on source_url.
    verified_at = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # Who proposed it: 'refresh_verified_rates' (the job) or 'admin:<username>'.
    proposed_by = models.CharField(max_length=100, blank=True, default='')
    # The refresh job's usage/cost row that found it.
    refresh_run = models.ForeignKey('AIQuotePriceAnalysis', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='verified_rate_proposals')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='+')
    rejected_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    # What the job saw: research text excerpt, extracted figure, page check.
    evidence = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)   # "found at"
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'verified_rates'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['kind', 'key', 'status']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.key} R{self.value} from {self.effective_from} ({self.status})'
