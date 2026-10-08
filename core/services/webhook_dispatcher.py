"""Outbound event notifications — the single entry point for firing webhooks.

Every event is scoped to the company that owns the object. Delivery goes only
to that company's own WebhookSubscriptions and legacy Webhooks, after the
transaction commits, from a Celery task (see core/services/webhook_delivery.py).
An event without a company is delivered to nobody.
"""

from typing import Optional

from core.services.webhook_delivery import LEGACY, Payload, build_body, dispatch, schedule


def dispatch_webhook(event_type: str, data: Payload, company_id: Optional[int] = None) -> int:
    """Queue ``event_type`` for the event company's own subscribers.

    ``company_id`` is the company that owns the event's object. None means
    nobody receives it (fail closed) — callers must always pass it.
    Never raises and never performs HTTP in the caller's thread."""
    try:
        return dispatch(event_type, data, company_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('webhook dispatch failed for %s', event_type)
        return 0


def dispatch_to_legacy_webhook(webhook, event_type: str, data: dict) -> None:
    """Queue one event to ONE legacy Webhook (used by its "test ping")."""
    company_id = getattr(webhook.operator, 'company_id', None)
    if not company_id:
        return
    schedule(event_type, company_id, [(LEGACY, webhook.id)], build_body(event_type, data))
