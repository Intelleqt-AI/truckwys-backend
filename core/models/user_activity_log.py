"""Raw per-request activity trail — every authenticated API call a user
makes, captured by core.middleware.UserActivityLoggingMiddleware.

Deliberately separate from AuditLog: AuditLog is compliance-grade (writes,
logins — low volume, kept indefinitely); this is a high-volume behavioral
stream (every request, reads included) with a short retention window
(core.services.activity_retention.sweep_stale_activity_logs). Mixing the two
would bloat AuditLog for the thing it's actually used for.
"""
from django.conf import settings
from django.db import models


class UserActivityLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_logs',
    )
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='user_activity_logs',
    )
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255)
    status_code = models.PositiveSmallIntegerField()
    duration_ms = models.PositiveIntegerField()
    ip_address = models.CharField(max_length=45, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_activity_logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['company', '-created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.user_id} {self.method} {self.path} ({self.status_code})'
