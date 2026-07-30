"""Company notifications: persist + deliver in one call.

`notify_company` writes a Notification row for every active user in the company
(so it survives refresh and shows in the bell with history), broadcasts it over
the WebSocket so connected browsers update instantly, AND pushes it to every
registered mobile device via FCM so it arrives with the app closed. All three
halves are best-effort and never raise — a notification failure must never break
the business action that triggered it.
"""
import logging
import uuid

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


def notify_company(company_id, ntype: str, title: str, message: str = '', link: str = '',
                    event: str = 'notification', exclude_user_id=None):
    if not company_id:
        return
    # 1) Persist one notification per active user in the company — except the
    # user who caused it, if the caller identifies one (nobody needs to be
    # told about their own action). The exclusion carries through to the FCM
    # step below via `recipients`.
    recipients = []
    try:
        from core.models import Notification
        from core.models import User
        qs = User.objects.filter(company_id=company_id, is_active=True)
        if exclude_user_id:
            qs = qs.exclude(id=exclude_user_id)
        recipients = list(qs)
        rows = [
            Notification(user=u, type=ntype, title=title, message=message, link=link)
            for u in recipients
        ]
        if rows:
            Notification.objects.bulk_create(rows)
    except Exception as exc:
        logger.warning('notify_company persist failed: %s', exc)

    # 2) Live push (drives the bell + toast + instant screen refresh). The
    # group is company-wide (every connected browser, actor included), so the
    # actor id and category ride along and the frontend self-suppresses.
    try:
        from core.ws.broadcast import broadcast_event
        try:
            from core.services.notification_prefs import category_for
            push_cat = category_for(event, 'push')
        except ImportError:
            push_cat = None
        broadcast_event(
            company_id,
            event,
            message=title,
            data={'title': title, 'message': message, 'link': link, 'type': ntype,
                  'actor_id': exclude_user_id, 'category': push_cat,
                  'event_id': str(uuid.uuid4())},
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
                from core.services.notify_copy import channel_for
                send_fcm_bulk(allowed, {
                    'title': title, 'message': message, 'link': link,
                    'type': ntype, 'event_id': event, 'channel': channel_for(event),
                })
    except Exception as exc:
        logger.warning('notify_company mobile push failed: %s', exc)
