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
