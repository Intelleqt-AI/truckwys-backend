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
    except Exception as exc:  # never break the request because a push failed
        logger.warning('broadcast_event failed: %s', exc)
