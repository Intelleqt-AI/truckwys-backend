"""Nightly "TruckWys vs <provider>" reconciliation.

Three views, each difference larger than R0.01 is stored for the UI:

  INVOICE   every synced invoice: total, VAT, amount outstanding, money
            received against it, and whether it is open / settled / void.
  CUSTOMER  open balance per linked customer (TruckWys: sum of invoice
            balances, credits negative; provider: amount due on open sales
            invoices minus unallocated credits).
  MONTH     each month from the cut-over: sales excl. VAT, output VAT,
            receipts, and debtors at month end.

Known, documented sources of difference (they are real and worth seeing):
documents entered directly in the provider, unallocated overpayments /
credit notes raised there, and invoices from before the cut-over that the
provider doesn't hold.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from core.accounting.base import not_pre_cutover
from core.accounting.events import log_event
from core.accounting.registry import get_adapter

logger = logging.getLogger(__name__)
ZERO = Decimal('0.00')
TOLERANCE = Decimal('0.01')
MAX_MONTHS = 12


def _status_class_truckwys(inv) -> str:
    if inv.status == 'CANCELLED':
        return 'void'
    return 'settled' if inv.balance <= 0 else 'open'


def _status_class_provider(state) -> str:
    s = (state.status or '').upper()
    if s in ('VOIDED', 'DELETED', 'VOID'):
        return 'void'
    if s == 'PAID' or state.amount_due <= 0:
        return 'settled'
    return 'open'


def _months(start: date, end: date):
    y, m = start.year, start.month
    out = []
    while (y, m) <= (end.year, end.month):
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
        out.append((f'{y}-{m:02d}', first, min(last, end)))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out[-MAX_MONTHS:]


class Collector:
    def __init__(self, run, connection, adapter):
        self.run, self.connection, self.adapter = run, connection, adapter
        self.rows = []

    def money(self, scope, key, label, field, ours, theirs, *, local_url='', provider_url=''):
        ours = Decimal(ours or 0).quantize(TOLERANCE)
        theirs = Decimal(theirs or 0).quantize(TOLERANCE)
        diff = ours - theirs
        if abs(diff) > TOLERANCE:
            self._add(scope, key, label, field, str(ours), str(theirs), diff, local_url, provider_url)

    def text(self, scope, key, label, field, ours, theirs, *, local_url='', provider_url=''):
        if ours != theirs:
            self._add(scope, key, label, field, ours, theirs, None, local_url, provider_url)

    def _add(self, scope, key, label, field, ours, theirs, diff, local_url, provider_url):
        from core.models import ReconciliationDifference
        self.rows.append(ReconciliationDifference(
            run=self.run, scope=scope, key=str(key)[:100], label=label[:255], field=field,
            truckwys_value=str(ours)[:60], provider_value=str(theirs)[:60], difference=diff,
            local_url=local_url[:255], provider_url=provider_url[:500]))


def run(connection):
    from core.models import (CreditNote, Customer, ExternalLink, Invoice, Payment, ReconciliationDifference,
                             ReconciliationRun)
    from core.services import accounting_reports as reports

    run = ReconciliationRun.objects.create(company_id=connection.company_id, connection=connection)
    if connection.status != 'ACTIVE' or not connection.cutover_date:
        run.status, run.error = ReconciliationRun.FAILED, 'Finish the setup first (connection, mapping, cut-over).'
        run.save()
        return run
    adapter = get_adapter(connection)
    col = Collector(run, connection, adapter)
    provider = connection.get_provider_display()
    try:
        # ---- invoices
        links = dict(ExternalLink.objects.filter(connection=connection, object_type='INVOICE', status='SYNCED')
                     .exclude(external_id='').filter(not_pre_cutover())
                     .values_list('local_id', 'external_id'))
        invoices = {i.pk: i for i in Invoice.objects.filter(pk__in=links.keys(), company_id=connection.company_id)
                    .select_related('customer')}
        states = {s.external_id: s for s in adapter.get_invoice_states(list(links.values()))}
        cn_alloc = {}
        for l in ExternalLink.objects.filter(connection=connection, object_type='CREDIT_NOTE', status='SYNCED'):
            cn = CreditNote.objects.filter(pk=l.local_id).values('invoice_id').first()
            if cn and (l.meta or {}).get('allocated'):
                cn_alloc[cn['invoice_id']] = cn_alloc.get(cn['invoice_id'], ZERO) + Decimal(l.meta['allocated'])
        remainders = dict(Payment.objects.filter(company_id=connection.company_id, source=connection.provider,
                                                 external_id__startswith='OVPREM:')
                          .values('invoice_id').annotate(t=Sum('amount')).values_list('invoice_id', 't'))
        for local_id, ext in links.items():
            inv = invoices.get(local_id)
            if inv is None:
                continue
            label = f'{inv.invoice_number} · {inv.customer.name}'
            urls = {'local_url': f'/finance/invoices/{inv.pk}', 'provider_url': adapter.web_url('INVOICE', ext)}
            st = states.get(ext)
            if st is None:
                col.text('INVOICE', inv.invoice_number, label, 'status', inv.get_status_display(),
                         f'missing in {provider}', **urls)
                continue
            if inv.status != 'CANCELLED':
                col.money('INVOICE', inv.invoice_number, label, 'total', inv.total_amount, st.total, **urls)
                col.money('INVOICE', inv.invoice_number, label, 'vat', inv.vat_amount, st.total_tax, **urls)
                col.money('INVOICE', inv.invoice_number, label, 'open_balance', max(inv.balance, ZERO),
                          st.amount_due, **urls)
                ours_paid = inv.paid_amount - (remainders.get(inv.pk) or ZERO)
                theirs_paid = st.amount_paid + st.amount_credited - cn_alloc.get(inv.pk, ZERO)
                col.money('INVOICE', inv.invoice_number, label, 'paid', ours_paid, theirs_paid, **urls)
            col.text('INVOICE', inv.invoice_number, label, 'status', _status_class_truckwys(inv),
                     _status_class_provider(st), **urls)

        # ---- customers
        cust_links = dict(ExternalLink.objects.filter(connection=connection, object_type='CONTACT_CUSTOMER',
                                                      status='SYNCED').exclude(external_id='')
                          .values_list('local_id', 'external_id'))
        receivables = adapter.receivables_by_contact()
        balances = dict(Invoice.objects.filter(company_id=connection.company_id, customer_id__in=cust_links.keys(),
                                               status__in=Invoice.ISSUED_STATUSES)
                        .values('customer_id').annotate(t=Sum('balance')).values_list('customer_id', 't'))
        names = dict(Customer.objects.filter(pk__in=cust_links.keys()).values_list('pk', 'name'))
        for cid, ext in cust_links.items():
            col.money('CUSTOMER', names.get(cid, cid), names.get(cid, str(cid)), 'open_balance',
                      balances.get(cid) or ZERO, receivables.get(ext, ZERO),
                      local_url=f'/customers/{cid}', provider_url=adapter.web_url('CONTACT_CUSTOMER', ext))

        # ---- months
        today = timezone.localdate()
        months = _months(connection.cutover_date, today)
        company = connection.company
        for key, start, end in months:
            docs = adapter.list_sales_documents(start, end)
            live = [d for d in docs if (d.status or '').upper() not in ('VOIDED', 'DELETED', 'VOID')]
            sign = {'INVOICE': 1, 'CREDIT_NOTE': -1}
            p_sales = sum((d.sub_total * sign[d.kind] for d in live), ZERO)
            p_vat = sum((d.total_tax * sign[d.kind] for d in live), ZERO)
            s = reports.sales(company, start, end)
            col.money('MONTH', key, key, 'sales_excl_vat', s['revenue_excl_vat'], p_sales)
            col.money('MONTH', key, key, 'output_vat', s['output_vat'], p_vat)
            receipts = sum((r.amount for r in adapter.list_receipts(start, end)), ZERO)
            col.money('MONTH', key, key, 'receipts', reports.cash(company, start, end)['cash_received_incl_vat'],
                      receipts)
            debtors = adapter.debtors_at(end)
            if debtors is not None:
                ageing = reports.debtors_ageing(company, end)
                col.money('MONTH', key, key, 'debtors', ageing['total'] - ageing['customer_credits'], debtors)
        run.checked = {'invoices': len(links), 'customers': len(cust_links), 'months': len(months)}
    except Exception as exc:
        logger.exception('reconciliation failed for connection %s', connection.pk)
        run.status, run.error = ReconciliationRun.FAILED, str(exc)[:2000]
        run.save()
        log_event(connection, 'reconcile', f'Reconciliation failed: {exc}', level='ERROR')
        return run
    ReconciliationDifference.objects.bulk_create(col.rows)
    run.difference_count = len(col.rows)
    run.status = ReconciliationRun.DIFFERENCES if col.rows else ReconciliationRun.OK
    run.save()
    connection.last_reconciled_at = timezone.now()
    connection.save(update_fields=['last_reconciled_at', 'updated_at'])
    log_event(connection, 'reconcile', f'Reconciliation: {len(col.rows)} differences',
              level='WARNING' if col.rows else 'INFO')
    return run


def serialize(run) -> dict:
    if run is None:
        return {'run': None, 'differences': []}
    return {
        'run': {'id': run.pk, 'ran_at': run.ran_at, 'status': run.status, 'error': run.error,
                'checked': run.checked, 'difference_count': run.difference_count},
        'differences': [{'id': d.pk, 'scope': d.scope, 'key': d.key, 'label': d.label, 'field': d.field,
                         'truckwys': d.truckwys_value, 'provider': d.provider_value,
                         'difference': str(d.difference) if d.difference is not None else None,
                         'local_url': d.local_url, 'provider_url': d.provider_url}
                        for d in run.differences.all()],
    }
