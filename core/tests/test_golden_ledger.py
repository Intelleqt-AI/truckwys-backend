"""Golden ledger: the Q3 2026 dataset in core/tests/fixtures/golden_ledger.json
replayed through the real services must reproduce, to the cent, the figures
the independent reference calculator (core/tests/golden_reference.py)
derived from first principles and froze into the JSON.

The same dataset + loader (core/tests/golden_loader.py) is meant to be
replayed into Xero / QuickBooks to prove the syncs report identical figures.
"""
import copy
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.services import accounting_reports as reports
from core.tests import golden_reference as ref
from core.tests.golden_loader import (
    FIXTURE_PATH, create_golden_company, load_dataset, load_golden_dataset,
)

D = Decimal
ZERO = D('0.00')


def m(value):
    """Money as the dataset spells it (2 dp string); None stays None."""
    if value is None:
        return None
    return str(D(value).quantize(D('0.01')))


class GoldenReferenceTests(SimpleTestCase):
    """The frozen `expected` block is exactly what the reference derives."""

    def setUp(self):
        self.ds = load_dataset()

    def test_frozen_expected_matches_reference(self):
        derived = ref.compute_expected(self.ds)
        self.assertEqual(json_norm(derived), json_norm(self.ds['expected']),
                         'The fixture changed without re-freezing: run '
                         'python core/tests/golden_reference.py --freeze ' + FIXTURE_PATH)

    def test_dataset_shape(self):
        ds = self.ds
        self.assertEqual(len(ds['customers']), 8)
        self.assertEqual(len(ds['suppliers']), 5)
        self.assertEqual(len(ds['invoices']), 50)
        self.assertGreaterEqual(len(ds['expenses']), 20)
        terms = {c['payment_terms'] for c in ds['customers']}
        self.assertTrue({'NET7', 'NET14', 'NET30', 'NET45', 'NET60', 'NET90'} <= terms)
        codes = {l['tax_code'] for i in ds['invoices'] for l in i['lines']}
        self.assertEqual(codes, {'STANDARD', 'ZERO_RATED', 'EXEMPT', 'NO_VAT'})

    def test_reference_refuses_an_invalid_fixture(self):
        ds = copy.deepcopy(self.ds)
        p = next(e for e in ds['events'] if e['type'] == 'payment' and e['id'] == 'P04')
        p['allow_overpayment'] = False   # the bank overpayment without the flag
        with self.assertRaises(ref.FixtureError):
            ref.Book(ds)

    def test_hand_checked_examples(self):
        """Worked by hand, independently of both the app and the reference."""
        inv = self.ds['expected']['invoices']

        # INV02  L1: 3 x 33.335 = 100.005; 10% = 10.0005; 100.005 - 10.0005 = 90.0045 -> 90.00
        #            VAT 90.00 x 15% = 13.50 -> line total 103.50
        #        L2: 1 x 10.10 = 10.10; VAT 1.515 -> 1.52 (half-up) -> 11.62
        #        subtotal 100.10, VAT 15.02, total 115.12; discount round2(100.005) - 90.00 = 100.01 - 90.00 = 10.01
        self.assertEqual([inv['INV02'][k] for k in ('subtotal', 'discount', 'vat_amount', 'total_amount')],
                         ['100.10', '10.01', '15.02', '115.12'])

        # INV04  L1: 1.5 x 9800 = 14700.00 - 250.00 = 14450.00; VAT 2167.50
        #        L2: 2.25 x 450 = 1012.50; VAT 151.875 -> 151.88
        #        subtotal 15462.50, VAT 2319.38, total 17781.88. Never paid; due 2026-07-06 + 45 = 2026-08-20,
        #        so at 2026-09-30 it is 41 days past due -> 31_60 bucket, status OVERDUE.
        self.assertEqual([inv['INV04'][k] for k in ('subtotal', 'vat_amount', 'total_amount', 'due_date', 'status')],
                         ['15462.50', '2319.38', '17781.88', '2026-08-20', 'OVERDUE'])
        self.assertEqual(self.ds['expected']['ageing']['2026-09-30']['invoices']['INV04'],
                         {'outstanding': '17781.88', 'days_past_due': 41, 'bucket': '31_60'})

        # INV08  4545.45 x 85% = 3863.6325 -> 3863.63; VAT 579.5445 -> 579.54; total 4443.17.
        #        Bank paid 4500.00 -> balance -56.83 (customer credit), status PAID; the 56.83 is overpayment,
        #        cash revenue share = 4443.17 x 3863.63 / 4443.17 = 3863.63.
        self.assertEqual([inv['INV08'][k] for k in ('subtotal', 'vat_amount', 'total_amount', 'balance', 'status')],
                         ['3863.63', '579.54', '4443.17', '-56.83', 'PAID'])

        # INV13  15000.00 VAT 2250.00; 1275.50 VAT 191.325 -> 191.33; total 18716.83.
        #        CN02 (Aug) credits the surcharge line 1275.50 + 191.33 = 1466.83; paid 17250.00 in Sept -> 0.00.
        self.assertEqual([inv['INV13'][k] for k in ('total_amount', 'credited_amount', 'paid_amount', 'balance')],
                         ['18716.83', '1466.83', '17250.00', '0.00'])

        # July 2026 output VAT, STANDARD lines of issued invoices (INV11 draft, INV12 void excluded):
        #   INV01 18500.00 -> 2775.00 | INV02 90.00 -> 13.50, 10.10 -> 1.52 | INV03 1850.00 -> 277.50
        #   INV04 14450.00 -> 2167.50, 1012.50 -> 151.88 | INV05 12000.00 -> 1800.00
        #   INV06 2 x 16750 = 33500 less 5% = 31825.00 -> 4773.75 | INV07 8800.00 -> 1320.00
        #   INV08 3863.63 -> 579.54 | INV09 23000.00 -> 3450.00 | INV10 3150.00 -> 472.50
        #   INV13 15000.00 -> 2250.00, 1275.50 -> 191.33 | INV15 5000.00 -> 750.00 | INV16 100.00 -> 15.00
        #   net 139926.73, VAT 20989.02
        #   less CN01 (INV05 full credit, 2026-07-20): STANDARD 12000.00 / 1800.00
        #   => STANDARD net 127926.73, output VAT 19189.02
        # Input VAT July: E02 1150 x 15/115 = 150.00; E04 stated 3000.00; E06 999.99 x 15/115 = 130.433 -> 130.43
        #   => 3280.43; net VAT payable 19189.02 - 3280.43 = 15908.59
        july = self.ds['expected']['periods']['2026-07']
        self.assertEqual(july['output_by_code']['STANDARD'], {'net': '127926.73', 'vat': '19189.02'})
        self.assertEqual((july['output_vat'], july['input_vat'], july['net_vat_payable']),
                         ('19189.02', '3280.43', '15908.59'))


