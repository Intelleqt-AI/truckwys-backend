"""Company notifications: persist + live-push in one call.

`notify_company` writes a Notification row for every active user in the company
(so it survives refresh and shows in the bell with history) AND broadcasts it
over the WebSocket so connected browsers update instantly. Both halves are
best-effort and never raise — a notification failure must never break the
business action that triggered it.
"""
import logging

logger = logging.getLogger(__name__)


def notify_company(company_id, ntype: str, title: str, message: str = '', link: str = '', event: str = 'notification',
                    exclude_user_id=None):
    """exclude_user_id: pass the acting user's id so they don't get notified
    about their own action — everyone else in the company still does. Leave
    unset for events with no company-user actor (e.g. a customer accepting a
    public quote link), where the whole company should hear about it."""
    if not company_id:
        return
    # 1) Persist one notification per active user in the company (minus the actor).
    try:
        from core.models import Notification
        from core.models import User
        users = User.objects.filter(company_id=company_id, is_active=True)
        if exclude_user_id:
            users = users.exclude(id=exclude_user_id)
        rows = [
            Notification(user=u, type=ntype, title=title, message=message, link=link)
            for u in users
        ]
        if rows:
            Notification.objects.bulk_create(rows)
    except Exception as exc:
        logger.warning('notify_company persist failed: %s', exc)

    # 2) Live push (drives the bell + toast + instant screen refresh). The
    # group is company-wide (every connected browser, actor included), so the
    # actor id rides along in the payload and the frontend self-suppresses.
    try:
        from core.ws.broadcast import broadcast_event
        broadcast_event(
            company_id,
            event,
            message=title,
            data={'title': title, 'message': message, 'link': link, 'type': ntype, 'actor_id': exclude_user_id},
        )
    except Exception as exc:
        logger.warning('notify_company broadcast failed: %s', exc)
