"""Audit Log model for tracking system actions."""

from django.db import models
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from typing import Any


class AuditLog(models.Model):
    """
    Audit log for tracking user actions and system events.

    Records who did what, when, and to which resource for compliance
    and debugging purposes.
    """

    ACTION_CHOICES = [
        ('CREATE', 'Create'),
        ('UPDATE', 'Update'),
        ('DELETE', 'Delete'),
        ('VIEW', 'View'),
        ('LOGIN', 'Login'),
        ('LOGOUT', 'Logout'),
        ('APPROVE', 'Approve'),
        ('DENY', 'Deny'),
        ('DISBURSE', 'Disburse'),
        ('SETTLE', 'Settle'),
        ('CANCEL', 'Cancel'),
        ('EXPORT', 'Export'),
        ('IMPORT', 'Import'),
        ('OTHER', 'Other'),
    ]

    # User who performed the action (nullable for system actions)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
        help_text='User who performed the action (null for system actions)'
    )

    # Action details
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES,
        db_index=True,
        help_text='Type of action performed'
    )

    # Resource information (generic to any model)
    resource_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text='Type of resource affected (model name)'
    )
    resource_id = models.CharField(
        max_length=100,
        db_index=True,
        help_text='ID of the affected resource'
    )

    # Additional context
    details = models.JSONField(
        default=dict,
        help_text='Additional details about the action (JSON)'
    )

    # Request metadata
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text='IP address of the requester'
    )

    # Timestamp
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True
    )

    class Meta:
        db_table = 'audit_logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['resource_type', 'resource_id']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['action', '-created_at']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self) -> str:
        user_str = self.user.email if self.user else 'SYSTEM'
        return f"{user_str} {self.action} {self.resource_type}:{self.resource_id}"

    @classmethod
    def log_action(
        cls,
        action: str,
        resource_type: str,
        resource_id: str | int,
        user: Any = None,
        details: dict | None = None,
        ip_address: str | None = None
    ) -> 'AuditLog':
        """
        Create an audit log entry.

        Args:
            action: Action type (from ACTION_CHOICES)
            resource_type: Type of resource (model name)
            resource_id: ID of the resource
            user: User who performed the action (optional)
            details: Additional details dictionary (optional)
            ip_address: IP address of requester (optional)

        Returns:
            AuditLog: Created audit log entry
        """
        return cls.objects.create(
            user=user,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            details=details or {},
            ip_address=ip_address
        )

    @classmethod
    def log_create(cls, instance: Any, user: Any = None, **kwargs) -> 'AuditLog':
        """Log a CREATE action."""
        return cls.log_action(
            action='CREATE',
            resource_type=instance.__class__.__name__,
            resource_id=instance.pk,
            user=user,
            **kwargs
        )

    @classmethod
    def log_update(cls, instance: Any, user: Any = None, changes: dict | None = None, **kwargs) -> 'AuditLog':
        """Log an UPDATE action."""
        details = kwargs.get('details', {})
        if changes:
            details['changes'] = changes

        return cls.log_action(
            action='UPDATE',
            resource_type=instance.__class__.__name__,
            resource_id=instance.pk,
            user=user,
            details=details,
            **{k: v for k, v in kwargs.items() if k != 'details'}
        )

    @classmethod
    def log_delete(cls, instance: Any, user: Any = None, **kwargs) -> 'AuditLog':
        """Log a DELETE action."""
        return cls.log_action(
            action='DELETE',
            resource_type=instance.__class__.__name__,
            resource_id=instance.pk,
            user=user,
            **kwargs
        )

    @classmethod
    def log_view(cls, instance: Any, user: Any = None, **kwargs) -> 'AuditLog':
        """Log a VIEW action."""
        return cls.log_action(
            action='VIEW',
            resource_type=instance.__class__.__name__,
            resource_id=instance.pk,
            user=user,
            **kwargs
        )

    @classmethod
    def get_resource_history(cls, resource_type: str, resource_id: str | int):
        """
        Get audit history for a specific resource.

        Args:
            resource_type: Type of resource
            resource_id: ID of resource

        Returns:
            QuerySet: Audit logs for this resource
        """
        return cls.objects.filter(
            resource_type=resource_type,
            resource_id=str(resource_id)
        )

    @classmethod
    def get_user_actions(cls, user: Any, limit: int = 100):
        """
        Get recent actions by a user.

        Args:
            user: User instance
            limit: Maximum number of entries to return

        Returns:
            QuerySet: Recent audit logs for this user
        """
        return cls.objects.filter(user=user)[:limit]
