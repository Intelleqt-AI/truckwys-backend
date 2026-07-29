"""Company notifications: persist + live-push in one call.

`notify_company` writes a Notification row for every active user in the company
(so it survives refresh and shows in the bell with history), broadcasts it over
the WebSocket so connected browsers update instantly, AND pushes it to every
registered mobile device via FCM so it arrives with the app closed. All three
halves are best-effort and never raise — a notification failure must never break
the business action that triggered it.
"""
import logging

logger = logging.getLogger(__name__)


def _push_allowed(user, event: str) -> bool:
    """Honour the user's per-category push preference when that module exists.

    `core.services.notification_prefs` landed on a later branch than this one.
    Until it is merged there is nothing to gate on, so push is allowed — the
    same default `should_notify` itself applies to unmapped push categories.
    """
    try:
        from core.services.notification_prefs import should_notify, EVENT_CATEGORY
    except ImportError:
        return True
    try:
        category = (EVENT_CATEGORY.get(event) or {}).get('push')
        if not category:
            return True
        return should_notify(user, 'push', category)
    except Exception:
        return True


def notify_company(company_id, ntype: str, title: str, message: str = '', link: str = '', event: str = 'notification'):
    if not company_id:
        return
    # 1) Persist one notification per active user in the company.
    recipients = []
    try:
        from core.models import Notification
        from core.models import User
        recipients = list(User.objects.filter(company_id=company_id, is_active=True))
        rows = [
            Notification(user=u, type=ntype, title=title, message=message, link=link)
            for u in recipients
        ]
        if rows:
            Notification.objects.bulk_create(rows)
    except Exception as exc:
        logger.warning('notify_company persist failed: %s', exc)

    # 2) Live push (drives the bell + toast + instant screen refresh).
    try:
        from core.ws.broadcast import broadcast_event
        broadcast_event(
            company_id,
            event,
            message=title,
            data={'title': title, 'message': message, 'link': link, 'type': ntype},
        )
    except Exception as exc:
        logger.warning('notify_company broadcast failed: %s', exc)

    # 3) Mobile push via FCM — the only channel that reaches a closed app.
    # Preferences are evaluated per user, but the send itself is one batched
    # dispatch: this runs inside the request/signal thread, so a large company
    # must not mean one HTTP round-trip per member.
    try:
        from core.services.fcm_push import send_fcm_bulk, fcm_configured
        if fcm_configured() and recipients:
            allowed = [u.id for u in recipients if _push_allowed(u, event)]
            if allowed:
                send_fcm_bulk(allowed, {
                    'title': title, 'message': message, 'link': link,
                    'type': ntype, 'event_id': event,
                })
    except Exception as exc:
        logger.warning('notify_company mobile push failed: %s', exc)
