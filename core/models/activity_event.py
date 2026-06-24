from django.conf import settings
from django.db import models


class ActivityEvent(models.Model):
    """Activity event tracking for system-wide actions"""

    EVENT_TYPES = [
        ('load', 'Load'),
        ('invoice', 'Invoice'),
        ('advance', 'Advance'),
        ('quote', 'Quote'),
        ('system', 'System'),
    ]

    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    entity_id = models.IntegerField(null=True, blank=True)
    entity_type = models.CharField(max_length=50, blank=True)
    metadata = models.JSONField(default=dict)
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_events',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event_type}: {self.title}"