def json_norm(obj):
    import json
    return json.loads(json.dumps(obj))


class GoldenLedgerTests(TestCase):
    """Replay through the services, then compare every figure."""

    @classmethod
    def setUpTestData(cls):
        cls.company = create_golden_company()
        cls.golden = load_golden_dataset(cls.company)
        cls.exp = cls.golden.expected

    def period_args(self, key):
        p = next(p for p in self.golden.dataset['periods'] if p['key'] == key)
        return date.fromisoformat(p['start']), date.fromisoformat(p['end'])

    def test_sales_and_output_vat(self):
        for key, want in self.exp['periods'].items():
            start, end = self.period_args(key)
            with self.subTest(period=key):
                s = reports.sales(self.company, start, end)
                self.assertEqual(s['invoice_count'], want['invoice_count'])
                self.assertEqual(s['credit_note_count'], want['credit_note_count'])
                for f in ('invoiced_excl_vat', 'credited_excl_vat', 'revenue_excl_vat', 'output_vat',
                          'invoiced_incl_vat', 'credited_incl_vat'):
                    self.assertEqual(m(s[f]), want[f], f)
                by_code = reports.output_vat_by_code(self.company, start, end)
                got = {c: {'net': m(by_code.get(c, {}).get('net', ZERO)),
                           'vat': m(by_code.get(c, {}).get('vat', ZERO))} for c in ref.TAX_CODES}
                self.assertEqual(got, json_norm(want['output_by_code']))

    def test_expenses_and_input_vat(self):
        for key, want in self.exp['periods'].items():
            start, end = self.period_args(key)
            with self.subTest(period=key):
                e = reports.expenses(self.company, start, end)
                self.assertEqual(e['count'], want['expense_count'])
                self.assertEqual(m(e['expenses_incl_vat']), want['expenses_incl_vat'])
                self.assertEqual(m(e['input_vat']), want['input_vat'])
                self.assertEqual(m(e['expenses_excl_vat']), want['expenses_excl_vat'])
                self.assertEqual({k: m(v) for k, v in e['by_category_excl_vat'].items()},
                                 json_norm(want['expenses_by_category_excl_vat']))

    def test_vat_summary(self):
        for key, want in self.exp['periods'].items():
            start, end = self.period_args(key)
            with self.subTest(period=key):
                v = reports.vat_summary(self.company, start, end)
                self.assertEqual((m(v['output_vat']), m(v['input_vat']), m(v['net_vat_payable'])),
                                 (want['output_vat'], want['input_vat'], want['net_vat_payable']))

    def test_cash(self):
        for key, want in self.exp['periods'].items():
            start, end = self.period_args(key)
            with self.subTest(period=key):
                c = reports.cash(self.company, start, end)
                self.assertEqual(c['payment_count'], want['payment_count'])
                self.assertEqual(m(c['cash_received_incl_vat']), want['cash_received_incl_vat'])
                self.assertEqual(m(c['cash_revenue_excl_vat']), want['cash_revenue_excl_vat'])
                self.assertEqual(m(c['overpayments']), want['overpayments'])

    def test_profit_and_loss_both_bases(self):
        for key, want in self.exp['periods'].items():
            start, end = self.period_args(key)
            with self.subTest(period=key, basis='accrual'):
                p = reports.profit_and_loss(self.company, start, end, basis='accrual')
                self.assertEqual((m(p['revenue_excl_vat']), m(p['expenses_excl_vat']), m(p['profit_excl_vat']),
                                  m(p['margin_pct'])),
                                 (want['revenue_excl_vat'], want['expenses_excl_vat'], want['profit_excl_vat'],
                                  want['margin_pct']))
            with self.subTest(period=key, basis='cash'):
                p = reports.profit_and_loss(self.company, start, end, basis='cash')
                self.assertEqual((m(p['revenue_excl_vat']), m(p['profit_excl_vat']), m(p['margin_pct'])),
                                 (want['cash_revenue_excl_vat'], want['profit_cash_basis_excl_vat'],
                                  want['margin_pct_cash_basis']))

    def test_quarter_is_sum_of_months(self):
        q = self.exp['periods']['2026-Q3']
        months = [self.exp['periods'][k] for k in ('2026-07', '2026-08', '2026-09')]
        for f in ('revenue_excl_vat', 'output_vat', 'input_vat', 'expenses_excl_vat', 'cash_received_incl_vat',
                  'cash_revenue_excl_vat', 'profit_excl_vat', 'profit_cash_basis_excl_vat'):
            self.assertEqual(sum((D(mo[f]) for mo in months), ZERO), D(q[f]), f)

    def test_debtors_ageing(self):
        for as_of, want in self.exp['ageing'].items():
            with self.subTest(as_of=as_of):
                a = reports.debtors_ageing(self.company, date.fromisoformat(as_of))
                self.assertEqual({b: m(v) for b, v in a['buckets'].items()}, json_norm(want['buckets']))
                self.assertEqual(m(a['total']), want['total'])
                self.assertEqual(m(a['customer_credits']), want['customer_credits'])
                cust_ids = {c.pk: cid for cid, c in self.golden.customers.items()}
                got_cust = {cust_ids[c['customer_id']]: {k: m(c[k]) for k in ref.BUCKETS + ('total',)}
                            for c in a['customers']}
                self.assertEqual(got_cust, json_norm(want['customers']))
                inv_ids = {i.pk: iid for iid, i in self.golden.invoices.items()}
                got_rows = {inv_ids[r['invoice_id']]: {'outstanding': m(r['outstanding']),
                                                       'days_past_due': r['days_past_due'], 'bucket': r['bucket']}
                            for r in a['invoices']}
                self.assertEqual(got_rows, json_norm(want['invoices']))

    def test_every_invoice_final_state(self):
        for iid, want in self.exp['invoices'].items():
            inv = self.golden.invoices[iid]
            inv.refresh_from_db()
            with self.subTest(invoice=iid):
                got = {
                    'due_date': inv.due_date.isoformat(), 'terms_days': inv.terms_days,
                    'subtotal': m(inv.subtotal), 'discount': m(inv.discount), 'vat_amount': m(inv.vat_amount),
                    'total_amount': m(inv.total_amount), 'paid_amount': m(inv.paid_amount),
                    'credited_amount': m(inv.credited_amount), 'balance': m(inv.balance), 'status': inv.status,
                    'lines': [{'net': m(l.net_amount), 'vat': m(l.vat_amount), 'total': m(l.total_amount)}
                              for l in inv.lines.order_by('position', 'id')],
                }
                self.assertEqual(got, json_norm(want))

    def test_credit_notes_and_expenses_rows(self):
        for cid, want in self.exp['credit_notes'].items():
            cn = self.golden.credit_notes[cid]
            cn.refresh_from_db()
            with self.subTest(credit_note=cid):
                self.assertEqual({'status': cn.status, 'subtotal': m(cn.subtotal), 'vat_amount': m(cn.vat_amount),
                                  'total_amount': m(cn.total_amount)}, json_norm(want))
        for eid, want in self.exp['expenses'].items():
            e = self.golden.expenses[eid]
            with self.subTest(expense=eid):
                self.assertEqual({'amount': m(e.amount), 'vat_amount': m(e.vat_amount),
                                  'net_amount': m(e.net_amount)}, json_norm(want))

    def test_drafts_and_voids_leave_no_trace(self):
        from core.models import Payment
        for spec in self.golden.dataset['invoices']:
            if spec['status'] in ('draft', 'void'):
                inv = self.golden.invoices[spec['id']]
                self.assertFalse(Payment.objects.filter(invoice=inv).exists(), spec['id'])
                self.assertFalse(inv.credit_notes.exists(), spec['id'])
        self.assertIsNone(self.golden.payments['P03'])  # the deleted payment is gone

    def test_invoice_detail_api_matches(self):
        api = APIClient()
        api.force_authenticate(self.golden.user)
        for iid in ('INV02', 'INV03', 'INV08', 'INV13', 'INV15', 'INV27', 'INV44'):
            want = self.exp['invoices'][iid]
            r = api.get(f'/api/v1/invoices/{self.golden.invoices[iid].pk}/')
            self.assertEqual(r.status_code, 200, r.data)
            with self.subTest(invoice=iid):
                for f in ('subtotal', 'discount', 'vat_amount', 'total_amount', 'paid_amount', 'credited_amount',
                          'balance'):
                    self.assertEqual(m(r.data[f]), want[f], f)
                self.assertEqual(r.data['status'], want['status'])
                self.assertEqual(r.data['due_date'], want['due_date'])
                self.assertEqual([(m(l['net_amount']), m(l['vat_amount'])) for l in r.data['lines']],
                                 [(l['net'], l['vat']) for l in want['lines']])
