import secrets
from django.db import models
from django.conf import settings


def generate_api_key():
    return secrets.token_hex(32)


class IntegrationAPIKey(models.Model):
    """API keys for third-party integrations (Lenders, Fleet TMS, Partners)."""

    KEY_TYPES = [
        ('LENDER', 'Lender'),
        ('FLEET_TMS', 'Fleet TMS'),
        ('PARTNER', 'Partner'),
    ]

    name = models.CharField(max_length=100, help_text="Friendly name for this key (e.g., 'ABC Fleet System')")
    key = models.CharField(max_length=64, unique=True, default=generate_api_key, db_index=True)
    key_type = models.CharField(max_length=20, choices=KEY_TYPES, default='FLEET_TMS')
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='api_keys',
        help_text="User/company that owns this key"
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    # --- Metering / quota (for the productised Risk-Scoring API) ---
    usage_count = models.PositiveIntegerField(default=0, help_text="Total metered API calls on this key")
    monthly_quota = models.PositiveIntegerField(default=0, help_text="Calls allowed per calendar month (0 = unlimited)")
    quota_used = models.PositiveIntegerField(default=0, help_text="Calls used in the current quota window")
    quota_period = models.CharField(max_length=7, blank=True, default='', help_text="YYYY-MM window quota_used tracks")

    class Meta:
        db_table = 'integration_api_keys'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['key', 'active']),
            models.Index(fields=['key_type', 'active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.key_type}) - {self.key[:16]}..."

    def mark_used(self):
        """Update last_used_at to now."""
        from django.utils import timezone
        self.last_used_at = timezone.now()
        self.save(update_fields=['last_used_at'])

    def _current_period(self):
        from django.utils import timezone
        return timezone.now().strftime('%Y-%m')

    def is_over_quota(self) -> bool:
        """True if this key has exhausted its monthly quota (0 = unlimited)."""
        if not self.monthly_quota:
            return False
        period = self._current_period()
        used = self.quota_used if self.quota_period == period else 0
        return used >= self.monthly_quota

    def record_call(self):
        """Meter one billable API call: bump lifetime + rolling-month counters."""
        from django.utils import timezone
        now = timezone.now()
        period = now.strftime('%Y-%m')
        if self.quota_period != period:
            self.quota_period = period
            self.quota_used = 0
        self.usage_count = (self.usage_count or 0) + 1
        self.quota_used = (self.quota_used or 0) + 1
        self.last_used_at = now
        self.save(update_fields=['usage_count', 'quota_used', 'quota_period', 'last_used_at'])
