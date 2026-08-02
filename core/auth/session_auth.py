"""DRF token auth backed by per-device UserSession rows.

Keeps the ``Token`` header keyword and the standard token-parsing behavior of
DRF's TokenAuthentication, but resolves the key against ``UserSession`` (one
row per device) instead of the single shared authtoken Token. This lets each
device be listed and revoked independently.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication

from core.models import UserSession

# Only write last_activity if it's older than this, to avoid a DB write on
# every authenticated request (this SPA fires many).
ACTIVITY_UPDATE_WINDOW = timedelta(minutes=5)

# Sessions idle longer than this are expired, but only for users who have the
# "Session timeout" security setting enabled. Configurable via settings.
SESSION_IDLE_TIMEOUT = timedelta(minutes=getattr(settings, 'SESSION_IDLE_TIMEOUT_MINUTES', 30))


class UserSessionTokenAuthentication(TokenAuthentication):
    """Resolve ``Authorization: Token <key>`` against UserSession.

    ``request.auth`` is set to the UserSession so LogoutView / SessionsView can
    act on the specific device.
    """

    keyword = 'Token'
    model = UserSession

    def authenticate_credentials(self, key):
        try:
            session = UserSession.objects.select_related('user').get(key=key)
        except UserSession.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid token.')

        if not session.user.is_active:
            raise exceptions.AuthenticationFailed('User inactive or deleted.')

        now = timezone.now()

        # Auto sign-out after inactivity when the user opted into "Session timeout".
        # Off by default — measured against the STORED last_activity, before
        # the refresh below.
        timeout_on = (session.user.security_settings or {}).get('session_timeout', False)
        if timeout_on and session.last_activity < now - SESSION_IDLE_TIMEOUT:
            session.delete()
            raise exceptions.AuthenticationFailed('Session expired due to inactivity.')

        if session.last_activity < now - ACTIVITY_UPDATE_WINDOW:
            # Bare single-column UPDATE — no model hooks, no instance race.
            UserSession.objects.filter(pk=session.pk).update(last_activity=now)

        return (session.user, session)
