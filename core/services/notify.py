"""Company notifications: persist + deliver in one call.

`notify_company` writes a Notification row for every active user in the
company (the bell keeps a complete history and survives refresh) AND delivers
per user preference (core/services/notification_prefs.py):

  - WebSocket broadcast → in-app toast; company-wide with a `category` field
    so the frontend suppresses toasts for categories the viewer disabled.
  - Browser push (Web Push, if the user has subscriptions and the category's
    push toggle is on).
  - Notification email (generic branded template, if the category's email
    toggle is on).

Every half is best-effort and never raises — a notification failure must
never break the business action that triggered it.
"""
import logging
import uuid

logger = logging.getLogger(__name__)


def notify_company(company_id, ntype: str, title: str, message: str = '', link: str = '', event: str = 'notification',
                    exclude_user_id=None, category=None):
    """exclude_user_id: pass the acting user's id so they don't get notified
    about their own action — everyone else in the company still does. Leave
    unset for events with no company-user actor (e.g. a customer accepting a
    public quote link), where the whole company should hear about it.

    category: optional explicit category override; normally derived from
    `event` via notification_prefs.EVENT_CATEGORY.
    """
    if not company_id:
        return

    from core.services.notification_prefs import category_for, should_notify
    email_cat = category or category_for(event, 'email')
    push_cat = category or category_for(event, 'push')

    recipients = []
    # 1) Persist one notification per active user in the company (minus the actor).
    try:
        from core.models import Notification
        from core.models import User
        users = User.objects.filter(company_id=company_id, is_active=True)
        if exclude_user_id:
            users = users.exclude(id=exclude_user_id)
        recipients = list(users)
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

    # 3) Browser push — server-gated per user preference; delivers with the
    # tab or browser closed via the service worker.
    if push_cat:
        try:
            from core.services.web_push import send_web_push, vapid_configured
            if vapid_configured():
                payload = {'title': title, 'message': message, 'link': link, 'type': ntype}
                for u in recipients:
                    if should_notify(u, 'push', push_cat):
                        send_web_push(u, payload)
        except Exception as exc:
            logger.warning('notify_company web push failed: %s', exc)

    # 4) Notification email — only for events with a mapped email category,
    # only to users who have that category enabled.
    if email_cat:
        try:
            from core.services.email_service import send_notification_email
            for u in recipients:
                if should_notify(u, 'email', email_cat):
                    send_notification_email(u, title, message, link)
        except Exception as exc:
            logger.warning('notify_company email failed: %s', exc)
