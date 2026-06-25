"""Push a real-time event to a company's connected browsers.

Safe to call from synchronous view code; a no-op (never raises) when the channel
layer isn't configured, so the rest of the request path is never affected.
"""
import logging

logger = logging.getLogger(__name__)


def broadcast_event(company_id, event_type: str, message: str = '', data: dict | None = None):
    if not company_id:
        return
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer is None:
            return
        payload = {
            'type': 'event',
            'event': event_type,
            'message': message,
            'data': data or {},
        }
        async_to_sync(layer.group_send)(
            f'company_{company_id}',
            {'type': 'app.event', 'payload': payload},
        )
        logger.debug('broadcast_event OK: company=%s event=%s', company_id, event_type)
    except Exception as exc:  # never break the request because a push failed
        logger.error('broadcast_event FAILED company=%s event=%s: %s', company_id, event_type, exc, exc_info=True)
