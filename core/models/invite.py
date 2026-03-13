"""
Invite model for user invitations
"""
import uuid
from datetime import timedelta
from django.db import models
from django.utils import timezone
from django.conf import settings


class Invite(models.Model):
    """
    User invitation model for inviting new users to join a company account
    """
    email = models.EmailField(help_text="Email address of the invited user")
    token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        help_text="Unique invite token"
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='invites_sent',
        help_text="User who sent the invitation"
    )
    company = models.ForeignKey(
        'Company',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='invites',
        help_text="Company the user is being invited to join"
    )
    company_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Company name (fallback if company FK is null)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the invitation was accepted"
    )
    expires_at = models.DateTimeField(
        help_text="When the invitation expires"
    )

    class Meta:
        db_table = 'invites'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['token']),
            models.Index(fields=['email']),
        ]

    def __str__(self):
        return f"Invite for {self.email} by {self.invited_by.username}"

    def save(self, *args, **kwargs):
        """Set expiration date on creation (7 days from now)"""
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*args, **kwargs)

    def is_valid(self) -> bool:
        """Check if invitation is still valid"""
        return (
            self.accepted_at is None and
            self.expires_at > timezone.now()
        )

    def accept(self):
        """Mark invitation as accepted"""
        self.accepted_at = timezone.now()
        self.save(update_fields=['accepted_at'])

    @property
    def is_expired(self) -> bool:
        """Check if invitation has expired"""
        return timezone.now() > self.expires_at

    @property
    def is_accepted(self) -> bool:
        """Check if invitation has been accepted"""
        return self.accepted_at is not None

    def get_company_name(self) -> str:
        """Get company name from FK or fallback field"""
        if self.company:
            return self.company.company_name
        return self.company_name or "a company"
