"""What the pricing analysis showed when a quote was saved, and what the
operator picked — the record that lets us later ask "did Balanced win more
often than Stretch?" and calibrate the likelihoods against real outcomes.

One row per quote (the latest save wins). `payload` keeps the full decision
object the client sent (validated, size-capped); the typed columns are the
load-bearing ones for reporting.
"""
from django.conf import settings
from django.db import models


class QuotePricingDecision(models.Model):
    PICKED_CHOICES = [
        ('safe', 'Safe'),
        ('balanced', 'Balanced'),
        ('stretch', 'Stretch'),
        ('custom', 'Custom'),
    ]
    LEVEL_CHOICES = [('model', 'Model'), ('rules', 'Rules')]

    quote = models.OneToOneField('Quote', on_delete=models.CASCADE, related_name='pricing_decision')
    company = models.ForeignKey('Company', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='quote_pricing_decisions')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='quote_pricing_decisions')

    version = models.CharField(max_length=20, blank=True)
    picked_choice = models.CharField(max_length=10, choices=PICKED_CHOICES, blank=True)
    final_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    floor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    likelihood_level = models.CharField(max_length=10, choices=LEVEL_CHOICES, blank=True)
    likelihood_at_final_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    band_at_final = models.CharField(max_length=20, blank=True)
    model_version = models.CharField(max_length=60, blank=True)
    market_tier = models.CharField(max_length=20, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quote_pricing_decisions'
        indexes = [models.Index(fields=['company', 'picked_choice'])]

    def __str__(self):
        return f'QuotePricingDecision(quote={self.quote_id}, {self.picked_choice}, {self.likelihood_level})'
