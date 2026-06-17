from django.conf import settings
from django.db import models


class CopilotConversation(models.Model):
    """A Copilot conversation thread. 'New chat' starts a new one; old threads
    are kept and browsable rather than destroyed."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copilot_conversations'
    )
    title = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'copilot_conversations'
        ordering = ['-updated_at']
        indexes = [models.Index(fields=['user', '-updated_at'])]

    def __str__(self):
        return self.title or f'Conversation {self.pk}'


class CopilotMessage(models.Model):
    """A single turn in a Copilot conversation. Persisted so conversations
    survive navigation, refresh and device changes."""
    ROLE_CHOICES = [('user', 'User'), ('assistant', 'Assistant')]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copilot_messages'
    )
    conversation = models.ForeignKey(
        CopilotConversation, on_delete=models.CASCADE, related_name='messages', null=True, blank=True
    )
    role = models.CharField(max_length=12, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'copilot_messages'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['conversation', 'created_at']),
        ]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"
