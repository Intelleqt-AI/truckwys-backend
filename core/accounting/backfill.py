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
from datetime import date
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
    if (connection.backfill or {}).get('state') == 'RUNNING':
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


def _set_step(connection, key, state, count=None, error=None):
    b = dict(connection.backfill or {})
    for s in b.get('steps') or []:
        if s['key'] == key:
            s['state'] = state
            if count is not None:
                s['count'] = count
            if error is not None:
                s['error'] = error[:1000]
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
    """The whole backfill, step by step. Each step is idempotent, so a
    re-run (after a failure or a rate limit) continues where it stopped."""
    from core.models import AccountingConnection
    from core.accounting import contacts, sync
    from core.accounting.pull import poll_payments

    conn = AccountingConnection.objects.select_related('company').get(pk=connection_id)
    cutover = conn.cutover_date
    current = None
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
        ok = failed = 0
        for inv in _invoices(conn, cutover):
            link = sync.get_or_create_link(conn, 'INVOICE', inv.pk)
            st = link.status if link.status == 'SYNCED' else sync.run_link(link.pk)
            ok, failed = (ok + 1, failed) if st == 'SYNCED' else (ok, failed + 1)
        _set_step(conn, current, 'DONE', count=ok, error=f'{failed} need attention (see Errors)' if failed else '')

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
                link = sync.get_or_create_link(conn, 'CREDIT_NOTE', obj.pk)
                st = link.status if link.status == 'SYNCED' else sync.run_link(link.pk)
                if st == 'SYNCED':
                    ok += 1
                else:
                    problems.append(f'{obj.credit_note_number}: see Errors')
            else:
                problem = push_receipt(conn, adapter, obj, account)
                if problem:
                    problems.append(problem)
                else:
                    ok += 1
        _set_step(conn, current, 'DONE' if not problems else 'FAILED', count=ok,
                  error='; '.join(problems)[:1000] if problems else '')
        if ok:
            log_event(conn, 'backfill', f'{ok} credit notes and receipts recorded in TruckWys were pushed; '
                                        f'payments are now managed in {conn.get_provider_display()}')

        current = 'bills'
        _set_step(conn, current, 'RUNNING')
        ok = failed = 0
        for exp in _bills(conn, cutover):
            link = sync.get_or_create_link(conn, 'BILL', exp.pk)
            st = link.status if link.status == 'SYNCED' else sync.run_link(link.pk)
            ok, failed = (ok + 1, failed) if st == 'SYNCED' else (ok, failed + 1)
        _set_step(conn, current, 'DONE', count=ok, error=f'{failed} need attention (see Errors)' if failed else '')

        current = 'payments'
        _set_step(conn, current, 'RUNNING')
        conn.refresh_from_db()
        totals = poll_payments(conn)
        _set_step(conn, current, 'DONE', count=totals.get('created', 0) if isinstance(totals, dict) else 0)
    except Exception as exc:
        logger.exception('backfill failed at %s', current)
        conn.refresh_from_db()
        if current:
            _set_step(conn, current, 'FAILED', error=str(exc))
        _set_state(conn.pk, 'FAILED', error=str(exc))
        log_event(conn, 'backfill', f'Initial sync stopped at "{current}": {exc}. Fix it and start it again; '
                                     'finished steps are not repeated.', level='ERROR')
        return 'FAILED'
    _set_state(conn.pk, 'DONE')
    log_event(conn, 'backfill', 'Initial sync finished')
    return 'DONE'


def push_receipt(connection, adapter, p, account):
    """One TruckWys receipt -> provider payment (+ overpayment for any excess),
    then adopted: the TruckWys row becomes source=<provider> with its id.
    Returns a problem string, or '' when done (or already done)."""
    from core.models import ExternalLink, Payment
    from core.accounting.contacts import get_link
    from core.services.ledger import recalculate_invoice

    provider = connection.provider
    if p.source not in ('MANUAL', 'BANK'):
        return ''
    inv_link = get_link(connection, 'INVOICE', p.invoice_id)
    if not (inv_link and inv_link.status == 'SYNCED' and inv_link.external_id):
        return f'{p.payment_number}: invoice {p.invoice.invoice_number} isn\'t synced yet'
    if not account:
        return f'{p.payment_number}: no receipts account mapped'
    state = adapter.get_invoice_state(inv_link.external_id)
    due = max(Decimal('0.00'), state.amount_due)
    ref = f'TruckWys {p.payment_number}'
    with transaction.atomic():
        row = Payment.objects.select_for_update().get(pk=p.pk)
        if row.source not in ('MANUAL', 'BANK'):
            return ''
        pay_part = min(row.amount, due)
        excess = row.amount - pay_part
        res = None
        if pay_part > 0:
            res = adapter.push_payment(invoice_external_id=inv_link.external_id, amount=pay_part,
                                       on=row.payment_date, account_code=account, reference=ref,
                                       idempotency_key=f'tw-{connection.company_id}-receipt-{row.pk}')
            ExternalLink.objects.update_or_create(
                connection=connection, object_type='PAYMENT', local_id=row.pk,
                defaults={'company_id': connection.company_id, 'provider': provider,
                          'external_id': res.external_id, 'status': 'SYNCED', 'last_synced_at': timezone.now()})
        if excess > 0:
            contact = get_link(connection, 'CONTACT_CUSTOMER', row.invoice.customer_id)
            if not (contact and contact.external_id):
                raise PermanentError(f'{row.payment_number}: the customer contact isn\'t linked')
            ovp = adapter.push_overpayment(contact_id=contact.external_id, amount=excess, on=row.payment_date,
                                           account_code=account, reference=f'{ref} (overpayment)',
                                           idempotency_key=f'tw-{connection.company_id}-overpay-{row.pk}')
            note = f'Overpayment held as customer credit in {connection.get_provider_display()}'
            if res is not None:
                row.amount = pay_part
                row.source, row.external_id = provider, res.external_id
                row.save(update_fields=['amount', 'source', 'external_id', 'updated_at'])
                rem = Payment.objects.create(
                    company=row.company, invoice=row.invoice, customer=row.customer, amount=excess,
                    payment_date=row.payment_date, payment_method=row.payment_method,
                    payment_number=f'{row.payment_number}-X'[:100], reference_number=row.reference_number,
                    notes=note, source=provider, external_id=f'OVPREM:{ovp.external_id}'[:100])
            else:
                row.source, row.external_id = provider, f'OVPREM:{ovp.external_id}'[:100]
                row.notes = (row.notes + '\n' if row.notes else '') + note
                row.save(update_fields=['source', 'external_id', 'notes', 'updated_at'])
                rem = row
            ExternalLink.objects.update_or_create(
                connection=connection, object_type='OVERPAYMENT', local_id=rem.pk,
                defaults={'company_id': connection.company_id, 'provider': provider,
                          'external_id': ovp.external_id, 'status': 'SYNCED', 'last_synced_at': timezone.now(),
                          'meta': {'origin_invoice': row.invoice_id}})
        else:
            row.source, row.external_id = provider, res.external_id
            row.save(update_fields=['source', 'external_id', 'updated_at'])
        recalculate_invoice(row.invoice_id)
    return ''
