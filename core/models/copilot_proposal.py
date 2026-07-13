from django.conf import settings
from django.db import models


class CopilotProposal(models.Model):
    """A pending database write prepared by the Copilot agent.

    Tools never mutate data directly: they persist one of these and the user
    confirms it in the chat UI, which executes it by id via
    /api/v1/agent/proposals/<id>/execute/. The client never supplies the
    endpoint or payload — everything needed to execute lives server-side here.
    """
    OPERATIONS = [('CREATE', 'Create'), ('UPDATE', 'Update'), ('DELETE', 'Delete'), ('SEND', 'Send')]
    STATUSES = [
        ('PENDING', 'Pending'),
        ('EXECUTED', 'Executed'),
        ('DISMISSED', 'Dismissed'),
        ('EXPIRED', 'Expired'),
        ('FAILED', 'Failed'),
    ]

    company = models.ForeignKey('core.Company', on_delete=models.CASCADE, related_name='copilot_proposals')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='copilot_proposals')
    conversation = models.ForeignKey(
        'core.CopilotConversation', on_delete=models.CASCADE,
        related_name='proposals', null=True, blank=True,
    )
    table = models.CharField(max_length=40)  # ENTITY_REGISTRY key
    operation = models.CharField(max_length=10, choices=OPERATIONS)
    target_id = models.CharField(max_length=64, blank=True, default='')  # UPDATE/DELETE target pk
    payload = models.JSONField(default=dict)   # validated writable fields
    display = models.JSONField(default=list)   # [{label, value, old_value?}] for the confirm card
    warning = models.CharField(max_length=300, blank=True, default='')
    # AI research findings shown on the card (e.g. why an email was drafted this way).
    analysis_summary = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=STATUSES, default='PENDING', db_index=True)
    result = models.JSONField(default=dict)    # {id, number, route} or {error}
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'copilot_proposals'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['conversation', 'status']),
        ]

    def __str__(self):
        return f"{self.operation} {self.table} ({self.status})"
