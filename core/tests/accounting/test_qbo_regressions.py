"""Regressions from the independent review of the QuickBooks branch: each
test reproduces a confirmed bug and now asserts the correct behaviour."""
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

from django.utils import timezone

from core.accounting import sync
from core.accounting.base import TransientError
from core.models import ExternalLink, Supplier
from core.tests.accounting.qbo_helpers import no_commit_delay
from core.tests.accounting.test_qbo_sync import QBOFlowBase


class RenumberingTests(QBOFlowBase):
    def test_custom_numbers_switched_off_later_never_leaves_a_renumbered_invoice(self):
        self.qbo.set_preference(custom_txn_numbers=False)   # switched off in QBO after connecting
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'BLOCKED')
        self.assertIn('Custom transaction numbers', link.last_error)
        live = [i for i in self.qbo.invoices() if not self.qbo.is_deleted('Invoice', i['Id'])]
        self.assertEqual(live, [])
        # Switched back on: the next poll re-reads the setting and the invoice goes.
        self.qbo.set_preference(custom_txn_numbers=True)
        with no_commit_delay(self):
            self.poll()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        self.assertEqual(self.qinv(inv)['DocNumber'], inv.invoice_number)


class BillReplacementTests(QBOFlowBase):
    def test_failed_delete_of_the_old_bill_is_finished_not_inverted(self):
        from core.accounting.quickbooks import QuickBooksAdapter
        from core.serializers import ExpenseSerializer
        sup = Supplier.objects.create(company=self.co, name='N4 Toll Concession', vat_number='4111111111')
        ser = ExpenseSerializer(data={'category': 'TOLLS', 'description': 'N4 tolls', 'amount': '1150.00',
                                      'expense_date': '2026-09-03', 'tax_code': 'STANDARD', 'supplier': sup.pk,
                                      'expense_number': f'EXP-{self.co.pk}-1', 'receipt_number': 'TOLL-778'},
                                context={'request': SimpleNamespace(user=self.admin), 'company': self.co})
        ser.is_valid(raise_exception=True)
        with no_commit_delay(self):
            exp = ser.save(company=self.co, created_by=self.admin)
        with mock.patch.object(QuickBooksAdapter, 'void_bill', side_effect=TransientError('QBO 503')):
            with no_commit_delay(self):
                exp.amount, exp.vat_amount = D('1265.00'), D('165.00')
                exp.save()
        link = self.link('BILL', exp.pk)
        self.assertEqual(link.status, 'ERROR')
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        sync.retry_due()
        link.refresh_from_db()
        live = [b for b in self.qbo.bills() if not self.qbo.is_deleted('Bill', b['Id'])]
        self.assertEqual([(b['Id'], D(b['TotalAmt'])) for b in live], [(link.external_id, D('1265.00'))])
        self.assertEqual(link.status, 'SYNCED')


class RetryAfterFixTests(QBOFlowBase):
    def test_retry_after_the_cause_is_fixed_is_a_new_request(self):
        three = [{'description': f'Admin fee {i}', 'quantity': '1', 'unit_price': '10.10', 'tax_code': 'STANDARD'}
                 for i in range(3)]
        self.qbo.honour_tax_override = False
        inv = self.issue(lines=three)
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'DEAD')
        self.qbo.honour_tax_override = True     # fixed on the QBO side
        with no_commit_delay(self):
            resp = self.api().post(f'/api/v1/integrations/accounting/connection/sync/{link.pk}/retry/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(D(self.qinv(inv)['TxnTaxDetail']['TotalTax']), D('4.56'))


class CreditIsNeverMoneyTests(QBOFlowBase):
    def test_journal_entry_credit_in_a_payment_is_not_money(self):
        from core.accounting.quickbooks import QuickBooksAdapter
        a = QuickBooksAdapter(self.conn)
        p = {'Id': '77', 'TotalAmt': 0, 'TxnDate': '2026-09-20',
             'Line': [{'Amount': 100, 'LinkedTxn': [{'TxnId': '5', 'TxnType': 'Invoice'}]},
                      {'Amount': 100, 'LinkedTxn': [{'TxnId': '9', 'TxnType': 'JournalEntry'}]}]}
        allocs = a._allocations(p)
        self.assertFalse(any(k == 'PAYMENT' for k, *_ in allocs))
        self.assertEqual(allocs, [('OTHER_CREDIT', '5', D('100.00'), 'JournalEntry:9')])

    def test_money_never_exceeds_what_the_payment_brought_in(self):
        from core.accounting.quickbooks import QuickBooksAdapter
        a = QuickBooksAdapter(self.conn)
        p = {'Id': '78', 'TotalAmt': 60, 'UnappliedAmt': 0, 'TxnDate': '2026-09-20',
             'Line': [{'Amount': 100, 'LinkedTxn': [{'TxnId': '5', 'TxnType': 'Invoice'}]}]}
        self.assertEqual(sum(amt for k, _i, amt, _s in a._allocations(p) if k == 'PAYMENT'), D('60.00'))

    def test_auto_apply_credits_blocks_sync(self):
        self.qbo.set_preference(auto_apply_credit=True)
        from core.accounting import mapping
        blockers = mapping.refresh_blockers(self.conn)
        self.assertTrue(any('Automatically apply credits' in b for b in blockers))
