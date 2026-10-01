"""Pulling payments back: webhooks (fast path) and the hourly poll (catch-up).

Webhook endpoint (views) only verifies the signature and stores the events
(AccountingWebhookEvent); process_webhook_events() does the provider calls.
The poll asks for everything changed since its cursor (Xero:
If-Modified-Since; QBO: CDC) with a 5-minute overlap, finds the invoices
affected, and mirrors each one (core.accounting.settlements). Mirroring is
idempotent, so overlap and duplicate events are harmless.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.accounting.base import AuthError, NotFound, RateLimited, TransientError
from core.accounting.events import log_event
from core.accounting.registry import get_adapter
from core.accounting.settlements import PREFIX, mirror_invoice, refresh_overpayment_remainders

logger = logging.getLogger(__name__)
OVERLAP = timedelta(minutes=5)


def _cursor(connection, name):
    raw = (connection.cursors or {}).get(name)
    if raw:
        return datetime.fromisoformat(raw) - OVERLAP
    cut = connection.cutover_date
    if cut:
        return timezone.make_aware(datetime.combine(cut, time.min))
    return None


def _set_cursor(connection, name, when):
    cursors = dict(connection.cursors or {})
    cursors[name] = when.isoformat()
    connection.cursors = cursors


def invoices_for_changes(connection, changes):
    """Linked TruckWys invoices touched by a list of RemotePaymentChange."""
    from core.models import ExternalLink, Invoice, Payment
    ext_ids = {c.invoice_external_id for c in changes if c.invoice_external_id}
    local_ids = set()
    for c in changes:
        if c.kind in PREFIX and c.source_id:
            # An allocation may have been removed: find invoices that had one.
            local_ids |= set(Payment.objects.filter(
                company_id=connection.company_id, source=connection.provider,
                external_id__startswith=f'{PREFIX[c.kind]}:{c.source_id}:').values_list('invoice_id', flat=True))
        if c.kind == 'PAYMENT' and c.status == 'DELETED' and c.external_id:
            # Providers that can pay several invoices with one payment store
            # '<payment id>:<invoice id>' per invoice (QBO); a deletion may not
            # say which invoices it touched.
            from django.db.models import Q
            local_ids |= set(Payment.objects.filter(company_id=connection.company_id, source=connection.provider)
                             .filter(Q(external_id=c.external_id) | Q(external_id__startswith=f'{c.external_id}:'))
                             .values_list('invoice_id', flat=True))
    links = ExternalLink.objects.filter(connection=connection, object_type='INVOICE')
    by_ext = dict(links.filter(external_id__in=ext_ids).values_list('local_id', 'external_id'))
    by_local = dict(links.filter(local_id__in=local_ids).values_list('local_id', 'external_id'))
    pairs = {**by_ext, **by_local}
    # Invoices from before the cut-over (linked only so a credit note could be
    # allocated) stay managed in TruckWys: never mirrored. By date, always.
    invoices = Invoice.objects.filter(pk__in=pairs.keys(), company_id=connection.company_id,
                                      issue_date__gte=connection.cutover_date)
    return [(inv, pairs[inv.pk]) for inv in invoices if pairs.get(inv.pk)]


def mirror_external_invoice(connection, invoice, external_id, adapter):
    try:
        state = adapter.get_invoice_state(external_id)
    except NotFound:
        log_event(connection, 'pull_payments',
                  f'{invoice.invoice_number} no longer exists in {connection.get_provider_display()}',
                  level='ERROR', object_type='INVOICE', local_id=invoice.pk, label=invoice.invoice_number)
        return None
    counts = mirror_invoice(connection, invoice, state, adapter=adapter)
    if counts and (counts['created'] or counts['removed'] or counts['updated']):
        # An allocation moved money off an overpayment we hold as a remainder
        # on another invoice: settle that now, not at the next hourly poll.
        refresh_overpayment_remainders(connection, adapter=adapter)
    return counts


def poll_payments(connection) -> dict:
    """Everything that changed since the cursor. Raises RateLimited /
    TransientError for the task to retry (the cursor only moves on success)."""
    from core.models import AccountingConnection
    if connection.status != AccountingConnection.ACTIVE or not connection.cutover_date:
        return {'skipped': True}
    adapter = get_adapter(connection)
    started = timezone.now()
    try:
        since = _cursor(connection, 'payments')
        changes = adapter.list_payments_since(since)
        changes += adapter.list_credit_note_allocations(since)
        touched = invoices_for_changes(connection, changes)
        totals = {'invoices': 0, 'created': 0, 'updated': 0, 'removed': 0, 'credit_notes': 0}
        for invoice, ext in touched:
            counts = mirror_external_invoice(connection, invoice, ext, adapter)
            if counts:
                totals['invoices'] += 1
                for k in ('created', 'updated', 'removed', 'credit_notes'):
                    totals[k] += counts[k]
        refresh_overpayment_remainders(connection, adapter=adapter)
    except AuthError as exc:
        from core.accounting.tokens import mark_needs_reauth
        mark_needs_reauth(connection, str(exc))
        raise
    _set_cursor(connection, 'payments', started)
    connection.last_payment_sync_at = started
    connection.save(update_fields=['cursors', 'last_payment_sync_at', 'updated_at'])
    if totals['created'] or totals['updated'] or totals['removed'] or totals['credit_notes']:
        log_event(connection, 'pull_payments',
                  f'Payments synced: {totals["created"]} new, {totals["updated"]} changed, '
                  f'{totals["removed"]} removed across {totals["invoices"]} invoices')
    return totals


# ---------------------------------------------------------------- webhooks

def store_webhook_events(provider, events: list[dict]) -> int:
    """events: [{tenant_id, resource_type, resource_id, event_type, event_at, dedupe_key, payload}]"""
    from core.models import AccountingWebhookEvent
    stored = 0
    for e in events:
        try:
            with transaction.atomic():
                AccountingWebhookEvent.objects.create(provider=provider, **e)
            stored += 1
        except IntegrityError:
            continue   # duplicate delivery
    if stored:
        _schedule_processing()
    return stored


def _schedule_processing():
    from django.conf import settings

    def go():
        if getattr(settings, 'ACCOUNTING_SYNC_EAGER', False):
            process_webhook_events()
            return
        try:
            from core.accounting.tasks import process_webhooks
            process_webhooks.delay()
        except Exception:
            logger.warning('could not enqueue webhook processing; the sweeper will run it')
    transaction.on_commit(go)


def process_webhook_events(limit=500) -> int:
    from core.models import AccountingConnection, AccountingWebhookEvent
    events = list(AccountingWebhookEvent.objects.filter(processed_at__isnull=True).order_by('received_at')[:limit])
    done = 0
    skip_tenants = set()
    for ev in events:
        if (ev.provider, ev.tenant_id) in skip_tenants:
            continue
        conn = AccountingConnection.objects.filter(provider=ev.provider, tenant_id=ev.tenant_id,
                                                   status=AccountingConnection.ACTIVE).first()
        if conn is None:
            ev.processed_at, ev.error = timezone.now(), 'No active connection for this organisation'
            ev.save(update_fields=['processed_at', 'error'])
            continue
        try:
            handled = _handle_event(conn, ev)
        except (RateLimited, TransientError) as exc:
            ev.attempts += 1
            ev.error = str(exc)[:2000]
            ev.save(update_fields=['attempts', 'error'])
            skip_tenants.add((ev.provider, ev.tenant_id))
            continue
        except AuthError as exc:
            from core.accounting.tokens import mark_needs_reauth
            mark_needs_reauth(conn, str(exc))
            skip_tenants.add((ev.provider, ev.tenant_id))
            continue
        except Exception as exc:
            logger.exception('webhook event %s failed', ev.pk)
            ev.attempts += 1
            ev.error = f'Unexpected: {exc}'[:2000]
            if ev.attempts >= 5:
                ev.processed_at = timezone.now()
            ev.save(update_fields=['attempts', 'error', 'processed_at'])
            continue
        ev.processed_at = timezone.now()
        ev.error = '' if handled else 'Not relevant to TruckWys'
        ev.save(update_fields=['processed_at', 'error'])
        done += 1
    return done


def _handle_event(conn, ev) -> bool:
    """Xero: INVOICE events carry the InvoiceID. QBO: Payment / Invoice /
    CreditMemo entities; a Payment event is resolved to its invoices by the
    adapter (core.accounting.quickbooks)."""
    from core.models import ExternalLink, Invoice
    adapter = get_adapter(conn)
    rtype = ev.resource_type.upper()
    invoice_ext_ids = []
    if rtype in ('INVOICE', 'INVOICES'):
        invoice_ext_ids = [ev.resource_id]
    elif hasattr(adapter, 'invoices_for_event'):
        invoice_ext_ids = adapter.invoices_for_event(rtype, ev.resource_id, ev.event_type)
    if not invoice_ext_ids:
        return False
    links = dict(ExternalLink.objects.filter(connection=conn, object_type='INVOICE', external_id__in=invoice_ext_ids)
                 .values_list('local_id', 'external_id'))
    if not links:
        return False   # a bill, or an invoice TruckWys didn't issue
    for inv in Invoice.objects.filter(pk__in=links.keys(), company_id=conn.company_id,
                                      issue_date__gte=conn.cutover_date):
        mirror_external_invoice(conn, inv, links[inv.pk], adapter)
    return True
