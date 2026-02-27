import hmac
import hashlib
import secrets
from django.db import models
from django.conf import settings


def generate_webhook_secret():
    return secrets.token_hex(32)


class Webhook(models.Model):
    """Webhook endpoint for outbound event notifications."""

    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='webhooks'
    )
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=64, default=generate_webhook_secret)
    events = models.JSONField(default=list, help_text="List of event types to subscribe to")
    active = models.BooleanField(default=True)
    failure_count = models.IntegerField(default=0)
    last_fired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'webhooks'
        ordering = ['-created_at']

    def __str__(self):
        return f"Webhook {self.id} - {self.url}"

    def sign_payload(self, body: str) -> str:
        """Generate HMAC signature for payload."""
        return 'sha256=' + hmac.new(
            self.secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()
