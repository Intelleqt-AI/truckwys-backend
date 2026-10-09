import uuid
import secrets
from django.db import models


def generate_api_key():
    """Generate UUID-based API key for webhook subscriptions."""
    return str(uuid.uuid4())


def generate_webhook_secret():
    """Generate secret key for webhook signature verification."""
    return secrets.token_hex(32)


class WebhookSubscription(models.Model):
    """Partner webhook subscriptions for event delivery."""

    partner_name = models.CharField(max_length=200, help_text="Partner company name")
    webhook_url = models.URLField(max_length=500, help_text="URL to deliver webhook events")
    events = models.JSONField(
        default=list,
        help_text="List of event types to subscribe to (e.g., ['invoice.created', 'load.delivered'])"
    )
    api_key = models.CharField(
        max_length=64,
        unique=True,
        default=generate_api_key,
        db_index=True,
        help_text="API key for authenticating partner API requests"
    )
    secret = models.CharField(
        max_length=64,
        default=generate_webhook_secret,
        help_text="Secret key used for HMAC signature verification"
    )
    is_active = models.BooleanField(default=True)
    # The transporter (tenant) that owns this subscription. Outbound events
    # are delivered ONLY to subscriptions of the event's own company, and the
    # fleet webhooks (/fleet/webhooks/*) act only for this company; with no
    # company it receives nothing and those webhooks refuse it (fail closed). Nullable
    # only so pre-existing rows survive the migration — bind them in Django
    # admin after reviewing them (manage.py audit_webhook_subscriptions).
    company = models.ForeignKey(
        'Company', on_delete=models.CASCADE, null=True, blank=True, related_name='webhook_subscriptions',
        help_text="Transporter that owns this subscription. Empty = receives no events.",
    )
    # The trip-update webhook's old body-only signature (no timestamp) can be
    # replayed; it is accepted only for subscriptions opted in here (Django
    # admin), and each such signature only once (replay cache).
    allow_legacy_signature = models.BooleanField(default=False, db_default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_delivery_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last successful webhook delivery timestamp"
    )
    failure_count = models.IntegerField(
        default=0,
        help_text="Number of consecutive delivery failures"
    )

    class Meta:
        db_table = 'webhook_subscriptions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['api_key', 'is_active']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"{self.partner_name} - {self.webhook_url}"

    def mark_delivery(self, success=True):
        """Update delivery status after webhook attempt."""
        from django.utils import timezone
        if success:
            self.last_delivery_at = timezone.now()
            self.failure_count = 0
        else:
            self.failure_count += 1
            # Auto-disable after 10 consecutive failures
            if self.failure_count >= 10:
                self.is_active = False
        self.save(update_fields=['last_delivery_at', 'failure_count', 'is_active'])


class UsedWebhookSignature(models.Model):
    """A fleet webhook signature already accepted (replay protection).

    A database table rather than the shared cache: the cache is culled (300
    entries, throttle keys churn it), which let replays through again. Rows
    expire (expires_at) and are deleted by core.tasks.purge_used_webhook_signatures
    (hourly) and opportunistically on use."""
    key = models.CharField(max_length=100, unique=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = 'used_webhook_signatures'

    def __str__(self):
        return self.key
