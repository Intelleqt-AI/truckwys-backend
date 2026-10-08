"""Outbound webhook delivery: tenant-scoped, after commit, never in the request.

Security model (WEBHOOK_SECURITY_FIX, Oct 2026):

* Every event belongs to ONE company. It is delivered only to that company's
  own targets — ``WebhookSubscription.company`` and the legacy ``Webhook``
  model's ``operator.company``. A target with no company, or an event with no
  company, delivers nothing (fail closed).
* Targets are resolved when the event happens (inside the request), but
  nothing is sent until the surrounding transaction commits: a rolled-back
  save never leaks, and the request never waits on a partner's endpoint.
* Delivery runs in a Celery task — one task per target, single HTTP attempt
  with a short timeout, bounded retries via ``self.retry(countdown=...)``.
  ``settings.WEBHOOK_DELIVERY_EAGER`` (tests/dev without a broker) sends one
  attempt inline in the on_commit hook instead; still no sleeps.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

logger = logging.getLogger(__name__)

# Target kinds
SUBSCRIPTION = 'subscription'   # core.WebhookSubscription (partner, HMAC X-Webhook-Signature)
LEGACY = 'legacy'               # core.Webhook (operator-owned, X-Truckwys-Signature)

# (connect, read) seconds. Short: a slow partner must not tie up a worker.
HTTP_TIMEOUT = (5, 10)
MAX_RETRIES = 3
RETRY_DELAYS = [30, 120, 600]   # seconds between attempts (Celery countdown)

# Delivery outcomes
OK = 'ok'
RETRY = 'retry'
FAILED = 'failed'
SKIPPED = 'skipped'

Payload = Union[Dict[str, Any], Callable[[], Dict[str, Any]]]


def _sign(body: str, secret: str) -> str:
    return 'sha256=' + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _wants(events, event_type: str) -> bool:
    # Membership in Python: JSONField __contains is Postgres-only.
    return event_type in (events or [])


def resolve_targets(event_type: str, company_id: Optional[int]) -> List[Tuple[str, int]]:
    """Every (kind, id) that may receive ``event_type`` for ``company_id``.

    Fail closed: no company -> nothing. Only targets owned by exactly that
    company are returned; company-less subscriptions never match."""
    if not company_id:
        return []
    from core.models import Webhook, WebhookSubscription

    targets: List[Tuple[str, int]] = []
    for sub_id, events in WebhookSubscription.objects.filter(
            is_active=True, company_id=company_id).values_list('id', 'events'):
        if _wants(events, event_type):
            targets.append((SUBSCRIPTION, sub_id))
    for hook_id, events in Webhook.objects.filter(
            active=True, operator__company_id=company_id).values_list('id', 'events'):
        if _wants(events, event_type):
            targets.append((LEGACY, hook_id))
    return targets


def build_body(event_type: str, data: Dict[str, Any]) -> str:
    return json.dumps({
        'event': event_type,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'data': data,
    }, cls=DjangoJSONEncoder)


def schedule(event_type: str, company_id: Optional[int], targets: List[Tuple[str, int]], body: str) -> None:
    """Queue one delivery per target once the current transaction commits."""
    if not targets:
        return
    jobs = [(kind, target_id, event_type, body, company_id) for kind, target_id in targets]

    def go():
        if getattr(settings, 'WEBHOOK_DELIVERY_EAGER', False):
            for job in jobs:
                try:
                    attempt(*job, final=True)
                except Exception:
                    logger.exception('webhook eager delivery failed (%s %s)', job[0], job[1])
            return
        from core.tasks import deliver_webhook
        for job in jobs:
            try:
                # retry=False: a dead broker fails fast instead of blocking the
                # process that committed the transaction.
                deliver_webhook.apply_async(args=list(job), retry=False)
            except Exception:
                logger.error('could not enqueue webhook %s for %s %s (broker unavailable); event dropped',
                             event_type, job[0], job[1])

    transaction.on_commit(go)


def dispatch(event_type: str, data: Payload, company_id: Optional[int]) -> int:
    """Deliver ``event_type`` to the event company's own targets (after commit).

    ``data`` may be a callable so callers skip serialising when nobody is
    subscribed. Returns the number of targets queued."""
    if not company_id:
        logger.debug('webhook %s has no company; not delivered (fail closed)', event_type)
        return 0
    targets = resolve_targets(event_type, company_id)
    if not targets:
        return 0
    payload = data() if callable(data) else data
    schedule(event_type, company_id, targets, build_body(event_type, payload))
    return len(targets)


def _load_target(kind: str, target_id: int):
    from core.models import Webhook, WebhookSubscription
    if kind == SUBSCRIPTION:
        sub = WebhookSubscription.objects.filter(pk=target_id).first()
        if sub is None:
            return None, None, False
        return sub, sub.company_id, sub.is_active
    if kind == LEGACY:
        hook = Webhook.objects.select_related('operator').filter(pk=target_id).first()
        if hook is None:
            return None, None, False
        return hook, getattr(hook.operator, 'company_id', None), hook.active
    return None, None, False


def _post(url: str, body: str, headers: Dict[str, str]):
    import requests
    return requests.post(url, data=body, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=False)


def attempt(kind: str, target_id: int, event_type: str, body: str, company_id: Optional[int],
            final: bool = True) -> str:
    """ONE HTTP attempt to one target. Never sleeps.

    Re-checks ownership at send time: if the target was deactivated, deleted
    or moved to another company since the event was queued, nothing is sent.
    Returns OK / RETRY / FAILED / SKIPPED. Failure counters are only bumped on
    the final attempt (or a non-retryable 4xx)."""
    target, target_company_id, active = _load_target(kind, target_id)
    if target is None or not active or not company_id or target_company_id != company_id:
        return SKIPPED

    if kind == SUBSCRIPTION:
        url = target.webhook_url
        headers = {
            'Content-Type': 'application/json',
            'X-Webhook-Signature': _sign(body, target.secret),
            'X-Webhook-Event': event_type,
            'User-Agent': 'TruckWys-Webhook/1.0',
        }
    else:
        url = target.url
        headers = {
            'Content-Type': 'application/json',
            'X-Truckwys-Signature': target.sign_payload(body),
            'X-Truckwys-Event': event_type,
            'User-Agent': 'Truckwys-Webhook/1.0',
        }

    try:
        response = _post(url, body, headers)
        code = response.status_code
    except Exception as exc:
        logger.warning('webhook %s to %s %s failed: %s', event_type, kind, target_id, exc)
        code = None

    if code is not None and 200 <= code < 300:
        _record(kind, target, success=True)
        return OK
    retryable = code is None or code >= 500 or code == 429
    if retryable and not final:
        return RETRY
    _record(kind, target, success=False)
    return FAILED


def _record(kind: str, target, success: bool) -> None:
    from django.utils import timezone
    if kind == SUBSCRIPTION:
        target.mark_delivery(success=success)
        return
    if success:
        target.failure_count = 0
        target.last_fired_at = timezone.now()
    else:
        target.failure_count += 1
        if target.failure_count >= 10:
            target.active = False
    target.save(update_fields=['failure_count', 'last_fired_at', 'active'])


class WebhookDeliveryService:
    """Back-compat facade over the module functions."""

    MAX_RETRIES = MAX_RETRIES
    RETRY_DELAYS = RETRY_DELAYS

    @staticmethod
    def _generate_signature(payload_json: str, secret: str) -> str:
        return _sign(payload_json, secret)

    @staticmethod
    def deliver_to_company(event_type: str, payload: Payload, company_id: Optional[int]) -> int:
        return dispatch(event_type, payload, company_id)
