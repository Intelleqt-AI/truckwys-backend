"""The foundation golden dataset, pushed through the fake QuickBooks Online ledger.

core/tests/fixtures/golden_ledger.json is replayed into TruckWys exactly as
test_golden_ledger does (50 invoices, every tax code, discounts, partial and
full credit notes, a voided credit note, voided invoices, an overpayment,
edited and deleted payments, 21 expenses). Then QBO is connected, mapped and
the initial sync runs from 1 July 2026.

Asserted to the cent, per month and for the quarter:
  * QBO tax per tax code (from the TaxLines) == TruckWys VAT201 split
    (frozen `expected`), and input tax == TruckWys input VAT on supplier
    expenses, by code
  * QBO P&L income (via the Items' income account) == TruckWys revenue excl.
    VAT; expense accounts == TruckWys supplier expenses excl. VAT
  * QBO receivables per customer and Balance Sheet A/R (15 Aug, 30 Sep)
    == TruckWys debtors ageing, net of credits
  * TruckWys' own reports still equal `expected` after the sync
  * reconciliation: zero differences, also after payments, deletions and
    credit applications are made in QBO.
  * QBO's per-rate tax rule WOULD differ from TruckWys on documents of this
    dataset; the TxnTaxDetail override is what keeps them equal.
Documented differences (docs/integrations/QUICKBOOKS.md §4) are asserted as such.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Sum
from django.test import TestCase
from rest_framework.test import APIClient

from core.accounting import backfill, pull, reconciliation
from core.accounting.http import use_transport
from core.models import CreditNote, Customer, Expense, ExternalLink, Invoice, Payment
from core.services import accounting_reports as reports
from core.tests.accounting.fake_qbo import FakeQBO
from core.tests.accounting.qbo_helpers import (
    PURCHASE_TAX, SALES_TAX, connect, map_everything, no_commit_delay, qbo_settings,
)
from core.tests.golden_loader import create_golden_company, load_golden_dataset

D = Decimal
ZERO = D('0.00')


def m(v):
    return D(v or 0).quantize(D('0.01'))


class QBOGoldenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = create_golden_company()
        cls.golden = load_golden_dataset(cls.company)
        cls.exp = cls.golden.expected
        cls.periods = {p['key']: (date.fromisoformat(p['start']), date.fromisoformat(p['end']))
                       for p in cls.golden.dataset['periods']}

    def setUp(self):
        self._settings = qbo_settings()
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.qbo = FakeQBO(companies=[{'realm': '9130350000000001', 'name': 'Golden Haulage (Pty) Ltd'}],
                           verifier_token='test-verifier-token')
        t = use_transport(self.qbo)
        t.__enter__()
        self.addCleanup(t.__exit__, None, None, None)
        self.conn, outcome = connect(self.company, self.golden.user, self.qbo)
        self.assertEqual(outcome, 'connected')
        map_everything(self.conn)
        with no_commit_delay(self):
            backfill.start(self.conn, '2026-07-01', self.golden.user)
        self.conn.refresh_from_db()

    # ------------------------------------------------------------ helpers
    def contact_for(self, customer):
        return ExternalLink.objects.get(connection=self.conn, object_type='CONTACT_CUSTOMER',
                                        local_id=customer.pk).external_id

    def qid(self, inv):
        return ExternalLink.objects.get(connection=self.conn, object_type='INVOICE', local_id=inv.pk).external_id

    def supplier_expenses(self, start, end, **filters):
        qs = (Expense.objects.filter(company=self.company, supplier__isnull=False, expense_date__gte=start,
                                     expense_date__lte=end, **filters).exclude(status='REJECTED'))
        gross = qs.aggregate(t=Sum('amount'))['t'] or ZERO
        vat = qs.aggregate(t=Sum('vat_amount'))['t'] or ZERO
        return gross - vat, vat

    def truckwys_net_by_customer(self, as_of):
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

    def poll(self):
        self.conn.refresh_from_db()
        pull.poll_payments(self.conn)

    # ------------------------------------------------------------ tests
    def test_initial_sync_pushes_exactly_the_issued_documents(self):
        status = backfill.status(self.conn)
        self.assertEqual(status['state'], 'DONE', status)
        self.assertTrue(all(s['state'] == 'DONE' for s in status['steps']), status['steps'])
        issued = Invoice.objects.filter(company=self.company, status__in=Invoice.ISSUED_STATUSES)
        links = ExternalLink.objects.filter(connection=self.conn, object_type='INVOICE')
        self.assertEqual(links.filter(status='SYNCED').count(), issued.count())
        self.assertEqual(links.exclude(status='SYNCED').count(), 0)
        qbo_invoices = self.qbo.invoices()
        self.assertEqual({i['DocNumber'] for i in qbo_invoices}, set(issued.values_list('invoice_number', flat=True)))
        self.assertEqual(len(qbo_invoices), issued.count())
        for key in ('INV11', 'INV12', 'INV24', 'INV25', 'INV38', 'INV41'):
            self.assertIsNone(self.qbo.find_invoice(self.golden.invoices[key].invoice_number), key)
        cns = CreditNote.objects.filter(company=self.company, status='ISSUED')
        self.assertEqual(len(self.qbo.credit_memos()), cns.count())
        bills = Expense.objects.filter(company=self.company, supplier__isnull=False).exclude(status='REJECTED')
        self.assertEqual(len(self.qbo.bills()), bills.count())
        self.assertFalse(Payment.objects.filter(company=self.company, source__in=('MANUAL', 'BANK')).exists())
        # Every invoice and credit memo carries our number and our totals.
        for inv in issued:
            q = self.qbo.find_invoice(inv.invoice_number)
            self.assertEqual((m(q['TotalAmt']), m(q['TxnTaxDetail']['TotalTax'])), (inv.total_amount, inv.vat_amount),
                             inv.invoice_number)
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

    def test_qbo_tax_by_code_matches_the_vat201_split(self):
        for key, (start, end) in self.periods.items():
            want = self.exp['periods'][key]
            sales = self.qbo.tax_totals(start, end)
            purchases = self.qbo.tax_totals(start, end, 'purchases')
            with self.subTest(period=key):
                for code, row in want['output_by_code'].items():
                    got = sales.get(SALES_TAX[code], {'net': ZERO, 'tax': ZERO})
                    self.assertEqual((m(got['net']), m(got['tax'])), (m(row['net']), m(row['vat'])), code)
                self.assertEqual(sum((m(v['tax']) for v in sales.values()), ZERO), m(want['output_vat']))
                _net, input_vat = self.supplier_expenses(start, end)
                self.assertEqual(sum((m(v['tax']) for v in purchases.values()), ZERO), m(input_vat))
                for code in PURCHASE_TAX:
                    _n, vat = self.supplier_expenses(start, end, tax_code=code)
                    self.assertEqual(m(purchases.get(PURCHASE_TAX[code], {'tax': ZERO})['tax']), m(vat), code)

    def test_per_rate_rounding_and_the_override(self):
        """QBO taxes the summed net per rate; TruckWys rounds per line. None
        of the dataset's 50 invoices happens to differ, so the override is a
        guarantee here, not a correction; a document where the rules do
        differ (3 x 10.10 @ 15 %: per line 4.56, per rate 4.55) is kept at
        TruckWys' figure only because of the TaxLine amounts."""
        def per_rate(q):
            return sum(((D(tl['TaxLineDetail']['NetAmountTaxable']) * D(tl['TaxLineDetail']['TaxPercent']) / 100)
                        .quantize(D('0.01'), rounding=ROUND_HALF_UP) for tl in q['TxnTaxDetail']['TaxLine']), ZERO)
        self.assertEqual([q['DocNumber'] for q in self.qbo.invoices() + self.qbo.credit_memos()
                          if per_rate(q) != m(q['TxnTaxDetail']['TotalTax'])], [])
        from core.services.invoice_lines import apply_lines
        from core.services.numbering import provisional_number
        inv01 = self.golden.invoices['INV01']

        def issue():
            inv = Invoice(company=self.company, customer=inv01.customer, invoice_number=provisional_number(),
                          issue_date=date(2026, 9, 30), due_date=date(2026, 10, 30), status='DRAFT',
                          subtotal=0, total_amount=0, balance=0)
            apply_lines(inv, [{'description': f'Admin fee {i}', 'quantity': '1', 'unit_price': '10.10',
                               'tax_code': 'STANDARD'} for i in range(3)])
            with no_commit_delay(self):
                inv.mark_as_sent()
            inv.refresh_from_db()
            return inv
        inv = issue()
        q = self.qbo.find_invoice(inv.invoice_number)
        self.assertEqual((inv.vat_amount, m(q['TxnTaxDetail']['TotalTax']), per_rate(q)),
                         (D('4.56'), D('4.56'), D('4.55')))
        self.qbo.honour_tax_override = False
        inv2 = issue()
        link = ExternalLink.objects.get(connection=self.conn, object_type='INVOICE', local_id=inv2.pk)
        self.assertEqual(link.status, 'DEAD')
        self.assertIn('VAT TruckWys 4.56 vs 4.55', link.last_error)
        self.assertIsNone(self.qbo.find_invoice(inv2.invoice_number))

    def test_qbo_profit_and_loss_matches(self):
        for key, (start, end) in self.periods.items():
            want = self.exp['periods'][key]
            report = self.qbo.profit_and_loss(start, end)
            income = sum(report['income'].values(), ZERO) + sum(report['other_income'].values(), ZERO)
            expenses = sum(report['cost_of_sales'].values(), ZERO) + sum(report['expenses'].values(), ZERO)
            with self.subTest(period=key):
                self.assertEqual(m(income), m(want['revenue_excl_vat']))
                # Every line of the dataset is freight -> item 1 -> income account 10.
                self.assertEqual(m(report['income'].get('10')), m(want['revenue_excl_vat']))
                net, _vat = self.supplier_expenses(start, end)
                self.assertEqual(m(expenses), m(net))
                for cat, acc in (('FUEL', '20'), ('SUBCONTRACTOR', '24'), ('TOLLS', '21')):
                    want_cat, _v = self.supplier_expenses(start, end, category=cat)
                    got = report['cost_of_sales'].get(acc, ZERO) + report['expenses'].get(acc, ZERO)
                    self.assertEqual(m(got), m(want_cat), cat)
                no_supplier = sum((e.amount - e.vat_amount for e in Expense.objects.filter(
                    company=self.company, supplier__isnull=True, expense_date__gte=start,
                    expense_date__lte=end).exclude(status='REJECTED')), ZERO)
                self.assertEqual(m(want['expenses_excl_vat']) - m(expenses), m(no_supplier))

    def test_qbo_receivables_match_ageing(self):
        for as_of, want in self.exp['ageing'].items():
            d = date.fromisoformat(as_of)
            ours = self.truckwys_net_by_customer(d)
            theirs = self.qbo.aged_receivables(d)
            with self.subTest(as_of=as_of):
                for cust in Customer.objects.filter(company=self.company):
                    self.assertEqual(m(theirs.get(self.contact_for(cust), ZERO)), m(ours.get(cust.pk, ZERO)),
                                     cust.name)
                self.assertEqual(m(self.qbo.balance_sheet_ar(d)), m(want['total']) - m(want['customer_credits']))
                # The adapter reads the same figure from the BalanceSheet report.
                from core.accounting.registry import get_adapter
                self.assertEqual(get_adapter(self.conn).debtors_at(d), m(want['total']) - m(want['customer_credits']))

    def test_reconciliation_is_clean_and_stays_clean(self):
        self.assert_clean_reconciliation('right after the initial sync')

        def later():
            self.qbo.now = datetime.now(dt_timezone.utc) + timedelta(minutes=10 + len(self.qbo.calls) % 7)

        # A customer pays an open invoice in QBO.
        inv04 = self.golden.invoices['INV04']
        later()
        pid = self.qbo.record_payment({self.qid(inv04): D('7781.88')}, date(2026, 9, 29))
        self.poll()
        inv04.refresh_from_db()
        self.assertEqual(inv04.balance, D('10000.00'))
        self.assert_clean_reconciliation('after a payment in QBO')

        # ... and it is deleted again (bounced).
        self.qbo.advance(minutes=10)
        self.qbo.delete_payment(pid)
        self.poll()
        inv04.refresh_from_db()
        self.assertEqual(inv04.balance, inv04.total_amount)
        self.assert_clean_reconciliation('after the payment was deleted')

        # One payment for two invoices of the same customer.
        open_by_customer = {}
        for inv in Invoice.objects.filter(company=self.company, balance__gt=100,
                                          status__in=Invoice.ISSUED_STATUSES).order_by('issue_date', 'pk'):
            open_by_customer.setdefault(inv.customer_id, []).append(inv)
        first, second = next(v for v in open_by_customer.values() if len(v) >= 2)[:2]
        self.qbo.advance(minutes=10)
        self.qbo.record_payment({self.qid(first): D('100.00'), self.qid(second): D('58.33')}, date(2026, 9, 30))
        self.poll()
        self.assert_clean_reconciliation('after one payment for two invoices')

        # A credit memo raised in QBO and applied to an invoice: imported.
        inv49 = self.golden.invoices['INV49']
        self.qbo.advance(minutes=10)
        cm = self.qbo.create_credit_memo(self.contact_for(inv49.customer), [FakeQBO.sales_line(D('100.00'))],
                                         date(2026, 9, 30), 'QCM-9')
        self.qbo.apply_credit(cm, self.qid(inv49), D('115.00'), date(2026, 9, 30))
        self.poll()
        self.assertTrue(CreditNote.objects.filter(invoice=inv49, source='QBO', external_id=cm).exists())
        self.assert_clean_reconciliation('after a credit memo raised in QBO')

        # The R56.83 INV08 overpayment (an unapplied QBO payment since the
        # initial sync) is used against another open invoice of the customer.
        inv08 = self.golden.invoices['INV08']
        rem = Payment.objects.get(invoice=inv08, external_id__startswith='OVPREM:')
        ovp_id = rem.external_id.split(':', 1)[1]
        self.assertEqual(D(self.qbo.payment(ovp_id)['UnappliedAmt']), D('56.83'))
        other = (Invoice.objects.filter(company=self.company, customer=inv08.customer, balance__gt=0)
                 .exclude(pk=inv08.pk).order_by('issue_date').first())
        self.assertIsNotNone(other)
        self.qbo.advance(minutes=10)
        self.qbo.apply_payment(ovp_id, self.qid(other), D('56.83'))
        self.poll()
        self.assertFalse(Payment.objects.filter(pk=rem.pk).exists())
        applied = Payment.objects.get(invoice=other, external_id=f'{ovp_id}:{self.qid(other)}')
        # QBO keeps the payment's own date (27 Jul): unlike Xero there is no
        # separate allocation date, so TruckWys dates the money by the receipt.
        self.assertEqual((applied.amount, applied.payment_date), (D('56.83'), date(2026, 7, 27)))
        # Unlike Xero (where TruckWys dates it by the allocation, 30 Sep, and
        # receipts / debtors differ for July and August), QBO and TruckWys
        # now agree to the cent: the money is dated 27 Jul on both sides and
        # INV16 was already issued (31 Jul) at the end of July. See
        # QUICKBOOKS.md §4 for the one case that would still differ.
        self.assertEqual(other.invoice_number, self.golden.invoices['INV16'].invoice_number)
        self.assert_clean_reconciliation('after the overpayment was applied in QBO')
