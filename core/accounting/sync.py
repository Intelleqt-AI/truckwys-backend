"""Pushing TruckWys documents to the connected accounting system.

Every push is one ExternalLink run by run_link():

  * claimed atomically (PENDING/ERROR/BLOCKED -> RUNNING), so two workers
    never push the same document;
  * created UNPOSTED (Xero DRAFT), its totals compared with TruckWys to the
    cent, and only then posted (finalise_document). A mismatch never reaches
    the ledger: the draft is discarded and the link goes DEAD with the
    difference spelled out;
  * the provider id is saved the moment it exists, plus an Idempotency-Key
    per payload, so a retry after a timeout can't create a duplicate;
  * before creating, the provider is searched for the same document number
    (an accountant may already have typed it in): equal totals -> linked,
    different totals -> DEAD (a human decides).

Failure policy:
  Blocked / ContactBlocked  -> BLOCKED  (waits for mapping / contact confirmation;
                                          re-queued when those change)
  AuthError                 -> PENDING  (connection -> NEEDS_REAUTH; resumes on reconnect)
  RateLimited               -> ERROR, next attempt after Retry-After (not counted)
  TransientError / bug      -> ERROR, exponential backoff, DEAD after MAX_ATTEMPTS
  PermanentError            -> DEAD     (shown in the error list with Retry)
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.accounting import documents
from core.accounting.base import AuthError, NotFound, PermanentError, RateLimited, TransientError
from core.accounting.contacts import ContactBlocked, ensure_contact
from core.accounting.documents import Blocked, payload_hash
from core.accounting.events import log_event
from core.accounting.registry import get_adapter

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
STALE_RUNNING = timedelta(minutes=15)


class RetryLater(TransientError):
    """A dependency (e.g. the invoice a credit note allocates to) isn't synced yet."""


# ---------------------------------------------------------------- connection helpers

def active_connection(company):
    from core.models import AccountingConnection
    if company is None:
        return None
    return (AccountingConnection.objects.filter(company=company, status=AccountingConnection.ACTIVE)
            .order_by('-connected_at').first())


def live_connection(company):
    """Active or waiting for re-auth: documents still queue (they push once
    reconnected) and payments stay managed by the provider."""
    from core.models import AccountingConnection
    if company is None:
        return None
    return (AccountingConnection.objects.filter(
        company=company, status__in=(AccountingConnection.ACTIVE, AccountingConnection.NEEDS_REAUTH))
        .order_by('-connected_at').first())


def blocking_reasons(connection) -> list[str]:
    from core.accounting import mapping
    reasons = []
    if connection.status != 'ACTIVE':
        reasons.append({'PENDING_ORG': 'Choose which organisation to connect',
                        'NEEDS_REAUTH': f'Reconnect {connection.get_provider_display()}',
                        'DISABLED': 'Disconnected'}.get(connection.status, connection.status))
    if not mapping.is_complete(connection):
        reasons.append('Map every revenue type, expense category and tax code')
    if not connection.cutover_date:
        reasons.append('Choose a cut-over date and run the initial sync')
    return reasons


def sync_enabled(connection) -> bool:
    return connection is not None and not blocking_reasons(connection)


def in_scope(connection, on_date) -> bool:
    cut = connection.cutover_date
    return bool(cut and on_date and on_date >= cut)


# ---------------------------------------------------------------- queueing

def get_or_create_link(connection, object_type, local_id):
    from core.models import ExternalLink
    link, _ = ExternalLink.objects.get_or_create(
        connection=connection, object_type=object_type, local_id=local_id,
        defaults={'company_id': connection.company_id, 'provider': connection.provider, 'status': 'PENDING'})
    return link


