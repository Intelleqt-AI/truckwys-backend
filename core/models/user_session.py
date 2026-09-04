"""
UserSession model — one row per per-device login token.

Replaces the single shared rest_framework.authtoken Token so individual
devices can be listed and revoked from the Security Settings panel.
"""
import binascii
import os
import uuid

from django.db import models
from django.utils import timezone


class UserSession(models.Model):
    """A per-device authentication session.

    ``key`` is the secret bearer token (sent as ``Authorization: Token <key>``)
    and is never serialized to the API. ``id`` is a public, opaque identifier
    used in the sessions list and the revoke URL.
    """

    # Public identifier surfaced in the API / revoke URL.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Secret bearer token — 40 hex chars, matching authtoken's format so the
    # Authorization header shape is unchanged. Never serialized.
    key = models.CharField(max_length=40, unique=True, db_index=True)
    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='sessions')
    device = models.CharField(max_length=100, blank=True, default='')
    user_agent = models.CharField(max_length=512, blank=True, default='')
    ip_address = models.CharField(max_length=45, blank=True, default='')  # 45 = max IPv6 text length
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity = models.DateTimeField(default=timezone.now)
    # The shared public demo login (demo@truckwys.com) is one Django User
    # shared by every visitor, so the one-quote cap can't live on that User
    # or its Company — every visitor would share the same counter. It lives
    # here instead, since a fresh login already gets its own UserSession row
    # (core.auth.session_auth.UserSessionTokenAuthentication) — giving each
    # visitor their own quote independent of what anyone else has used.
    # Meaningless (stays False) for every non-demo session.
    demo_quote_used = models.BooleanField(default=False)

    class Meta:
        db_table = 'user_sessions'
        ordering = ['-last_activity']
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['last_activity']),
        ]

    @classmethod
    def generate_key(cls):
        return binascii.hexlify(os.urandom(20)).decode()

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = self.generate_key()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user_id} / {self.device or 'session'}"
