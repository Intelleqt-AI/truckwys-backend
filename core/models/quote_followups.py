"""Quote follow-ups (Oct 2026): fuel price clause, fuel change alerts,
expiry / no-answer nudges, the weekly margin email and the pricing setup flag.

Kept in their own tables (not new Company / Quote columns) so this feature
merges cleanly with the branches that edit those models.
"""
from decimal import Decimal

from django.db import models

# Server-side defaults (db_default) on every NOT NULL column that has a
# default: an app image that predates a column can still insert rows.
JSON_EMPTY_OBJ = models.Value({}, output_field=models.JSONField())
JSON_EMPTY_LIST = models.Value([], output_field=models.JSONField())


class QuoteAutomationSettings(models.Model):
    """Per-company settings for the follow-up features. One row per company,
    created by migration 0179 for existing companies (fuel clause OFF with a
    one-time prompt) and on company creation for new ones (clause ON)."""

    company = models.OneToOneField('Company', on_delete=models.CASCADE, related_name='quote_automation')

    # 1. Fuel price clause on quotes (PDF line + invoice adjustment).
    fuel_surcharge_enabled = models.BooleanField(default=True, db_default=True)
    fuel_surcharge_threshold_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('5.00'), db_default=Decimal('5.00'))
    # Existing companies start OFF and are asked once (clients show the prompt
    # while this is true; any decision through the settings endpoint clears it).
    fuel_surcharge_prompt_pending = models.BooleanField(default=False, db_default=False)
    fuel_surcharge_decided_at = models.DateTimeField(null=True, blank=True)

    # 2. Fuel change alert (bell/push always per user prefs; email per user prefs).
    fuel_alerts_enabled = models.BooleanField(default=True, db_default=True)

    # 3. Expiry and no-answer nudges.
    follow_ups_enabled = models.BooleanField(default=True, db_default=True)
    follow_up_after_days = models.PositiveSmallIntegerField(default=3, db_default=3)
    expiry_nudge_days = models.PositiveSmallIntegerField(default=2, db_default=2)

    # 4. Weekly margin email to admins (Monday 07:00 SAST).
    weekly_margin_email_enabled = models.BooleanField(default=True, db_default=True)

    # 5. Pricing setup: {key: {"how": "changed"|"confirmed"|"inferred", "at": iso}}
    pricing_setup = models.JSONField(default=dict, blank=True, db_default=JSON_EMPTY_OBJ)
    pricing_setup_dismissed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quote_automation_settings'

    def __str__(self):
        return f'Quote automation settings ({self.company_id})'


class QuoteFollowUp(models.Model):
    """When a quote was sent and which nudges / reminders it has had in the
    current send cycle. A new transition into SENT starts a new cycle."""

    SENT_AT_RECORDED = 'recorded'
    SENT_AT_ESTIMATED = 'estimated'   # backfilled from updated_at for quotes sent before Oct 2026

    quote = models.OneToOneField('Quote', on_delete=models.CASCADE, related_name='follow_up')
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_at_source = models.CharField(max_length=10, default=SENT_AT_RECORDED, db_default=SENT_AT_RECORDED)
    expiry_nudged_at = models.DateTimeField(null=True, blank=True)
    expiry_nudged_for = models.DateField(null=True, blank=True, help_text='valid_until the expiry nudge was for')
    no_answer_nudged_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    reminder_count = models.PositiveSmallIntegerField(default=0, db_default=0)
    reminder_last_to = models.CharField(max_length=254, blank=True, default='', db_default='')
    reminder_last_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quote_follow_ups'


class QuoteFuelClause(models.Model):
    """The fuel price clause as it was on the quote the customer received
    (stamped when the quote is sent). Only a stamped clause adjusts an invoice."""

    quote = models.OneToOneField('Quote', on_delete=models.CASCADE, related_name='fuel_clause')
    threshold_pct = models.DecimalField(max_digits=5, decimal_places=2)
    basis_price = models.DecimalField(max_digits=8, decimal_places=4, help_text='Official zone price at pricing (R/L)')
    basis_effective_from = models.DateTimeField(null=True, blank=True)
    zone = models.CharField(max_length=10)
    product = models.CharField(max_length=12, help_text="'diesel' | 'petrol_95' | 'petrol_93'")
    litres = models.DecimalField(max_digits=10, decimal_places=3)
    text = models.TextField()
    stamped_at = models.DateTimeField()

    class Meta:
        db_table = 'quote_fuel_clauses'


class FuelChangeAlert(models.Model):
    """One alert per company per official price period per fuel product."""

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='fuel_change_alerts')
    period_start = models.DateField(help_text='First-Wednesday date of the price period (SAST)')
    product = models.CharField(max_length=12)
    zone = models.CharField(max_length=10)
    old_price = models.DecimalField(max_digits=8, decimal_places=4)
    new_price = models.DecimalField(max_digits=8, decimal_places=4)
    effective_from = models.DateTimeField()
    target_margin_pct = models.DecimalField(max_digits=6, decimal_places=2)
    quotes = models.JSONField(default=list, blank=True, db_default=JSON_EMPTY_LIST)
    quotes_affected = models.PositiveIntegerField(default=0, db_default=0)
    quotes_under_target = models.PositiveIntegerField(default=0, db_default=0)
    title = models.CharField(max_length=200, blank=True, default='', db_default='')
    message = models.TextField(blank=True, default='', db_default='')
    notified_at = models.DateTimeField(null=True, blank=True)
    emails_sent = models.PositiveIntegerField(default=0, db_default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'fuel_change_alerts'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['company', 'period_start', 'product'],
                                    name='uniq_fuel_change_alert_company_period_product'),
        ]


class WeeklyMarginReport(models.Model):
    """The Monday margin email, one per company per week (idempotency + history)."""

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='weekly_margin_reports')
    week_start = models.DateField()
    figures = models.JSONField(default=dict, blank=True, db_default=JSON_EMPTY_OBJ)
    recipients = models.PositiveIntegerField(default=0, db_default=0)
    emails_sent = models.PositiveIntegerField(default=0, db_default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'weekly_margin_reports'
        ordering = ['-week_start']
        constraints = [
            models.UniqueConstraint(fields=['company', 'week_start'], name='uniq_weekly_margin_report_company_week'),
        ]