def enqueue(connection, object_type, local_id, *, force=False):
    """Mark the document for push and schedule it after the current
    transaction commits. Safe to call repeatedly."""
    from core.models import ExternalLink
    link = get_or_create_link(connection, object_type, local_id)
    # A worker holds it: tell that worker to run it again when it finishes,
    # so a void / edit made meanwhile is never lost.
    if ExternalLink.objects.filter(pk=link.pk, status='RUNNING').update(requeue=True):
        return link
    if link.status in ('SYNCED', 'VOIDED', 'DEAD', 'BLOCKED', 'ERROR') or force:
        ExternalLink.objects.filter(pk=link.pk).exclude(status='RUNNING').update(
            status='PENDING', next_attempt_at=None, updated_at=timezone.now())
    _schedule(link.pk)
    return link


def _schedule(link_id):
    def go():
        if getattr(settings, 'ACCOUNTING_SYNC_EAGER', False):
            run_link(link_id)
            return
        try:
            from core.accounting.tasks import push_link
            push_link.delay(link_id)
        except Exception:
            # Broker down: the link stays PENDING and retry_due() picks it up.
            logger.warning('could not enqueue accounting push %s; the sweeper will retry', link_id)
    transaction.on_commit(go)


def requeue_blocked(connection):
    from core.models import ExternalLink
    ids = list(ExternalLink.objects.filter(connection=connection, status='BLOCKED')
               .exclude(object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER')).values_list('pk', flat=True))
    ExternalLink.objects.filter(pk__in=ids).update(status='PENDING', updated_at=timezone.now())
    for i in ids:
        _schedule(i)
    return len(ids)


def backoff_seconds(attempts: int) -> float:
    base = min(30 * (2 ** max(0, attempts - 1)), 6 * 3600)
    return base * (0.8 + random.random() * 0.4)


# ---------------------------------------------------------------- runner

def _claim(link_id):
    from core.models import ExternalLink
    now = timezone.now()
    claimed = ExternalLink.objects.filter(pk=link_id, status__in=('PENDING', 'ERROR', 'BLOCKED')).update(
        status='RUNNING', requeue=False, updated_at=now)
    if not claimed:
        # A RUNNING link whose worker died is re-claimable after STALE_RUNNING.
        claimed = ExternalLink.objects.filter(pk=link_id, status='RUNNING',
                                              updated_at__lt=now - STALE_RUNNING).update(requeue=False, updated_at=now)
    if not claimed:
        return None
    return ExternalLink.objects.select_related('connection', 'connection__company').get(pk=link_id)


FINISH_FIELDS = ['status', 'last_error', 'attempts', 'next_attempt_at', 'external_id', 'external_number',
                 'external_version', 'last_hash', 'last_synced_at', 'meta', 'updated_at']


def _finish(link, status, *, error='', attempts=None, next_at=None, **fields):
    """Save the outcome. Never writes `requeue` (a concurrent enqueue may
    have just set it; run_link reads it after this)."""
    link.status = status
    link.last_error = (error or '')[:4000]
    if attempts is not None:
        link.attempts = attempts
    link.next_attempt_at = next_at
    for k, v in fields.items():
        setattr(link, k, v)
    link.save(update_fields=FINISH_FIELDS)


HANDLERS = {}


def handler(object_type):
    def deco(fn):
        HANDLERS[object_type] = fn
        return fn
    return deco


def run_link(link_id) -> str | None:
    """Push one link. Returns the resulting status (None if not claimable)."""
    link = _claim(link_id)
    if link is None:
        return None
    connection = link.connection
    if connection.status != 'ACTIVE':
        _finish(link, 'PENDING', error=f'Waiting: {connection.get_status_display()}')
        return 'PENDING'
    fn = HANDLERS.get(link.object_type)
    if fn is None:
        _finish(link, 'DEAD', error=f'No push handler for {link.object_type}')
        return 'DEAD'
    try:
        adapter = get_adapter(connection)
        fn(connection, adapter, link)
    except (Blocked, ContactBlocked) as exc:
        _finish(link, 'BLOCKED', error=str(exc))
        log_event(connection, f'push_{link.object_type.lower()}', str(exc), level='WARNING',
                  object_type=link.object_type, local_id=link.local_id, label=_label(link))
    except AuthError as exc:
        from core.accounting.tokens import mark_needs_reauth
        mark_needs_reauth(connection, str(exc))
        _finish(link, 'PENDING', error=str(exc))
    except RateLimited as exc:
        _finish(link, 'ERROR', error=str(exc), next_at=timezone.now() + timedelta(seconds=exc.retry_after))
    except TransientError as exc:
        attempts = link.attempts + (1 if getattr(exc, 'counts', True) else 0)
        if attempts >= MAX_ATTEMPTS:
            _finish(link, 'DEAD', error=f'Gave up after {attempts} attempts: {exc}', attempts=attempts)
            log_event(connection, f'push_{link.object_type.lower()}', f'Gave up: {exc}', level='ERROR',
                      object_type=link.object_type, local_id=link.local_id, label=_label(link))
        else:
            # Never shorter than the backoff: a timeout's own hint (30 s)
            # must not turn eight attempts into four minutes.
            wait = max(exc.retry_after or 0, backoff_seconds(max(attempts, 1)))
            _finish(link, 'ERROR', error=str(exc), attempts=attempts,
                    next_at=timezone.now() + timedelta(seconds=wait))
    except PermanentError as exc:
        _finish(link, 'DEAD', error=str(exc), attempts=link.attempts + 1)
        log_event(connection, f'push_{link.object_type.lower()}', str(exc), level='ERROR',
                  object_type=link.object_type, local_id=link.local_id, label=_label(link))
    except Exception as exc:  # a bug: keep it visible, retry a few times
        logger.exception('accounting push failed for link %s', link.pk)
        attempts = link.attempts + 1
        status = 'DEAD' if attempts >= MAX_ATTEMPTS else 'ERROR'
        _finish(link, status, error=f'Unexpected error: {exc}', attempts=attempts,
                next_at=None if status == 'DEAD' else timezone.now() + timedelta(seconds=backoff_seconds(attempts)))
        log_event(connection, f'push_{link.object_type.lower()}', f'Unexpected error: {exc}', level='ERROR',
                  object_type=link.object_type, local_id=link.local_id, label=_label(link))
    from core.models import ExternalLink
    if ExternalLink.objects.filter(pk=link.pk, requeue=True).update(
            requeue=False, status='PENDING', next_attempt_at=None, updated_at=timezone.now()):
        _schedule(link.pk)
        return 'PENDING'
    return link.status


def retry_due(limit=200) -> int:
    """Sweeper: ERROR links whose time has come, PENDING links nobody ran
    (broker hiccup), RUNNING links whose worker died."""
    from django.db.models import Q
    from core.models import ExternalLink
    now = timezone.now()
    qs = (ExternalLink.objects.filter(connection__status='ACTIVE')
          .filter(Q(status='ERROR', next_attempt_at__lte=now) |
                  Q(status='PENDING', updated_at__lt=now - timedelta(minutes=2)) |
                  Q(status='RUNNING', updated_at__lt=now - STALE_RUNNING))
          .exclude(object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER'))
          .order_by('next_attempt_at', 'updated_at').values_list('pk', flat=True)[:limit])
    n = 0
    for pk in list(qs):
        if run_link(pk) is not None:
            n += 1
    return n


def _label(link):
    try:
        if link.object_type == 'INVOICE':
            from core.models import Invoice
            return Invoice.objects.filter(pk=link.local_id).values_list('invoice_number', flat=True).first() or ''
        if link.object_type == 'CREDIT_NOTE':
            from core.models import CreditNote
            return CreditNote.objects.filter(pk=link.local_id).values_list('credit_note_number', flat=True).first() or ''
        if link.object_type == 'BILL':
            from core.models import Expense
            return Expense.objects.filter(pk=link.local_id).values_list('expense_number', flat=True).first() or ''
    except Exception:
        pass
    return f'{link.object_type} {link.local_id}'


# ---------------------------------------------------------------- shared push steps

def _key(link, h):
    """Idempotency key for a create. A verified-and-discarded attempt bumps
    meta['tries'] so a retry after the cause is fixed is a NEW request, not a
    replay of the discarded one."""
    tries = int((link.meta or {}).get('tries', 0))
    return f'tw-{link.company_id}-{link.object_type}-{link.local_id}-{h[:24]}-{tries}'


def _bump_tries(link):
    link.meta = {**(link.meta or {}), 'tries': int((link.meta or {}).get('tries', 0)) + 1}
    link.save(update_fields=['meta', 'updated_at'])


def _number_kept(adapter, doc, res) -> bool:
    """The provider kept TruckWys' number (QBO renumbers when custom
    transaction numbers are off; bills carry the supplier's number)."""
    if doc.kind == 'BILL' or not res.external_number:
        return True
    want = doc.number[:adapter.max_number_length] if adapter.max_number_length else doc.number
    return res.external_number == want


def totals_diff(doc, res) -> list[str]:
    out = []
    for label, ours, theirs in (('subtotal', doc.sub_total, res.sub_total), ('VAT', doc.total_tax, res.total_tax),
                                ('total', doc.total, res.total)):
        if theirs is not None and Decimal(ours) != Decimal(theirs):
            out.append(f'{label} TruckWys {ours} vs {theirs}')
    return out


def _save_external(link, res):
    from django.db import IntegrityError
    link.external_id = res.external_id
    if res.external_number:
        link.external_number = res.external_number
    if res.version:
        link.external_version = res.version
    try:
        with transaction.atomic():
            link.save(update_fields=['external_id', 'external_number', 'external_version', 'updated_at'])
    except IntegrityError:
        link.external_id = ''
        raise PermanentError(f'{res.external_number or res.external_id} is already linked to another TruckWys '
                             'document (same number twice?). Check for a duplicate.')


def push_document(connection, adapter, link, doc, *, kind):
    """Create-or-reuse, verify to the cent, post. Returns the PushResult."""
    h = payload_hash(doc)
    provider = connection.get_provider_display()
    if link.external_id:
        try:
            current = adapter.get_document(kind, link.external_id)
        except NotFound:
            current = None
            link.external_id = ''
        if current is not None:
            if current.status in ('VOIDED', 'DELETED'):
                raise PermanentError(f'This document was voided/deleted in {provider} but is live in TruckWys. '
                                     'Correct it in TruckWys (credit note or void) or restore it in '
                                     f'{provider}, then retry.')
            if current.status not in ('DRAFT', 'SUBMITTED'):
                diff = totals_diff(doc, current)
                if diff:
                    raise PermanentError(f'{provider} has different totals: ' + '; '.join(diff))
                return current, h
    else:
        existing = adapter.find_document(kind, doc.number, contact_id=doc.contact_id if kind == 'BILL' else '')
        if existing is not None:
            diff = totals_diff(doc, existing)
            if diff:
                raise PermanentError(f'{provider} already has {doc.number} with different totals ('
                                     + '; '.join(diff) + '). Fix or rename it there, then retry.')
            _save_external(link, existing)
            link.meta = {**(link.meta or {}), 'matched_existing': True}
            if existing.status in ('DRAFT', 'SUBMITTED'):
                return adapter.finalise_document(kind, existing.external_id), h
            return existing, h

    pusher = {'INVOICE': adapter.push_invoice, 'CREDIT_NOTE': adapter.push_credit_note,
              'BILL': adapter.push_bill}[kind]
    res = pusher(doc, external_id=link.external_id, idempotency_key=_key(link, h))
    _save_external(link, res)
    diff = totals_diff(doc, res)
    renumbered = not _number_kept(adapter, doc, res)
    if diff or renumbered:
        try:
            adapter.discard_document(kind, res.external_id)
            link.external_id = ''
            link.save(update_fields=['external_id', 'updated_at'])
        except Exception:
            logger.exception('could not discard unverified %s %s', kind, res.external_id)
        _bump_tries(link)
        if renumbered:
            from core.accounting import mapping
            mapping.refresh_blockers(connection, adapter)
            raise Blocked(f'{provider} numbered {doc.number} as {res.external_number}, so it was removed again. '
                          + ('; '.join(mapping.provider_blockers(connection)) or
                             f'Check the numbering settings in {provider}.'))
        raise PermanentError(f'{provider} calculated different totals, so it was not posted: ' + '; '.join(diff))
    if res.status in ('DRAFT', 'SUBMITTED', ''):
        res = adapter.finalise_document(kind, res.external_id)
    return res, h


def _synced(link, res, h, connection, action, label, **meta):
    link.meta = {**(link.meta or {}), **meta}
    _finish(link, 'SYNCED', attempts=0, last_hash=h, last_synced_at=timezone.now(),
            external_number=res.external_number or link.external_number)
    log_event(connection, action, f'Synced {label}', object_type=link.object_type, local_id=link.local_id,
              label=label)


# ---------------------------------------------------------------- handlers

@handler('INVOICE')
def push_invoice(connection, adapter, link):
    from core.models import Invoice
    inv = (Invoice.objects.select_related('customer', 'load__vehicle', 'trip__vehicle')
           .filter(pk=link.local_id, company_id=connection.company_id).first())
    if inv is None or inv.status == 'DRAFT':
        _finish(link, 'VOIDED', error='Not an issued invoice; nothing to sync')
        return
    if inv.status == 'CANCELLED':
        if link.external_id:
            adapter.void_invoice(link.external_id, version=link.external_version)
            log_event(connection, 'void_invoice', f'Voided {inv.invoice_number}', object_type='INVOICE',
                      local_id=inv.pk, label=inv.invoice_number)
        _finish(link, 'VOIDED')
        return
    if not link.external_id and not in_scope(connection, inv.issue_date):
        raise Blocked(f'{inv.invoice_number} is dated before the cut-over date; it is not synced.')
    contact_id = ensure_contact(connection, 'CONTACT_CUSTOMER', inv.customer, adapter)
    tracker = documents.Tracker(connection, adapter)
    doc = documents.build_invoice(connection, inv, contact_id, tracker)
    res, h = push_document(connection, adapter, link, doc, kind='INVOICE')
    _synced(link, res, h, connection, 'push_invoice', inv.invoice_number)
    # Receipts TruckWys recorded before payments moved to the provider (the
    # initial sync couldn't push them while this invoice was failing).
    from core.models import Payment
    if Payment.objects.filter(invoice=inv, source__in=('MANUAL', 'BANK')).exists():
        from core.accounting.backfill import push_receipts_for_invoice
        push_receipts_for_invoice(connection, adapter, inv)
    for w in tracker.warnings:
        log_event(connection, 'push_invoice', w, level='WARNING', object_type='INVOICE', local_id=inv.pk,
                  label=inv.invoice_number)


def _invoice_external(connection, adapter, invoice):
    """Provider id of the invoice a credit note belongs to, linking an
    invoice from before the cut-over by number if the provider has it."""
    from core.accounting.contacts import get_link
    link = get_link(connection, 'INVOICE', invoice.pk)
    if link and link.status == 'SYNCED' and link.external_id:
        return link.external_id
    if in_scope(connection, invoice.issue_date):
        enqueue(connection, 'INVOICE', invoice.pk)
        raise RetryLater(f'Waiting for invoice {invoice.invoice_number} to sync first', retry_after=60)
    found = adapter.find_document('INVOICE', invoice.invoice_number)
    if found is None:
        return ''
    if found.total is not None and found.total != invoice.total_amount:
        log_event(connection, 'push_credit_note',
                  f'{connection.get_provider_display()} has an invoice numbered {invoice.invoice_number} but for '
                  f'{found.total} (TruckWys: {invoice.total_amount}); the credit note is left unallocated.',
                  level='WARNING', object_type='INVOICE', local_id=invoice.pk, label=invoice.invoice_number)
        return ''
    link = get_or_create_link(connection, 'INVOICE', invoice.pk)
    link.external_id, link.external_number = found.external_id, found.external_number
    link.status, link.last_synced_at = 'SYNCED', timezone.now()
    link.meta = {**(link.meta or {}), 'pre_cutover': True, 'matched_existing': True}
    link.save(update_fields=['external_id', 'external_number', 'status', 'last_synced_at', 'meta', 'updated_at'])
    return found.external_id


@handler('CREDIT_NOTE')
def push_credit_note(connection, adapter, link):
    from core.models import CreditNote
    cn = (CreditNote.objects.select_related('invoice', 'customer')
          .filter(pk=link.local_id, company_id=connection.company_id).first())
    if cn is None:
        _finish(link, 'VOIDED', error='Credit note no longer exists')
        return
    if cn.source != 'MANUAL':
        _finish(link, 'SYNCED' if link.external_id else 'VOIDED')
        return
    if cn.status == CreditNote.VOID:
        if link.external_id:
            adapter.void_credit_note(link.external_id, version=link.external_version)
            log_event(connection, 'void_credit_note', f'Voided {cn.credit_note_number}', object_type='CREDIT_NOTE',
                      local_id=cn.pk, label=cn.credit_note_number)
        _finish(link, 'VOIDED')
        return
    if not link.external_id and not in_scope(connection, cn.issue_date):
        raise Blocked(f'{cn.credit_note_number} is dated before the cut-over date; it is not synced.')
    inv_ext = _invoice_external(connection, adapter, cn.invoice)
    contact_id = ensure_contact(connection, 'CONTACT_CUSTOMER', cn.customer, adapter)
    tracker = documents.Tracker(connection, adapter)
    doc = documents.build_credit_note(connection, cn, contact_id, tracker)
    res, h = push_document(connection, adapter, link, doc, kind='CREDIT_NOTE')
    meta = {}
    if inv_ext and not (link.meta or {}).get('allocated'):
        # Idempotent: an allocation the provider applied but whose answer was
        # lost is found on the credit note, never sent twice.
        detail = adapter.get_credit_note_detail(res.external_id)
        already = sum((a['amount'] for a in detail['allocations'] if a['invoice_id'] == inv_ext), Decimal('0.00'))
        if already > 0:
            amount = already
        else:
            state = adapter.get_invoice_state(inv_ext)
            amount = min(cn.total_amount, detail['remaining'], max(Decimal('0.00'), state.amount_due))
            if amount > 0:
                adapter.allocate_credit_note(res.external_id, inv_ext, amount, cn.issue_date)
        meta['allocated'] = str(amount)
        link.meta = {**(link.meta or {}), 'allocated': str(amount)}
        link.save(update_fields=['meta', 'updated_at'])
        if amount < cn.total_amount:
            meta['unallocated'] = str(cn.total_amount - amount)
            log_event(connection, 'push_credit_note',
                      f'{cn.credit_note_number}: {cn.total_amount - amount} left as customer credit '
                      f'(the invoice had only {amount} outstanding)', level='WARNING', object_type='CREDIT_NOTE',
                      local_id=cn.pk, label=cn.credit_note_number)
    elif not inv_ext:
        meta['allocated'] = '0.00'
        log_event(connection, 'push_credit_note',
                  f'{cn.credit_note_number}: invoice {cn.invoice.invoice_number} isn\'t in '
                  f'{connection.get_provider_display()}, so the credit is unallocated', level='WARNING',
                  object_type='CREDIT_NOTE', local_id=cn.pk, label=cn.credit_note_number)
    _synced(link, res, h, connection, 'push_credit_note', cn.credit_note_number, **meta)


@handler('BILL')
def push_bill(connection, adapter, link):
    from core.models import Expense
    exp = (Expense.objects.select_related('supplier', 'vehicle')
           .filter(pk=link.local_id, company_id=connection.company_id).first())
    if exp is None or exp.status == 'REJECTED' or exp.supplier_id is None:
        if link.external_id:
            adapter.void_bill(link.external_id, version=link.external_version)
            log_event(connection, 'void_bill', f'Voided bill for expense {link.local_id}', object_type='BILL',
                      local_id=link.local_id)
        _finish(link, 'VOIDED')
        return
    if not link.external_id and not in_scope(connection, exp.expense_date):
        raise Blocked(f'Expense {exp.expense_number} is dated before the cut-over date; it is not synced.')
    contact_id = ensure_contact(connection, 'CONTACT_SUPPLIER', exp.supplier, adapter)
    tracker = documents.Tracker(connection, adapter)
    doc = documents.build_bill(connection, exp, contact_id, tracker)
    h = payload_hash(doc)
    if link.external_id and link.last_hash == h:
        _finish(link, 'SYNCED')
        return
    if link.external_id:
        res = _replace_bill(connection, adapter, link, doc, h, exp)
    else:
        res, h = push_document(connection, adapter, link, doc, kind='BILL')
    _synced(link, res, h, connection, 'push_bill', exp.expense_number)


def _replace_bill(connection, adapter, link, doc, h, exp):
    """An edited expense. Never changes a posted bill in place (the new
    figures would post before they are verified): a draft is updated in
    place; a posted, unpaid bill is replaced by a new one that is created
    unposted, verified, posted, and only then is the old one voided.

    Resumable: meta['replacing'] holds the old bill until it is gone, so a
    retry after a failed void only finishes that (it never re-creates, and
    never removes, the replacement)."""
    provider = connection.get_provider_display()
    meta = dict(link.meta or {})
    if meta.get('replacing'):
        adapter.void_bill(meta['replacing'], version=meta.get('replacing_version', ''))
        made_for = meta.get('replacement_hash')
        for k in ('replacing', 'replacing_version', 'replacement_hash'):
            meta.pop(k, None)
        link.meta = meta
        link.save(update_fields=['meta', 'updated_at'])
        if made_for == h:
            return adapter.get_document('BILL', link.external_id)
        # The expense changed again meanwhile: replace once more.
    state = adapter.get_bill_state(link.external_id)
    if state.status in ('PAID',) or state.amount_paid > 0:
        raise PermanentError(f'The bill for {exp.expense_number} is (partly) paid in {provider}; '
                             f'adjust it there.')
    if state.status in ('VOIDED', 'DELETED'):
        raise PermanentError(f'The bill for {exp.expense_number} was voided in {provider}.')
    if state.status in ('DRAFT', 'SUBMITTED'):
        res = adapter.push_bill(doc, external_id=link.external_id, version=link.external_version,
                                idempotency_key=_key(link, h))
        diff = totals_diff(doc, res)
        if diff:
            raise PermanentError(f'{provider} calculated different totals for the updated bill: ' + '; '.join(diff))
        return adapter.finalise_document('BILL', res.external_id)
    old_id, old_version = link.external_id, link.external_version
    res = adapter.push_bill(doc, idempotency_key=_key(link, h))
    diff = totals_diff(doc, res)
    if diff:
        try:
            adapter.discard_document('BILL', res.external_id)
        except Exception:
            logger.exception('could not discard unverified bill %s', res.external_id)
        _bump_tries(link)
        raise PermanentError(f'{provider} calculated different totals for the updated bill, so it was not '
                             'changed: ' + '; '.join(diff))
    if res.status in ('DRAFT', 'SUBMITTED', ''):
        res = adapter.finalise_document('BILL', res.external_id)
    # Point at the replacement and remember the old bill BEFORE voiding it.
    link.meta = {**(link.meta or {}), 'replacing': old_id, 'replacing_version': old_version,
                 'replacement_hash': h}
    link.save(update_fields=['meta', 'updated_at'])
    _save_external(link, res)
    adapter.void_bill(old_id, version=old_version)
    link.meta = {k: v for k, v in (link.meta or {}).items()
                 if k not in ('replacing', 'replacing_version', 'replacement_hash')}
    link.save(update_fields=['meta', 'updated_at'])
    log_event(connection, 'push_bill', f'{exp.expense_number} changed: bill replaced in {provider}',
              object_type='BILL', local_id=exp.pk, label=exp.expense_number)
    return res
