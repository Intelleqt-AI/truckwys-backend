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


class CopilotUserMemory(models.Model):
    """Per-user copilot memory: short facts the user explicitly asked the agent to
    remember (preferences, context). Scoped to (user, company) so memory never
    follows a user into a different workspace if their company is ever re-bound."""
    MAX_FACTS = 20
    MAX_FACT_LEN = 300

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copilot_memories'
    )
    company = models.ForeignKey(
        'core.Company', on_delete=models.CASCADE, related_name='copilot_memories'
    )
    facts = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'copilot_user_memory'
        unique_together = [('user', 'company')]

    def __str__(self):
        return f'Memory for user {self.user_id} @ company {self.company_id} ({len(self.facts)} facts)'


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
    # Structured extras that must survive reload (e.g. {"proposal_id": 12} so the
    # confirm card rehydrates when a conversation is reopened).
    metadata = models.JSONField(default=dict, blank=True)
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
