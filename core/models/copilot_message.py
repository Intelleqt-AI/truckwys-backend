from django.conf import settings
from django.db import models


class CopilotMessage(models.Model):
    """A single turn in a user's Copilot conversation.

    Persisted so the conversation survives navigation, refresh and device
    changes — the user can leave the page mid-chat and pick up where they
    left off. One continuous thread per user (cleared via "New chat").
    """
    ROLE_CHOICES = [('user', 'User'), ('assistant', 'Assistant')]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copilot_messages'
    )
    role = models.CharField(max_length=12, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'copilot_messages'
        ordering = ['created_at']
        indexes = [models.Index(fields=['user', 'created_at'])]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"
