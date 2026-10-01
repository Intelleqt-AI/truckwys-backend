"""First sync after connecting ("backfill").

    connect -> read tax rates / accounts / tracking -> match contacts (wizard)
    -> choose a cut-over date -> push invoices and credit notes from that date
    -> push receipts already recorded in TruckWys against them
    -> push supplier bills from that date -> pull payments / credits since.

Documents dated BEFORE the cut-over date are never pushed: they are assumed
to be in the books already (a credit note on such an invoice is still pushed,
and allocated to it when the provider has an invoice with that number).

Receipts recorded in TruckWys before the connection are pushed once, to the
mapped receipts (bank) account, and then become provider payments in
TruckWys (source + external id), so the provider stays the single source of
payments from then on. A receipt larger than what is still due at the
provider is split: the due part is a payment, the excess an overpayment
(customer credit), exactly as TruckWys showed it.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.accounting import mapping
from core.accounting.base import PermanentError
from core.accounting.events import log_event
from core.accounting.registry import get_adapter

logger = logging.getLogger(__name__)

STEPS = [
    ('settings', 'Read tax rates, accounts and tracking'),
    ('contacts', 'Match contacts'),
    ('invoices', 'Push invoices from the cut-over date'),
    ('receipts', 'Push credit notes and payments recorded in TruckWys, in date order'),
    ('bills', 'Push supplier bills'),
    ('payments', 'Pull payments and credit allocations'),
]


class BackfillError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _invoices(connection, cutover):
    from core.models import Invoice
    return (Invoice.objects.filter(company_id=connection.company_id, issue_date__gte=cutover,
                                   status__in=Invoice.ISSUED_STATUSES).order_by('issue_date', 'id'))


def _credit_notes(connection, cutover):
    from core.models import CreditNote
    return (CreditNote.objects.filter(company_id=connection.company_id, issue_date__gte=cutover,
                                      status=CreditNote.ISSUED, source='MANUAL').order_by('issue_date', 'id'))


def _bills(connection, cutover):
    from core.models import Expense
    return (Expense.objects.filter(company_id=connection.company_id, expense_date__gte=cutover,
                                   supplier__isnull=False).exclude(status='REJECTED').order_by('expense_date', 'id'))


def _historic_receipts(connection, cutover):
    from core.models import Payment
    # Receipts entered in TruckWys (or matched from a bank statement) before
    # the connection; anything already from this provider is skipped.
    return (Payment.objects.filter(company_id=connection.company_id, source__in=('MANUAL', 'BANK'),
                                   invoice__in=_invoices(connection, cutover)).order_by('payment_date', 'id'))


def preview(connection, cutover: date | None) -> dict:
    if not cutover:
        return {'invoices': 0, 'credit_notes': 0, 'bills': 0, 'historic_receipts': 0, 'contacts_unconfirmed': 0}
    from core.models import ExternalLink
    return {
        'invoices': _invoices(connection, cutover).count(),
        'credit_notes': _credit_notes(connection, cutover).count(),
        'bills': _bills(connection, cutover).count(),
        'historic_receipts': _historic_receipts(connection, cutover).count(),
        'contacts_unconfirmed': ExternalLink.objects.filter(
            connection=connection, object_type__in=('CONTACT_CUSTOMER', 'CONTACT_SUPPLIER'),
            status='SUGGESTED').count(),
    }


def status(connection) -> dict:
    b = connection.backfill or {}
    steps = {s['key']: s for s in b.get('steps') or []}
    raw = (connection.settings or {}).get('cutover_date')
    return {
        'state': b.get('state', 'NOT_STARTED'),
        'cutover_date': raw,
        'started_at': b.get('started_at'), 'finished_at': b.get('finished_at'),
        'steps': [{'key': k, 'label': label, 'state': steps.get(k, {}).get('state', 'PENDING'),
                   'count': steps.get(k, {}).get('count', 0), 'error': steps.get(k, {}).get('error', '')}
                  for k, label in STEPS],
        'preview': preview(connection, connection.cutover_date),
    }


def start(connection, cutover_raw, user=None) -> dict:
    """Validate, record the cut-over date and queue the backfill."""
    from core.models import AccountingConnection
    if connection.status != AccountingConnection.ACTIVE:
        raise BackfillError('Finish connecting first.', 'not_active')
    b = connection.backfill or {}
    if b.get('state') == 'RUNNING' and not _stale(b):
        raise BackfillError('The initial sync is already running.', 'already_running')
    try:
        cutover = date.fromisoformat(str(cutover_raw))
    except (TypeError, ValueError):
        raise BackfillError('Choose a valid cut-over date (YYYY-MM-DD).', 'invalid_cutover')
    if cutover > timezone.localdate():
        raise BackfillError('The cut-over date can\'t be in the future.', 'invalid_cutover')
    existing = connection.cutover_date
    if existing and cutover > existing:
        raise BackfillError(f'The cut-over date can only move earlier (it is {existing}); documents from '
                            f'{existing} are already in {connection.get_provider_display()}.', 'invalid_cutover')
    if not mapping.is_complete(connection):
        raise BackfillError('Map every revenue type, expense category and tax code first.', 'mapping_incomplete')
    pv = preview(connection, cutover)
    if pv['contacts_unconfirmed']:
        raise BackfillError(f'Confirm the {pv["contacts_unconfirmed"]} suggested contact matches first.',
                            'contacts_unconfirmed')
    if pv['historic_receipts'] and not (connection.settings or {}).get('receipts_account'):
        raise BackfillError(f'{pv["historic_receipts"]} payments were recorded in TruckWys against these invoices. '
                            'Choose the bank account they were received into (Mapping → Receipts account).',
                            'mapping_incomplete')
    s = dict(connection.settings or {})
    s['cutover_date'] = cutover.isoformat()
    connection.settings = s
    connection.backfill = {'state': 'RUNNING', 'started_at': timezone.now().isoformat(), 'finished_at': None,
                           'started_by': getattr(user, 'pk', None),
                           'steps': [{'key': k, 'state': 'PENDING', 'count': 0, 'error': ''} for k, _ in STEPS]}
    connection.save(update_fields=['settings', 'backfill', 'updated_at'])
    log_event(connection, 'backfill', f'Initial sync started from {cutover}')

    def go():
        from django.conf import settings
        if getattr(settings, 'ACCOUNTING_SYNC_EAGER', False):
            run(connection.pk)
            return
        try:
            from core.accounting.tasks import run_backfill
            run_backfill.delay(connection.pk)
        except Exception:
            logger.exception('could not enqueue backfill')
            _set_state(connection.pk, 'FAILED', error='Could not start the background job; try again.')
    transaction.on_commit(go)
    return status(connection)


STALE_AFTER_HOURS = 2


def _stale(b) -> bool:
    """A RUNNING backfill whose worker died (deploy, OOM) can be restarted
    once it has made no progress for STALE_AFTER_HOURS."""
    from datetime import datetime, timedelta
    raw = b.get('heartbeat') or b.get('started_at')
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return timezone.now() - last > timedelta(hours=STALE_AFTER_HOURS)


def _set_step(connection, key, state, count=None, error=None):
    b = dict(connection.backfill or {})
    for s in b.get('steps') or []:
        if s['key'] == key:
            s['state'] = state
            if count is not None:
                s['count'] = count
            if error is not None:
                s['error'] = error[:1000]
    b['heartbeat'] = timezone.now().isoformat()
    connection.backfill = b
    connection.save(update_fields=['backfill', 'updated_at'])


def _set_state(connection_id, state, error=''):
    from core.models import AccountingConnection
    conn = AccountingConnection.objects.get(pk=connection_id)
    b = dict(conn.backfill or {})
    b['state'] = state
    b['finished_at'] = timezone.now().isoformat()
    if error:
        b['error'] = error
    conn.backfill = b
    conn.save(update_fields=['backfill', 'updated_at'])


def run(connection_id):
    """The whole backfill, step by step. Every step is idempotent, so a re-run
    (after a failure, a rate limit or a dead worker) continues where it
    stopped. A rate limit / outage re-schedules the job itself after the
    provider's Retry-After; anything else stops it as FAILED with the reason."""
    from core.models import AccountingConnection
    from core.accounting import contacts, sync
    from core.accounting.base import RateLimited, TransientError
    from core.accounting.pull import poll_payments

    conn = AccountingConnection.objects.select_related('company').get(pk=connection_id)
    cutover = conn.cutover_date
    current = None
    failed_steps = []

    def run_links(object_type, objs):
        ok = failed = 0
        for obj in objs:
            link = sync.get_or_create_link(conn, object_type, obj.pk)
            st = link.status if link.status == 'SYNCED' else sync.run_link(link.pk)
            if st == 'SYNCED':
                ok += 1
            elif st == 'ERROR' and link_retry_after(link.pk):
                raise TransientError('waiting to retry', retry_after=link_retry_after(link.pk))
            else:
                failed += 1
        return ok, failed

    try:
        current = 'settings'
        _set_step(conn, current, 'RUNNING')
        mapping.refresh_options(conn)
        _set_step(conn, current, 'DONE')

        current = 'contacts'
        _set_step(conn, current, 'RUNNING')
        summ = contacts.run_matching(conn)
        _set_step(conn, current, 'DONE', count=summ.get('MATCHED', 0) + summ.get('CREATE', 0))

        current = 'invoices'
        _set_step(conn, current, 'RUNNING')
        ok, failed = run_links('INVOICE', _invoices(conn, cutover))
        if failed:
            failed_steps.append(current)
        _set_step(conn, current, 'FAILED' if failed else 'DONE', count=ok,
                  error=f'{failed} need attention (see Errors)' if failed else '')

        # Credit notes and historic receipts in the order they happened, so
        # each lands in the provider the way it did in TruckWys (a credit
        # note after full payment stays as customer credit; a payment after
        # a credit note pays what was left).
        current = 'receipts'
        _set_step(conn, current, 'RUNNING')
        events = ([(cn.issue_date, 1, 'cn', cn) for cn in _credit_notes(conn, cutover)] +
                  [(p.payment_date, 0, 'pay', p) for p in _historic_receipts(conn, cutover)])
        events.sort(key=lambda e: (e[0], e[1], e[3].pk))
        adapter = get_adapter(conn)
        account = (conn.settings or {}).get('receipts_account')
        ok, problems = 0, []
        for _when, _order, kind, obj in events:
            if kind == 'cn':
                c_ok, c_failed = run_links('CREDIT_NOTE', [obj])
                if c_ok:
                    ok += 1
                else:
                    problems.append(f'{obj.credit_note_number}: see Errors')
            else:
                problem = push_receipt(conn, adapter, obj, account)
                if problem:
                    problems.append(problem)
                else:
                    ok += 1
        if problems:
            failed_steps.append(current)
        _set_step(conn, current, 'FAILED' if problems else 'DONE', count=ok,
                  error='; '.join(problems)[:1000] if problems else '')
        if ok:
            log_event(conn, 'backfill', f'{ok} credit notes and receipts recorded in TruckWys were pushed; '
                                        f'payments are now managed in {conn.get_provider_display()}')

        current = 'bills'
        _set_step(conn, current, 'RUNNING')
        ok, failed = run_links('BILL', _bills(conn, cutover))
        if failed:
            failed_steps.append(current)
        _set_step(conn, current, 'FAILED' if failed else 'DONE', count=ok,
                  error=f'{failed} need attention (see Errors)' if failed else '')

        current = 'payments'
        _set_step(conn, current, 'RUNNING')
        conn.refresh_from_db()
        totals = poll_payments(conn)
        _set_step(conn, current, 'DONE', count=totals.get('created', 0) if isinstance(totals, dict) else 0)
    except (RateLimited, TransientError) as exc:
        wait = max(30.0, float(getattr(exc, 'retry_after', None) or 60))
        conn.refresh_from_db()
        if _resume_later(conn.pk, wait):
            when = timezone.localtime(timezone.now() + timedelta(seconds=wait)).strftime('%H:%M')
            _set_step(conn, current, 'RUNNING', error=f'Waiting for {conn.get_provider_display()} '
                                                      f'({exc}); continues by itself at {when}')
            return 'WAITING'
        _set_step(conn, current, 'FAILED', error=str(exc))
        _set_state(conn.pk, 'FAILED', error=str(exc))
        log_event(conn, 'backfill', f'Initial sync paused at "{current}": {exc}. Start it again; finished '
                                     'steps are not repeated.', level='ERROR')
        return 'FAILED'
    except Exception as exc:
        logger.exception('backfill failed at %s', current)
        conn.refresh_from_db()
        if current:
            _set_step(conn, current, 'FAILED', error=str(exc))
        _set_state(conn.pk, 'FAILED', error=str(exc))
        log_event(conn, 'backfill', f'Initial sync stopped at "{current}": {exc}. Fix it and start it again; '
                                     'finished steps are not repeated.', level='ERROR')
        return 'FAILED'
    if failed_steps:
        _set_state(conn.pk, 'FAILED', error='Some documents need attention: ' + ', '.join(failed_steps))
        log_event(conn, 'backfill', 'Initial sync finished, but some documents need attention (see Errors). '
                                    'Fix them and start it again; synced documents are skipped.', level='WARNING')
        return 'FAILED'
    _set_state(conn.pk, 'DONE')
    log_event(conn, 'backfill', 'Initial sync finished')
    return 'DONE'


