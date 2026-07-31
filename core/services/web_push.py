"""Web Push delivery via pywebpush + VAPID.

Configuration (backend/.env):
    VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY  — generate with
        manage.py generate_vapid_keys
    VAPID_CLAIM_EMAIL — contact for the push service (mailto claim)

send_web_push(user, payload) pushes to every subscription the user has
registered and prunes endpoints the push service reports gone. Best-effort:
never raises — a push failure must not break the business action.
"""
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def vapid_configured() -> bool:
    return bool(getattr(settings, 'VAPID_PUBLIC_KEY', '') and
                getattr(settings, 'VAPID_PRIVATE_KEY', ''))


def send_web_push(user, payload: dict) -> int:
    """Push `payload` (title/message/link/type) to all of the user's browser
    subscriptions. Returns the number of successful pushes."""
    if not vapid_configured():
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed — web push skipped")
        return 0

    from core.models import PushSubscription
    sent = 0
    for sub in PushSubscription.objects.filter(user=user):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=json.dumps(payload),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{settings.VAPID_CLAIM_EMAIL}"},
                ttl=3600,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (404, 410):
                sub.delete()  # endpoint gone — browser unsubscribed
            else:
                logger.warning("web push to %s failed: %s", sub.endpoint[:40], exc)
        except Exception as exc:
            logger.warning("web push to %s failed: %s", sub.endpoint[:40], exc)
    return sent
