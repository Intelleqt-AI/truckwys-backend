"""Fast Pay under real concurrency (Postgres only: SQLite has no row locks).

Run: DATABASE_URL=postgres://... manage.py test core.tests.test_capital_concurrency
"""
from decimal import Decimal
from unittest import skipIf

from django.db import connection, transaction
from django.db.utils import InternalError
from django.test import TransactionTestCase

from core.capital import engine, ledger
from core.models import AdvanceRequest, CapitalLedgerEntry, Invoice
from core.tests.capital_fixtures import make_funder
from core.tests.test_capital_engine import Setup, exposure, new_debtor
from core.tests.test_foundation_concurrency import _run_threads

PG_ONLY = skipIf(connection.vendor != 'postgresql', 'needs real row locks (Postgres)')
D = Decimal


@PG_ONLY
class CapitalConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.funder = make_funder('race', pot='5000000')
        self.co, self.line = Setup.transporter(self.funder, line='3000000', grade='B')

    def _request(self, ids):
        def go(i):
            adv, _a, ev, _created = engine.request(Invoice.objects.get(pk=ids[i]))
            return ev.decision, (adv.amount if adv is not None and adv.status != 'QUEUED' else D('0'))
        return go

    def test_two_requests_cannot_exceed_a_debtor_cap(self):
        debtor = new_debtor('Capped debtor', 'RETAIL_FMCG', 'B')
        # B cap in a small book = min(8% of R5m, R1m) = R400k; R360k already used -> R40k left,
        # less than either request's eligible amount (75% of R69,000 / R73,600).
        exposure(self.funder, debtor, D('360000'))
        invoices = [Setup.invoice(self.co, debtor, subtotal=f'{sub}.00', vat=f'{sub * 15 // 100}.00')[0]
                    for sub in (60000, 64000)]
        results, errors = _run_threads(2, self._request([i.pk for i in invoices]))
        self.assertEqual(errors, [])
        committed = ledger.balances(funder=self.funder, debtor=debtor)['committed']
        self.assertLessEqual(committed, D('400000.00'))
        self.assertEqual(committed, D('400000.00'))  # exactly one took the last R60k
        self.assertEqual(sorted(d for d, _ in results), ['PART_FUND', 'QUEUE'])
        self.assertTrue(ledger.reconcile(self.funder)['ok'])

    def test_many_requests_never_exceed_the_pot(self):
        debtors = [new_debtor(f'Pot debtor {i}', 'RETAIL_FMCG', 'B') for i in range(8)]
        exposure(self.funder, new_debtor('Big filler'), D('4800000'))  # R200k of pot left
        invoices = [Setup.invoice(self.co, d, subtotal='60000.00', vat='9000.00')[0] for d in debtors]
        results, errors = _run_threads(len(invoices), self._request([i.pk for i in invoices]))
        self.assertEqual(errors, [])
        pot = ledger.balances(funder=self.funder)['committed']
        self.assertLessEqual(pot, D('5000000.00'))
        funded = sum(a for _, a in results)
        self.assertLessEqual(funded, D('200000.00'))
        self.assertGreater(funded, 0)
        self.line.refresh_from_db()
        self.assertEqual(self.line.reserved, funded)
        self.assertTrue(ledger.reconcile(self.funder)['ok'])

    def test_same_invoice_twice_opens_one_advance(self):
        debtor = new_debtor('Dupe debtor', 'RETAIL_FMCG', 'B')
        inv, _ = Setup.invoice(self.co, debtor, subtotal='50000.00', vat='7500.00')
        results, errors = _run_threads(4, self._request([inv.pk] * 4))
        self.assertEqual(errors, [])
        self.assertEqual(AdvanceRequest.objects.filter(invoice=inv).count(), 1)
        adv = AdvanceRequest.objects.get(invoice=inv)
        self.assertEqual(ledger.balances(advance=adv)['reserved'], adv.amount)
        self.assertTrue(ledger.reconcile(self.funder)['ok'])

    def test_database_refuses_ledger_update_and_delete(self):
        debtor = new_debtor('Trigger debtor', 'OTHER', 'B')
        row = exposure(self.funder, debtor, D('1000'))
        for sql in ('UPDATE capital_ledger_entries SET amount = 1 WHERE id = %s',
                    'DELETE FROM capital_ledger_entries WHERE id = %s'):
            with self.subTest(sql=sql):
                with self.assertRaises(InternalError):
                    with transaction.atomic(), connection.cursor() as cur:
                        cur.execute(sql, [row.pk])
        self.assertEqual(CapitalLedgerEntry.objects.get(pk=row.pk).amount, D('1000.00'))
