"""Activity / error log for the accounting sync UI."""
import logging

logger = logging.getLogger(__name__)


def log_event(connection, action, message, *, level='INFO', object_type='', local_id=None, label=''):
    from core.models import AccountingSyncEvent
    try:
        return AccountingSyncEvent.objects.create(
            company_id=connection.company_id, connection=connection, level=level, action=action[:40],
            object_type=object_type or '', local_id=local_id, label=(label or '')[:120], message=str(message)[:4000])
    except Exception:  # the log must never break a sync
        logger.exception('could not write accounting sync event')
        return None
