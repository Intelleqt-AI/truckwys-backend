"""Tracks every run of the OpenAI-based quote price-verification panel
(core.services.quote_ai_pricing) — one row per "analysis run" (the research
web_search call plus the structuring call combined), so per-user and
all-time token/cost totals are a single Sum() query. See PLAN doc Part 3.
"""
from django.conf import settings
from django.db import models


class AIQuotePriceAnalysis(models.Model):
    TRIGGER_CHOICES = [
        ('auto', 'Auto (first run)'),
        ('manual', 'Manual re-check'),
    ]
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]
    FAILED_AT_CALL_CHOICES = [
        ('research', 'Research call (web_search)'),
        ('structuring', 'Structuring call'),
        ('pricing', 'Pricing / win scoring (after both calls)'),
    ]

    quote = models.ForeignKey(
        'Quote', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='ai_price_analyses',
        help_text='Null when run against an unsaved draft quote',
    )
    company = models.ForeignKey(
        'Company', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='ai_price_analyses',
    )
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ai_price_analyses',
    )
    trigger_type = models.CharField(max_length=10, choices=TRIGGER_CHOICES, default='auto')

    model = models.CharField(max_length=50, default='gpt-5.6-sol')
    reasoning_effort = models.CharField(max_length=10, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error_message = models.TextField(blank=True)
    failed_at_call = models.CharField(max_length=12, choices=FAILED_AT_CALL_CHOICES, blank=True)

    # Flat usage columns, one set per call — cheap Sum() for admin totals
    # instead of a JSON-extract aggregation.
    research_input_tokens = models.PositiveIntegerField(default=0)
    research_cached_tokens = models.PositiveIntegerField(default=0)
    research_output_tokens = models.PositiveIntegerField(default=0)
    research_reasoning_tokens = models.PositiveIntegerField(default=0)
    research_web_search_calls = models.PositiveIntegerField(default=0)
    structuring_input_tokens = models.PositiveIntegerField(default=0)
    structuring_cached_tokens = models.PositiveIntegerField(default=0)
    structuring_output_tokens = models.PositiveIntegerField(default=0)
    structuring_reasoning_tokens = models.PositiveIntegerField(default=0)

    token_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    web_search_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)

    suggested_price_zar = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    verification_status = models.CharField(max_length=20, blank=True)
    confidence = models.CharField(max_length=10, blank=True)

    # request_context = the condensed payload actually sent to OpenAI.
    # raw_result = {'research_text', 'citations', 'structured'} — everything
    # both calls produced, for debugging/audit.
    request_context = models.JSONField(default=dict, blank=True)
    raw_result = models.JSONField(default=dict, blank=True)

    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ai_quote_price_analyses'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company', '-created_at']),
            models.Index(fields=['triggered_by', '-created_at']),
            models.Index(fields=['quote', '-created_at']),
        ]

    def __str__(self):
        return f'AIQuotePriceAnalysis(quote={self.quote_id}, {self.status}, ${self.total_cost_usd})'
