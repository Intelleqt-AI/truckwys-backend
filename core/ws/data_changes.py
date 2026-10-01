"""Live "data changed" pushes: the backend half of instant screen updates.

Every save or delete of a record a screen shows (invoices, payments,
expenses, quotes, loads, fleet, customers, ...) sends one small
`data.changed` event to that company's WebSocket group after the
transaction commits. The frontend (components/LiveEvents.tsx) refetches only
the data that topic feeds, so Home, Finance and the lists update within a
second without polling.

Unlike notify_company() this creates no notification, bell entry, email or
push: it only says "this kind of data changed". Changes made inside one
transaction (e.g. a payment and the invoice it settles) go out as one event.
A failed push never affects the save (broadcast_event swallows errors).
"""
import logging
import threading

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save

logger = logging.getLogger(__name__)

# model name -> topic the frontend maps to its caches.
TOPICS = {
    'Invoice': 'invoice',
    'Payment': 'payment',
    'Expense': 'expense',
    'Quote': 'quote',
    'Load': 'load',
    'Trip': 'trip',
    'Vehicle': 'vehicle',
    'Driver': 'driver',
    'Customer': 'customer',
    'AdvanceRequest': 'advance',
}
MAX_IDS = 20  # per topic per event; enough for detail pages to match theirs

_pending = threading.local()


def _company_id(instance):
    company_id = getattr(instance, 'company_id', None)
    if company_id:
        return company_id
    # Rows without their own company: take it from what they belong to.
    for parent in ('load', 'invoice'):
        parent_id = getattr(instance, f'{parent}_id', None)
        if parent_id:
            try:
                model = instance._meta.get_field(parent).related_model
                return model.objects.filter(pk=parent_id).values_list('company_id', flat=True).first()
            except Exception:
                return None
    return None


def _send(batch):
    from core.ws.broadcast import broadcast_event
    for company_id, topics in batch.items():
        broadcast_event(company_id, 'data.changed', data={
            'topics': sorted(topics),
            'ids': {t: sorted(ids)[:MAX_IDS] for t, ids in topics.items()},
        })


def _flush():
    batch = getattr(_pending, 'batch', None) or {}
    _pending.batch = None
    _send(batch)


def _record(sender, instance, **kwargs):
    if kwargs.get('raw') or not getattr(settings, 'LIVE_DATA_EVENTS', True):
        return
    topic = TOPICS.get(sender.__name__)
    if not topic:
        return
    try:
        company_id = _company_id(instance)
        if not company_id:
            return
        conn = transaction.get_connection()
        if not conn.in_atomic_block:
            _send({company_id: {topic: {instance.pk}}})
            return
        # Inside a transaction: batch until it commits, one event for all of
        # it. A batch whose flush is no longer queued belonged to a
        # transaction that rolled back, so it's dropped, never sent.
        scheduled = any(entry[1] is _flush for entry in conn.run_on_commit)
        if not scheduled:
            _pending.batch = {}
        _pending.batch.setdefault(company_id, {}).setdefault(topic, set()).add(instance.pk)
        if not scheduled:
            transaction.on_commit(_flush)
    except Exception as exc:  # never break a save over a live push
        logger.warning('data.changed record failed for %s: %s', sender.__name__, exc)
        _pending.batch = None


def connect():
    from django.apps import apps
    for name in TOPICS:
        model = apps.get_model('core', name)
        post_save.connect(_record, sender=model, dispatch_uid=f'data_changed_save_{name}')
        post_delete.connect(_record, sender=model, dispatch_uid=f'data_changed_delete_{name}')
