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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event_type}: {self.title}"
