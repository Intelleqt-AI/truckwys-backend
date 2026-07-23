"""Best-effort recording of auth events (sign-in/sign-out) into AuditLog.

Auth flows must never fail because an audit write did, so log_auth_event
swallows every exception — same "best-effort" stance as the login-alert email.
"""
import logging

from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address

logger = logging.getLogger(__name__)

# Event subtype -> AuditLog.action. The subtype itself is kept in details.event
# so the activity feed can distinguish an explicit logout from a revocation.
EVENT_ACTIONS = {
    'login': 'LOGIN',
    'logout': 'LOGOUT',
    'revoked': 'LOGOUT',
    'revoked_others': 'LOGOUT',
    'revoked_all': 'LOGOUT',
}


def log_auth_event(user, event, request=None, session=None, device=None, **extra):
    """Record one LOGIN/LOGOUT AuditLog row for the user's activity feed."""
    try:
        from core.models import AuditLog
        from core.utils.request_meta import parse_device, client_ip

        # AuditLog.ip_address is a GenericIPAddressField (inet in Postgres),
        # while session.ip_address/client_ip() are display strings that may be
        # '' or junk from X-Forwarded-For — validate or store NULL.
        ip = (getattr(session, 'ip_address', '') or
              (client_ip(request) if request else '')) or None
        if ip:
            try:
                validate_ipv46_address(ip)
            except (ValidationError, TypeError):
                ip = None

        device = device or (getattr(session, 'device', '') or
                            (parse_device(request) if request else '')) or 'Unknown device'

        AuditLog.log_action(
            action=EVENT_ACTIONS.get(event, 'OTHER'),
            resource_type='UserSession',
            resource_id=str(session.id) if session else '-',
            user=user,
            details={'event': event, 'device': device, **extra},
            ip_address=ip,
        )
    except Exception:
        logger.warning(
            'auth event %r not recorded for user %s',
            event, getattr(user, 'id', None), exc_info=True,
        )