def link_retry_after(link_id) -> float:
    """Seconds until an ERROR link (rate limited / outage) may run again."""
    from core.models import ExternalLink
    nxt = ExternalLink.objects.filter(pk=link_id, status='ERROR').values_list('next_attempt_at', flat=True).first()
    if not nxt:
        return 0.0
    return max(0.0, (nxt - timezone.now()).total_seconds())


def _resume_later(connection_id, wait) -> bool:
    from django.conf import settings
    if getattr(settings, 'ACCOUNTING_SYNC_EAGER', False):
        return False
    try:
        from core.accounting.tasks import run_backfill
        run_backfill.apply_async((connection_id,), countdown=int(wait) + 1)
        return True
    except Exception:
        logger.exception('could not re-schedule the backfill')
        return False


def push_receipt(connection, adapter, p, account):
    """One TruckWys receipt -> a provider payment (+ an overpayment for any
    excess), then adopted: the TruckWys row becomes source=<provider> with its
    id. Returns a problem string, or '' when done (or already done).

    Safe to repeat after any failure: no provider call runs inside a DB
    transaction, each provider id is stored the moment it exists (PAYMENT
    link meta), and a payment the provider applied but whose answer was lost
    is found again by its reference before anything is re-sent."""
    from core.models import ExternalLink, Payment
    from core.accounting.contacts import get_link
    from core.services.ledger import recalculate_invoice

    provider = connection.provider
    p = Payment.objects.select_related('invoice').get(pk=p.pk)
    if p.source not in ('MANUAL', 'BANK'):
        return ''
    inv_link = get_link(connection, 'INVOICE', p.invoice_id)
    if not (inv_link and inv_link.status == 'SYNCED' and inv_link.external_id):
        return f'{p.payment_number}: invoice {p.invoice.invoice_number} isn\'t synced yet'
    if (inv_link.meta or {}).get('matched_existing'):
        return adopt_matched_invoice(connection, adapter, p.invoice, inv_link)
    if not account:
        return f'{p.payment_number}: no receipts account mapped'
    ref = f'TruckWys {p.payment_number}'
    rec_link, _ = ExternalLink.objects.get_or_create(
        connection=connection, object_type='PAYMENT', local_id=p.pk,
        defaults={'company_id': connection.company_id, 'provider': provider, 'status': 'PENDING'})
    rec = dict(rec_link.meta or {})

    def remember(**kw):
        rec.update(kw)
        rec_link.meta = rec
        if kw.get('payment_id'):
            rec_link.external_id = kw['payment_id']
        rec_link.save(update_fields=['meta', 'external_id', 'updated_at'])

    if 'pay_part' not in rec:
        state = adapter.get_invoice_state(inv_link.external_id)
        found = next((s for s in state.settlements if s.kind == 'PAYMENT' and (s.reference or '').strip() == ref),
                     None)
        if found is not None:
            remember(pay_part=str(found.amount), payment_id=found.external_id)
        else:
            pay_part = min(p.amount, max(Decimal('0.00'), state.amount_due))
            payment_id = ''
            if pay_part > 0:
                res = adapter.push_payment(invoice_external_id=inv_link.external_id, amount=pay_part,
                                           on=p.payment_date, account_code=account, reference=ref,
                                           idempotency_key=f'tw-{connection.company_id}-receipt-{p.pk}')
                payment_id = res.external_id
            remember(pay_part=str(pay_part), payment_id=payment_id)
    pay_part = Decimal(rec['pay_part'])
    excess = p.amount - pay_part
    if excess > 0 and not rec.get('overpayment_id'):
        contact = get_link(connection, 'CONTACT_CUSTOMER', p.invoice.customer_id)
        if not (contact and contact.external_id):
            return f'{p.payment_number}: the customer contact isn\'t linked'
        oref = f'{ref} (overpayment)'
        found = next((c for c in adapter.list_unallocated_credits()
                      if c.kind == 'OVERPAYMENT' and (c.number or '').strip() == oref), None)
        if found is not None:
            remember(overpayment_id=found.external_id)
        else:
            ovp = adapter.push_overpayment(contact_id=contact.external_id, amount=excess, on=p.payment_date,
                                           account_code=account, reference=oref,
                                           idempotency_key=f'tw-{connection.company_id}-overpay-{p.pk}')
            if not ovp.external_id:
                raise PermanentError(f'{connection.get_provider_display()} didn\'t return the overpayment id '
                                     f'for {p.payment_number}')
            remember(overpayment_id=ovp.external_id)

    # Adopt locally: database only, no provider calls from here on.
    note = f'Overpayment held as customer credit in {connection.get_provider_display()}'
    with transaction.atomic():
        row = Payment.objects.select_for_update().get(pk=p.pk)
        if row.source not in ('MANUAL', 'BANK'):
            return ''
        rem = None
        if pay_part > 0:
            row.amount = pay_part
            row.source, row.external_id = provider, rec['payment_id']
            row.save(update_fields=['amount', 'source', 'external_id', 'updated_at'])
            if excess > 0:
                rem = Payment.objects.create(
                    company=row.company, invoice=row.invoice, customer=row.customer, amount=excess,
                    payment_date=row.payment_date, payment_method=row.payment_method,
                    payment_number=f'{row.payment_number}-X'[:100], reference_number=row.reference_number,
                    notes=note, source=provider, external_id=f'OVPREM:{rec["overpayment_id"]}'[:100])
        else:
            row.source, row.external_id = provider, f'OVPREM:{rec["overpayment_id"]}'[:100]
            row.notes = (row.notes + '\n' if row.notes else '') + note
            row.save(update_fields=['source', 'external_id', 'notes', 'updated_at'])
            rem = row
        if rem is not None:
            ExternalLink.objects.update_or_create(
                connection=connection, object_type='OVERPAYMENT', local_id=rem.pk,
                defaults={'company_id': connection.company_id, 'provider': provider,
                          'external_id': rec['overpayment_id'], 'status': 'SYNCED', 'last_synced_at': timezone.now(),
                          'meta': {'origin_invoice': row.invoice_id, 'origin_payment': p.pk}})
        rec_link.status = 'SYNCED'
        rec_link.last_synced_at = timezone.now()
        rec_link.save(update_fields=['status', 'last_synced_at', 'updated_at'])
        recalculate_invoice(row.invoice_id)
    return ''


