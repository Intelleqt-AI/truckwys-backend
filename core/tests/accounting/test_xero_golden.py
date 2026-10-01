"""The foundation golden dataset, pushed through the fake Xero ledger.

core/tests/fixtures/golden_ledger.json is replayed into TruckWys exactly as
test_golden_ledger does (50 invoices, every tax code, discounts, partial and
full credit notes, a voided credit note, voided invoices, an overpayment,
edited and deleted payments, 21 expenses). Then Xero is connected, mapped
and the initial sync runs from 1 July 2026.

Asserted to the cent, per month and for the quarter:
  * Xero output tax per tax rate  == TruckWys VAT201 split (frozen `expected`)
  * Xero input tax                == TruckWys input VAT on supplier expenses
  * Xero P&L income               == TruckWys revenue excl. VAT
  * Xero P&L expenses             == TruckWys supplier expenses excl. VAT
  * Xero aged receivables per contact and Balance Sheet AR (15 Aug, 30 Sep)
                                   == TruckWys debtors ageing, net of credits
  * TruckWys' own reports still equal `expected` after the sync
  * reconciliation: zero differences, also after payments, deletions and
    allocations are made in Xero.
Documented differences (docs/integrations/XERO.md §4) are asserted as such.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase
from rest_framework.test import APIClient

from core.accounting import backfill, pull, reconciliation
from core.accounting.http import use_transport
from core.models import CreditNote, Customer, Expense, ExternalLink, Invoice, Payment
from core.services import accounting_reports as reports
from core.tests.accounting.fake_xero import FakeXero
from core.tests.accounting.xero_helpers import (
    PURCHASE_TAX, SALES_TAX, connect, map_everything, no_commit_delay, xero_settings,
)
from core.tests.golden_loader import create_golden_company, load_golden_dataset

D = Decimal
ZERO = D('0.00')


def m(v):
    return D(v or 0).quantize(D('0.01'))


class XeroGoldenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = create_golden_company()
        cls.golden = load_golden_dataset(cls.company)
        cls.exp = cls.golden.expected
        cls.periods = {p['key']: (date.fromisoformat(p['start']), date.fromisoformat(p['end']))
                       for p in cls.golden.dataset['periods']}

    def setUp(self):
        self._settings = xero_settings()
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.xero = FakeXero(orgs=[{'tenant_id': 'golden-org', 'name': 'Golden Haulage (Pty) Ltd',
                                    'currency': 'ZAR', 'short_code': '!gold', 'country': 'ZA'}])
        t = use_transport(self.xero)
        t.__enter__()
        self.addCleanup(t.__exit__, None, None, None)
        self.conn, outcome = connect(self.company, self.golden.user, self.xero)
        self.assertEqual(outcome, 'connected')
        map_everything(self.conn)
        with no_commit_delay(self):
            backfill.start(self.conn, '2026-07-01', self.golden.user)
        self.conn.refresh_from_db()

    # ------------------------------------------------------------ helpers
    def contact_for(self, customer):
        return ExternalLink.objects.get(connection=self.conn, object_type='CONTACT_CUSTOMER',
                                        local_id=customer.pk).external_id

    def supplier_expenses(self, start, end):
        qs = (Expense.objects.filter(company=self.company, supplier__isnull=False, expense_date__gte=start,
                                     expense_date__lte=end).exclude(status='REJECTED'))
        gross = qs.aggregate(t=Sum('amount'))['t'] or ZERO
        vat = qs.aggregate(t=Sum('vat_amount'))['t'] or ZERO
        return gross - vat, vat

    def truckwys_net_by_customer(self, as_of):
        """Per customer: outstanding minus credits at `as_of` (the ageing rule)."""
        out = {}
        for inv in Invoice.objects.filter(company=self.company, issue_date__lte=as_of).exclude(
                status__in=['DRAFT', 'CANCELLED']):
            paid = inv.payments.filter(payment_date__lte=as_of).aggregate(t=Sum('amount'))['t'] or ZERO
            cred = (CreditNote.objects.filter(invoice=inv, issue_date__lte=as_of, status='ISSUED')
                    .aggregate(t=Sum('total_amount'))['t'] or ZERO)
            out[inv.customer_id] = out.get(inv.customer_id, ZERO) + inv.total_amount - paid - cred
        return out

    def assert_clean_reconciliation(self, msg=''):
        run = reconciliation.run(self.conn)
        diffs = [(d.scope, d.key, d.field, d.truckwys_value, d.provider_value) for d in run.differences.all()]
        self.assertEqual((run.status, diffs), ('OK', []), msg)
        return run

    # ------------------------------------------------------------ tests
    def test_initial_sync_pushes_exactly_the_issued_documents(self):
        status = backfill.status(self.conn)
        self.assertEqual(status['state'], 'DONE', status)
        self.assertTrue(all(s['state'] == 'DONE' for s in status['steps']), status['steps'])
        issued = Invoice.objects.filter(company=self.company, status__in=Invoice.ISSUED_STATUSES)
        links = ExternalLink.objects.filter(connection=self.conn, object_type='INVOICE')
        self.assertEqual(links.filter(status='SYNCED').count(), issued.count())
        self.assertEqual(links.exclude(status='SYNCED').count(), 0)
        xero_accrec = [i for i in self.xero.org()['invoices'].values()
                       if i['Type'] == 'ACCREC' and i['Status'] not in ('DELETED',)]
        self.assertEqual(len(xero_accrec), issued.count())
        self.assertEqual({i['InvoiceNumber'] for i in xero_accrec}, set(issued.values_list('invoice_number', flat=True)))
        # Voided before connecting (INV12/25/41) and drafts never reach Xero; nor does the voided CN04.
        for key in ('INV11', 'INV12', 'INV24', 'INV25', 'INV38', 'INV41'):
            self.assertIsNone(self.xero.find_invoice(self.golden.invoices[key].invoice_number), key)
        cns = CreditNote.objects.filter(company=self.company, status='ISSUED')
        self.assertEqual(ExternalLink.objects.filter(connection=self.conn, object_type='CREDIT_NOTE',
                                                     status='SYNCED').count(), cns.count())
        bills = Expense.objects.filter(company=self.company, supplier__isnull=False).exclude(status='REJECTED')
        self.assertEqual(ExternalLink.objects.filter(connection=self.conn, object_type='BILL',
                                                     status='SYNCED').count(), bills.count())
        # Every receipt recorded in TruckWys is now a Xero payment.
        self.assertFalse(Payment.objects.filter(company=self.company, source__in=('MANUAL', 'BANK')).exists())
        # Manual payments are refused from now on.
        api = APIClient(HTTP_HOST='localhost')
        api.force_authenticate(self.golden.user)
        resp = api.post('/api/v1/payments/', {'invoice': self.golden.invoices['INV04'].pk, 'amount': '1.00',
                                              'payment_date': '2026-09-30', 'payment_method': 'EFT'}, format='json')
        self.assertEqual(resp.status_code, 409)

    def test_truckwys_reports_are_unchanged_by_the_sync(self):
        for key, (start, end) in self.periods.items():
            want = self.exp['periods'][key]
            with self.subTest(period=key):
                s = reports.sales(self.company, start, end)
                self.assertEqual(m(s['revenue_excl_vat']), m(want['revenue_excl_vat']))
                self.assertEqual(m(s['output_vat']), m(want['output_vat']))
                c = reports.cash(self.company, start, end)
                self.assertEqual(m(c['cash_received_incl_vat']), m(want['cash_received_incl_vat']))
                self.assertEqual(m(c['cash_revenue_excl_vat']), m(want['cash_revenue_excl_vat']))
                self.assertEqual(m(c['overpayments']), m(want['overpayments']))
        for as_of, want in self.exp['ageing'].items():
            with self.subTest(ageing=as_of):
                a = reports.debtors_ageing(self.company, date.fromisoformat(as_of))
                self.assertEqual(m(a['total']), m(want['total']))
                self.assertEqual(m(a['customer_credits']), m(want['customer_credits']))

    def test_xero_tax_totals_match_the_vat201_split(self):
        for key, (start, end) in self.periods.items():
            want = self.exp['periods'][key]
            totals = {'sales': self.xero.tax_totals(start, end),
                      'purchases': self.xero.tax_totals(start, end, 'ACCPAY')}
            with self.subTest(period=key):
                for code, row in want['output_by_code'].items():
                    got = totals['sales'].get(SALES_TAX[code], {'net': ZERO, 'tax': ZERO})
                    self.assertEqual((m(got['net']), m(got['tax'])), (m(row['net']), m(row['vat'])), code)
                xero_output = sum((m(v['tax']) for v in totals['sales'].values()), ZERO)
                self.assertEqual(xero_output, m(want['output_vat']))
                _net, input_vat = self.supplier_expenses(start, end)
                xero_input = sum((m(v['tax']) for v in totals['purchases'].values()), ZERO)
                self.assertEqual(xero_input, m(input_vat))
                # Purchases by tax type too.
                for code in PURCHASE_TAX:
                    qs = (Expense.objects.filter(company=self.company, supplier__isnull=False, tax_code=code,
                                                 expense_date__gte=start, expense_date__lte=end)
                          .exclude(status='REJECTED'))
                    vat = qs.aggregate(t=Sum('vat_amount'))['t'] or ZERO
                    got = totals['purchases'].get(PURCHASE_TAX[code], {'tax': ZERO})
                    self.assertEqual(m(got['tax']), m(vat), code)

    def test_xero_profit_and_loss_matches(self):
        for key, (start, end) in self.periods.items():
            want = self.exp['periods'][key]
            report = self.xero.profit_and_loss(start, end)
            pl = {'income': sum(report['income'].values(), ZERO) + sum(report['other_income'].values(), ZERO),
                  'expenses': sum(report['cost_of_sales'].values(), ZERO) + sum(report['expenses'].values(), ZERO)}
            with self.subTest(period=key):
                self.assertEqual(m(pl['income']), m(want['revenue_excl_vat']))
                # Every line of the dataset is freight -> account 200.
                self.assertEqual(m(report['income'].get('200')), m(want['revenue_excl_vat']))
                net, _vat = self.supplier_expenses(start, end)
                self.assertEqual(m(pl['expenses']), m(net))
                for cat, code in (('FUEL', '449'), ('SUBCONTRACTOR', '478'), ('TOLLS', '450')):
                    qs = (Expense.objects.filter(company=self.company, supplier__isnull=False, category=cat,
                                                 expense_date__gte=start, expense_date__lte=end)
                          .exclude(status='REJECTED'))
                    want_cat = sum((e.amount - e.vat_amount for e in qs), ZERO)
                    got = report['cost_of_sales'].get(code, ZERO) + report['expenses'].get(code, ZERO)
                    self.assertEqual(m(got), m(want_cat), cat)
                # Documented difference: expenses without a supplier stay in TruckWys.
                no_supplier = sum((e.amount - e.vat_amount for e in Expense.objects.filter(
                    company=self.company, supplier__isnull=True, expense_date__gte=start,
                    expense_date__lte=end).exclude(status='REJECTED')), ZERO)
                self.assertEqual(m(want['expenses_excl_vat']) - m(pl['expenses']), m(no_supplier))

    def test_xero_receivables_match_ageing(self):
        for as_of, want in self.exp['ageing'].items():
            d = date.fromisoformat(as_of)
            ours = self.truckwys_net_by_customer(d)
            theirs = self.xero.aged_receivables(d)
            with self.subTest(as_of=as_of):
                for cust in Customer.objects.filter(company=self.company):
                    self.assertEqual(m(theirs.get(self.contact_for(cust), ZERO)), m(ours.get(cust.pk, ZERO)), cust.name)
                self.assertEqual(m(self.xero.balance_sheet_ar(d)), m(want['total']) - m(want['customer_credits']))

    def test_reconciliation_is_clean_and_stays_clean(self):
        self.assert_clean_reconciliation('right after the initial sync')

        def poll():
            self.conn.refresh_from_db()
            pull.poll_payments(self.conn)

        # A customer pays an open invoice in Xero.
        inv04 = self.golden.invoices['INV04']
        x04 = ExternalLink.objects.get(connection=self.conn, object_type='INVOICE', local_id=inv04.pk).external_id
        self.xero.now = datetime.now(dt_timezone.utc) + timedelta(minutes=10)
        pid = self.xero.record_payment(x04, D('7781.88'), date(2026, 9, 29))
        poll()
        inv04.refresh_from_db()
        self.assertEqual(inv04.balance, D('10000.00'))
        self.assert_clean_reconciliation('after a payment in Xero')

        # ... and it is deleted again (bounced).
        self.xero.now += timedelta(minutes=10)
        self.xero.delete_payment(pid)
        poll()
        inv04.refresh_from_db()
        self.assertEqual(inv04.balance, inv04.total_amount)
        self.assert_clean_reconciliation('after the payment was deleted')

        # The R56.83 INV08 overpayment is used against another invoice of the
        # same customer in the same month it was received... (C05)
        inv08 = self.golden.invoices['INV08']
        rem = Payment.objects.get(invoice=inv08, external_id__startswith='OVPREM:')
        ovp_id = rem.external_id.split(':', 1)[1]
        other = (Invoice.objects.filter(company=self.company, customer=inv08.customer, balance__gt=0)
                 .exclude(pk=inv08.pk).order_by('issue_date').first())
        self.assertIsNotNone(other)
        xo = ExternalLink.objects.get(connection=self.conn, object_type='INVOICE', local_id=other.pk).external_id
        self.xero.now += timedelta(minutes=10)
        self.xero.allocate('OVERPAYMENT', ovp_id, xo, D('56.83'), date(2026, 9, 30))
        poll()
        self.assertFalse(Payment.objects.filter(pk=rem.pk).exists())
        alloc = Payment.objects.get(invoice=other, external_id__startswith=f'OVP:{ovp_id}:')
        self.assertEqual(alloc.amount, D('56.83'))
        # Documented difference: TruckWys dates that money by the allocation
        # (30 Sep); Xero by the receipt (27 Jul), so Xero also shows it as a
        # customer credit at the end of July and August. Nothing else differs.
        run = reconciliation.run(self.conn)
        diffs = {(d.key, d.field): d.difference for d in run.differences.all()}
        self.assertEqual(diffs, {('2026-07', 'receipts'): D('-56.83'), ('2026-09', 'receipts'): D('56.83'),
                                 ('2026-07', 'debtors'): D('56.83'), ('2026-08', 'debtors'): D('56.83')})
