"""
InviteToken model for team member invitations.

Stores invite tokens with expiry and role information.
"""
from django.db import models
from django.utils import timezone
import uuid
from datetime import timedelta


class InviteToken(models.Model):
    """Team member invitation token."""

    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('manager', 'Manager'),
        ('operator', 'Operator'),
        ('viewer', 'Viewer'),
        ('driver', 'Driver'),
    ]

    # UUID token for secure invite links
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    # Who invited and to which company
    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='invites')
    invited_by = models.ForeignKey('User', on_delete=models.CASCADE, related_name='sent_invites')

    # Invitee details
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)

    # Token lifecycle
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        db_table = 'invite_tokens'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['token']),
            models.Index(fields=['email']),
        ]

    def save(self, *args, **kwargs):
        """Set expiry to 7 days from now if not set."""
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*args, **kwargs)

    def is_valid(self):
        """Check if token is still valid (not expired and not used)."""
        return not self.is_used and timezone.now() < self.expires_at

    def mark_as_used(self):
        """Mark token as used."""
        self.is_used = True
        self.used_at = timezone.now()
        self.save()

    def __str__(self):
        return f"Invite for {self.email} to {self.company.company_name}"
