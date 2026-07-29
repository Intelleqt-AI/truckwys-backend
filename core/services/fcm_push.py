"""Firebase Cloud Messaging push for the mobile app.

FCM is the transport for both platforms — Android natively, iOS through FCM's
APNs relay — so the same call reaches every device a user has installed. Sends
are best-effort and never raise: a push failure must not break the business
action that triggered it.

Configuration (settings / environment):
    FIREBASE_CREDENTIALS       path to the service-account JSON, or
    FIREBASE_CREDENTIALS_JSON  the JSON itself (for platforms without a filesystem)

Both blank => fcm_configured() is False and every send is a no-op, mirroring
web_push.vapid_configured().

SECURITY NOTE: the service-account JSON can send push as this Firebase project
and must never be committed — supply it via environment only. Note also that
notification title/body transit Google's (and for iOS, Apple's) infrastructure,
so keep them to a short summary and put nothing sensitive beyond what the web
service worker already sends.
"""
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# firebase-admin requires exactly one app instance per process.
_app = None
_init_failed = False

# FCM's documented per-request maximum for send_each.
FCM_BATCH_LIMIT = 500
# Hard ceiling on devices we will ever fan out to in one notification. A single
# business event must not turn into an unbounded number of outbound requests.
MAX_TOKENS_PER_EVENT = 2000


def fcm_configured() -> bool:
    return bool(
        getattr(settings, 'FIREBASE_CREDENTIALS', '')
        or getattr(settings, 'FIREBASE_CREDENTIALS_JSON', '')
    )


def _get_app():
    """Lazily initialise the firebase-admin app. Returns None if unavailable."""
    global _app, _init_failed
    if _app is not None or _init_failed:
        return _app
    if not fcm_configured():
        _init_failed = True
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        logger.warning('firebase-admin not installed — mobile push skipped')
        _init_failed = True
        return None
    try:
        raw_json = getattr(settings, 'FIREBASE_CREDENTIALS_JSON', '')
        if raw_json:
            cred = credentials.Certificate(json.loads(raw_json))
        else:
            cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS)
        # Reuse an app another import may already have created.
        try:
            _app = firebase_admin.get_app()
        except ValueError:
            _app = firebase_admin.initialize_app(cred)
    except Exception as exc:
        # Never log the exception's payload at a level that could echo the key.
        logger.error('Firebase init failed: %s', type(exc).__name__)
        _init_failed = True
        return None
    return _app


def _build_message(messaging, token: str, title: str, body: str, data: dict):
    return messaging.Message(
        token=token,
        # A `notification` block is what makes the OS display this itself while
        # the app is backgrounded or killed — which is the whole point.
        notification=messaging.Notification(title=title, body=body),
        data=data,
        android=messaging.AndroidConfig(
            priority='high',
            notification=messaging.AndroidNotification(channel_id='default', sound='default'),
        ),
        apns=messaging.APNSConfig(
            headers={'apns-priority': '10'},
            payload=messaging.APNSPayload(aps=messaging.Aps(sound='default', content_available=True)),
        ),
    )


def _dispatch(messaging, devices, title: str, body: str, data: dict) -> int:
    """Send to a list of FcmDevice rows, pruning any the service rejects."""
    from core.models import FcmDevice

    sent = 0
    stale = []
    for start in range(0, len(devices), FCM_BATCH_LIMIT):
        chunk = devices[start:start + FCM_BATCH_LIMIT]
        messages = [_build_message(messaging, d.token, title, body, data) for d in chunk]
        try:
            # send_each reports per-message results, so one dead token cannot
            # fail the batch.
            batch = messaging.send_each(messages)
        except Exception as exc:
            logger.warning('FCM batch send failed: %s', exc)
            continue
        for device, result in zip(chunk, batch.responses):
            if result.success:
                sent += 1
                continue
            exc = result.exception
            name = type(exc).__name__ if exc else ''
            # App uninstalled, or the token was rotated / belongs to another
            # sender — drop the row rather than retrying it forever.
            if name in ('UnregisteredError', 'SenderIdMismatchError'):
                stale.append(device.id)
            else:
                logger.warning('FCM error for device %s: %s', device.id, name)

    if stale:
        FcmDevice.objects.filter(id__in=stale).delete()
    return sent


def _payload_parts(payload: dict):
    title = str(payload.get('title') or 'Truckwys')
    body = str(payload.get('message') or '')
    # Every FCM data value must be a string.
    data = {
        'link': str(payload.get('link') or ''),
        'type': str(payload.get('type') or 'info'),
        'event_id': str(payload.get('event_id') or ''),
    }
    return title, body, data


def send_fcm(user, payload: dict) -> int:
    """Push `payload` to every device registered for a single user."""
    if _get_app() is None:
        return 0
    from firebase_admin import messaging
    from core.models import FcmDevice

    devices = list(FcmDevice.objects.filter(user=user)[:FCM_BATCH_LIMIT])
    if not devices:
        return 0
    title, body, data = _payload_parts(payload)
    return _dispatch(messaging, devices, title, body, data)


def send_fcm_bulk(user_ids, payload: dict) -> int:
    """Push `payload` to every device of every user in `user_ids`.

    One query and one batched dispatch for the whole company, rather than a
    round-trip per user — `notify_company` runs inside the request/signal
    thread, so a company with hundreds of staff must not mean hundreds of
    sequential HTTP calls.
    """
    if _get_app() is None:
        return 0
    user_ids = list(user_ids)
    if not user_ids:
        return 0

    from firebase_admin import messaging
    from core.models import FcmDevice

    devices = list(
        FcmDevice.objects.filter(user_id__in=user_ids).only('id', 'token')[:MAX_TOKENS_PER_EVENT]
    )
    if not devices:
        return 0
    title, body, data = _payload_parts(payload)
    return _dispatch(messaging, devices, title, body, data)