def adopt_matched_invoice(connection, adapter, invoice, inv_link):
    """An invoice that was already in the provider (typed in by the
    accountant) and linked by number: its receipts are presumably recorded
    there too. If the provider shows exactly what TruckWys recorded, swap the
    TruckWys rows for the provider's; otherwise report it (never push, never
    double count)."""
    from core.models import Payment
    from core.accounting.settlements import mirror_invoice
    state = adapter.get_invoice_state(inv_link.external_id)
    theirs = sum((s.amount for s in state.settlements if s.kind in ('PAYMENT', 'OVERPAYMENT', 'PREPAYMENT')),
                 Decimal('0.00'))
    rows = Payment.objects.filter(invoice=invoice, source__in=('MANUAL', 'BANK'))
    ours = sum((r.amount for r in rows), Decimal('0.00'))
    name = connection.get_provider_display()
    if theirs != ours:
        return (f'{invoice.invoice_number} was already in {name} with R{theirs} received, but TruckWys has '
                f'R{ours}. Record the difference in {name}, then start the initial sync again.')
    with transaction.atomic():
        for r in rows.select_for_update():
            r.delete()
    mirror_invoice(connection, invoice, state, adapter=adapter)
    return ''


def push_receipts_for_invoice(connection, adapter, invoice):
    """Receipts recorded in TruckWys on an invoice that reached the provider
    after the initial sync (its push had failed then). Logs problems."""
    from core.models import Payment
    account = (connection.settings or {}).get('receipts_account')
    problems = []
    for p in Payment.objects.filter(invoice=invoice, source__in=('MANUAL', 'BANK')).order_by('payment_date', 'id'):
        problem = push_receipt(connection, adapter, p, account)
        if problem:
            problems.append(problem)
    for problem in problems:
        log_event(connection, 'push_receipt', problem, level='ERROR', object_type='INVOICE', local_id=invoice.pk,
                  label=invoice.invoice_number)
    return problems
