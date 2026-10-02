"""Regressions from the independent review of the Xero PR: each test is a
reproduction of a confirmed bug, now asserting the correct behaviour."""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

import redis
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from core.accounting import backfill, reconciliation, sync
from core.accounting.base import RateLimited
from core.accounting.ratelimit import Limits, RateLimiter
from core.models import CreditNote, ExternalLink, Payment, Supplier
from core.tests.accounting.test_xero_sync import XeroFlowBase
from core.tests.accounting.xero_helpers import map_everything, no_commit_delay


class LostReceiptResponseTests(XeroFlowBase):
    map_on_setup = False

    def test_receipt_applied_but_answer_lost_is_not_pushed_twice(self):
        from core.services.payments import record_payment
        map_everything(self.conn)
        inv = self.issue(issue=date(2026, 8, 10))
        record_payment(self.co, self.admin, {'invoice': inv.pk, 'amount': str(inv.total_amount),
                                             'payment_date': '2026-08-12', 'payment_method': 'EFT'})
        self.xero.timeout_next('PUT', r'/Payments$', after_processing=True)
        with no_commit_delay(self):
            backfill.start(self.conn, '2026-08-01', self.admin)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.backfill['state'], 'FAILED')
        with no_commit_delay(self):
            backfill.start(self.conn, '2026-08-01', self.admin)   # "Start again"
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.backfill['state'], 'DONE', self.conn.backfill)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, inv.total_amount)
        self.assertEqual(list(Payment.objects.filter(invoice=inv).values_list('source', flat=True)), ['XERO'])
        x = self.xinv(inv)
        self.assertEqual(D(x['AmountPaid']), inv.total_amount)
        self.assertEqual(len(x['Payments']), 1)
        self.assertEqual([o for o in self.xero.org()['overpayments'].values() if o['Status'] != 'VOIDED'], [])


class LateInvoiceReceiptsTests(XeroFlowBase):
    map_on_setup = False

    def test_receipt_on_an_invoice_that_failed_during_backfill_is_pushed_when_it_syncs(self):
        from core.services.payments import record_payment
        map_everything(self.conn)
        inv = self.issue(issue=date(2026, 8, 10))
        record_payment(self.co, self.admin, {'invoice': inv.pk, 'amount': '1000.00',
                                             'payment_date': '2026-08-12', 'payment_method': 'EFT'})
        self.xero.fail_next('PUT', r'/Invoices$', status=503)
        with no_commit_delay(self):
            backfill.start(self.conn, '2026-08-01', self.admin)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.backfill['state'], 'FAILED')   # not DONE while a receipt is outstanding
        link = self.link('INVOICE', inv.pk)
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        sync.retry_due()
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(D(self.xinv(inv)['AmountPaid']), D('1000.00'))
        self.poll()
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, D('1000.00'))
        self.assertEqual(list(Payment.objects.filter(invoice=inv).values_list('source', 'amount')),
                         [('XERO', D('1000.00'))])

class MirrorSafetyTests(XeroFlowBase):
    def test_never_mirrors_while_truckwys_receipts_are_unpushed(self):
        """Safety net: Xero money is not added on top of a TruckWys receipt
        Xero doesn't have yet."""
        from core.accounting.settlements import mirror_invoice
        inv = self.issue()
        Payment.objects.create(company=self.co, invoice=inv, customer=inv.customer, amount=D('500.00'),
                               payment_date=date(2026, 9, 6), payment_method='EFT', payment_number='PAY-T-1')
        xid = self.link('INVOICE', inv.pk).external_id
        self.xero.record_payment(xid, D('500.00'), date(2026, 9, 6))
        counts = mirror_invoice(self.conn, inv, self.xero_adapter().get_invoice_state(xid))
        self.assertEqual(counts['created'], 0)
        self.assertEqual(Payment.objects.filter(invoice=inv).count(), 1)

    def xero_adapter(self):
        from core.accounting.registry import get_adapter
        return get_adapter(self.conn)


class CutoverMovedEarlierTests(XeroFlowBase):
    def test_an_invoice_linked_before_the_cutover_moves_with_it(self):
        from core.services.credit_notes import create_credit_note
        from core.services.payments import PaymentError, record_payment
        inv = self.issue(issue=date(2026, 8, 10))   # before the 1 Sep cut-over
        cid = self.xero.add_contact(name='Acme Mining (Pty) Ltd', TaxNumber='4123456789')
        lines = [{'Description': l.description, 'Quantity': str(l.quantity), 'UnitAmount': str(l.unit_price),
                  'DiscountRate': str(l.discount_percent) if l.discount_percent else None,
                  'AccountCode': '200', 'TaxType': 'OUTPUT2' if l.tax_code == 'STANDARD' else 'ZERORATEDOUTPUT',
                  'TaxAmount': str(l.vat_amount)} for l in inv.lines.order_by('position')]
        for l in lines:
            if l['DiscountRate'] is None:
                del l['DiscountRate']
        xid = self.xero.add_invoice(contact_id=cid, number=inv.invoice_number, date=inv.issue_date, lines=lines)
        with no_commit_delay(self):
            create_credit_note(inv, user=self.admin, reason='waived', issue_date=date(2026, 9, 5),
                               lines=[{'invoice_line': inv.lines.get(position=2).pk, 'description': 'x',
                                       'quantity': '1', 'unit_price': '1275.50', 'tax_code': 'STANDARD'}])
        self.assertTrue(self.link('INVOICE', inv.pk).meta.get('pre_cutover'))
        with no_commit_delay(self):
            backfill.start(self.conn, '2026-08-01', self.admin)
        self.xero.now = datetime.now(dt_timezone.utc) + timedelta(minutes=5)
        self.xero.record_payment(xid, D('1000.00'), date(2026, 9, 20))
        self.poll()
        self.assertEqual(list(Payment.objects.filter(invoice=inv).values_list('source', 'amount')),
                         [('XERO', D('1000.00'))])
        with self.assertRaises(PaymentError) as ctx:
            record_payment(self.co, self.admin, {'invoice': inv.pk, 'amount': '1.00', 'payment_date': '2026-09-21',
                                                 'payment_method': 'EFT'})
        self.assertEqual(ctx.exception.status_code, 409)


class RunningLinkTests(XeroFlowBase):
    def test_void_while_the_push_is_running_is_not_lost(self):
        from core.accounting.xero import XeroAdapter
        from core.models import Invoice
        from core.services.credit_notes import void_invoice
        orig = XeroAdapter.finalise_document
        holder = {}

        def finalise(self_, kind, ext):
            res = orig(self_, kind, ext)
            if kind == 'INVOICE' and not holder.get('done'):
                holder['done'] = True
                void_invoice(Invoice.objects.get(pk=holder['pk']), user=self.admin, reason='raised twice')
            return res

        orig_mark = Invoice.mark_as_sent

        def mark(inv_self):
            holder['pk'] = inv_self.pk
            return orig_mark(inv_self)

        with mock.patch.object(XeroAdapter, 'finalise_document', finalise), \
                mock.patch.object(Invoice, 'mark_as_sent', mark):
            inv = self.issue()
        sync.retry_due()
        self.assertEqual(self.xinv(inv)['Status'], 'VOIDED')
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'VOIDED')


class ReconciliationImportedCreditNoteTests(XeroFlowBase):
    def test_imported_credit_note_is_not_a_difference(self):
        inv = self.issue()
        xid = self.link('INVOICE', inv.pk).external_id
        cid = self.xinv(inv)['Contact']['ContactID']
        cn_id = self.xero.create_credit_note(cid, [{'Description': 'Damaged pallet', 'Quantity': '1',
                                                     'UnitAmount': '100.00', 'AccountCode': '200',
                                                     'TaxType': 'OUTPUT2'}], date(2026, 9, 11), 'XCN-1')
        self.xero.allocate('CREDIT_NOTE', cn_id, xid, D('115.00'), date(2026, 9, 11))
        self.webhook(xid)
        self.assertTrue(CreditNote.objects.filter(invoice=inv, source='XERO').exists())
        run = reconciliation.run(self.conn)
        self.assertEqual(run.difference_count, 0, list(run.differences.values_list('field', 'truckwys_value',
                                                                                    'provider_value')))

    def test_imported_credit_note_survives_later_syncs_of_the_invoice(self):
        inv = self.issue()
        xid = self.link('INVOICE', inv.pk).external_id
        cid = self.xinv(inv)['Contact']['ContactID']
        cn_id = self.xero.create_credit_note(cid, [{'Description': 'Damaged pallet', 'Quantity': '1',
                                                     'UnitAmount': '100.00', 'AccountCode': '200',
                                                     'TaxType': 'OUTPUT2'}], date(2026, 9, 11), 'XCN-1')
        self.xero.allocate('CREDIT_NOTE', cn_id, xid, D('115.00'), date(2026, 9, 11))
        self.webhook(xid)
        self.webhook(xid)   # the invoice is mirrored again (any later change)
        self.xero.record_payment(xid, D('100.00'), date(2026, 9, 12))
        self.webhook(xid)
        self.assertEqual(CreditNote.objects.get(invoice=inv, source='XERO').status, 'ISSUED')
        inv.refresh_from_db()
        self.assertEqual((inv.credited_amount, inv.paid_amount), (D('115.00'), D('100.00')))

    def test_xero_credit_note_removed_there_is_voided_here(self):
        inv = self.issue()
        xid = self.link('INVOICE', inv.pk).external_id
        cid = self.xinv(inv)['Contact']['ContactID']
        cn_id = self.xero.create_credit_note(cid, [{'Description': 'Damaged pallet', 'Quantity': '1',
                                                     'UnitAmount': '100.00', 'AccountCode': '200',
                                                     'TaxType': 'OUTPUT2'}], date(2026, 9, 11), 'XCN-1')
        alloc = self.xero.allocate('CREDIT_NOTE', cn_id, xid, D('115.00'), date(2026, 9, 11))
        self.webhook(xid)
        self.xero.now = datetime.now(dt_timezone.utc) + timedelta(minutes=2)
        self.xero.remove_allocation('CREDIT_NOTE', cn_id, alloc)
        self.xero.void_credit_note(cn_id)
        self.webhook(xid)
        inv.refresh_from_db()
        self.assertEqual(inv.credited_amount, D('0.00'))
        self.assertEqual(CreditNote.objects.get(invoice=inv, source='XERO').status, 'VOID')


class BillEditVerificationTests(XeroFlowBase):
    def test_an_edit_xero_would_calculate_differently_is_never_posted(self):
        from core.serializers import ExpenseSerializer
        sup = Supplier.objects.create(company=self.co, name='Midrand Fuel Depot', vat_number='4111111111')
        ser = ExpenseSerializer(data={'category': 'TOLLS', 'description': 'N4 tolls', 'amount': '1150.00',
                                      'expense_date': '2026-09-03', 'tax_code': 'STANDARD', 'supplier': sup.pk,
                                      'expense_number': f'EXP-{self.co.pk}-1', 'receipt_number': 'TOLL-778'},
                                context={'request': SimpleNamespace(user=self.admin), 'company': self.co})
        ser.is_valid(raise_exception=True)
        with no_commit_delay(self):
            exp = ser.save(company=self.co, created_by=self.admin)
        old_id = self.link('BILL', exp.pk).external_id
        self.xero.honour_tax_amount = False
        self.xero.force_tax_delta = D('0.01')
        with no_commit_delay(self):
            exp.amount = D('1265.00')
            exp.vat_amount = D('165.00')
            exp.save()
        link = self.link('BILL', exp.pk)
        self.assertEqual((link.status, link.external_id), ('DEAD', old_id))
        posted = [b for b in self.xero.org()['invoices'].values() if b['Type'] == 'ACCPAY' and b['Status'] == 'AUTHORISED']
        self.assertEqual([(b['InvoiceID'], D(b['Total'])) for b in posted], [(old_id, D('1150.00'))])


class AllocationRetryTests(XeroFlowBase):
    def test_allocation_applied_but_answer_lost(self):
        from core.services.credit_notes import create_credit_note
        inv = self.issue()
        self.xero.timeout_next('PUT', r'/Allocations$', after_processing=True)
        with no_commit_delay(self):
            cn = create_credit_note(inv, user=self.admin, reason='waived', issue_date=date(2026, 9, 10),
                                    lines=[{'invoice_line': inv.lines.get(position=2).pk, 'description': 'x',
                                            'quantity': '1', 'unit_price': '1275.50', 'tax_code': 'STANDARD'}])
        link = self.link('CREDIT_NOTE', cn.pk)
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        sync.retry_due()
        link.refresh_from_db()
        self.assertEqual((link.status, link.meta.get('allocated')), ('SYNCED', str(cn.total_amount)))
        self.assertEqual(D(self.xinv(inv)['AmountCredited']), cn.total_amount)
        self.assertEqual(reconciliation.run(self.conn).difference_count, 0)


class BackoffTests(XeroFlowBase):
    def test_timeouts_back_off_exponentially(self):
        self.xero.timeout_next('PUT', r'/Invoices$', times=20)
        inv = self.issue()
        waits = []
        for _ in range(12):
            link = self.link('INVOICE', inv.pk)
            if link.status == 'DEAD':
                break
            waits.append((link.next_attempt_at - timezone.now()).total_seconds())
            ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            sync.retry_due()
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'DEAD')
        self.assertGreater(waits[-1], waits[0] * 8)
        self.assertGreater(sum(waits), 3000)   # ~an hour of patience (0.8 x 3810 s with jitter), not minutes


class ReconnectTests(XeroFlowBase):
    def test_reconnecting_the_same_org_resumes_the_old_connection(self):
        from core.accounting import connection as conn_svc
        from core.services.credit_notes import create_credit_note
        from core.tests.accounting.xero_helpers import connect
        inv = self.issue()
        with no_commit_delay(self):
            cn = create_credit_note(inv, user=self.admin, reason='waived', issue_date=date(2026, 9, 10),
                                    lines=[{'invoice_line': inv.lines.get(position=2).pk, 'description': 'x',
                                            'quantity': '1', 'unit_price': '1275.50', 'tax_code': 'STANDARD'}])
        conn_svc.disconnect(self.conn, self.admin)
        with no_commit_delay(self):
            conn2, outcome = connect(self.co, self.admin, self.xero)
        self.assertEqual((conn2.pk, outcome, conn2.status), (self.conn.pk, 'connected', 'ACTIVE'))
        self.assertEqual(conn2.cutover_date, date(2026, 9, 1))
        self.poll()
        self.assertEqual(list(CreditNote.objects.filter(invoice=inv).values_list('source', flat=True)), ['MANUAL'])
        inv.refresh_from_db()
        self.assertEqual(inv.credited_amount, cn.total_amount)

    def test_reconnect_consent_without_the_org_keeps_the_connection(self):
        from core.accounting import connection as conn_svc
        from core.models import AccountingConnection
        from core.tests.accounting.xero_helpers import connect
        from core.accounting.xero import XeroAdapter
        self.xero.revoke_all_tokens()
        AccountingConnection.objects.filter(pk=self.conn.pk).update(status='NEEDS_REAUTH')
        with mock.patch.object(XeroAdapter, 'list_orgs', return_value=[]):
            with self.assertRaises(conn_svc.ConnectError) as ctx:
                connect(self.co, self.admin, self.xero)
        self.assertEqual(ctx.exception.code, 'no_organisations')
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, 'NEEDS_REAUTH')   # not disabled; payments stay managed


@override_settings(REDIS_URL='redis://127.0.0.1:6379/15')
class LimiterRefusalTests(SimpleTestCase):
    def test_refused_calls_spend_no_quota(self):
        r = redis.Redis.from_url('redis://127.0.0.1:6379/15')
        ns = 'reg-rl'
        for k in r.scan_iter(f'{ns}:*'):
            r.delete(k)
        lim = RateLimiter('XERO', Limits(per_minute=3, per_day=3, concurrent=1), client=r, namespace=ns)
        with lim.acquire('t1'):
            for _ in range(4):
                with self.assertRaises(RateLimited) as ctx:
                    with lim.acquire('t1'):
                        pass
                self.assertEqual(ctx.exception.scope, 'concurrent')
        with lim.acquire('t1'):
            pass
        with lim.acquire('t1'):
            pass
        day = [k for k in r.scan_iter(f'{ns}:XERO:t1:day:*')]
        self.assertEqual(int(r.get(day[0])), 3)
        for k in r.scan_iter(f'{ns}:*'):
            r.delete(k)


class ResumableReplacementTests(XeroFlowBase):
    def _bill(self):
        from core.serializers import ExpenseSerializer
        sup = Supplier.objects.create(company=self.co, name='Midrand Fuel Depot', vat_number='4111111111')
        ser = ExpenseSerializer(data={'category': 'TOLLS', 'description': 'N4 tolls', 'amount': '1150.00',
                                      'expense_date': '2026-09-03', 'tax_code': 'STANDARD', 'supplier': sup.pk,
                                      'expense_number': f'EXP-{self.co.pk}-9', 'receipt_number': 'TOLL-900'},
                                context={'request': SimpleNamespace(user=self.admin), 'company': self.co})
        ser.is_valid(raise_exception=True)
        with no_commit_delay(self):
            return ser.save(company=self.co, created_by=self.admin)

    def test_failed_void_of_the_old_bill_is_finished_on_retry(self):
        exp = self._bill()
        old_id = self.link('BILL', exp.pk).external_id
        self.xero.fail_next('POST', rf'/Invoices/{old_id}$', status=503)
        with no_commit_delay(self):
            exp.amount = D('1265.00')
            exp.vat_amount = D('165.00')
            exp.save()
        link = self.link('BILL', exp.pk)
        self.assertEqual(link.status, 'ERROR')
        new_id = link.external_id
        self.assertNotEqual(new_id, old_id)
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        sync.retry_due()
        link = self.link('BILL', exp.pk)
        self.assertEqual((link.status, link.external_id), ('SYNCED', new_id))
        bills = self.xero.org()['invoices']
        self.assertEqual((bills[old_id]['Status'], bills[new_id]['Status']), ('VOIDED', 'AUTHORISED'))
        self.assertEqual(D(bills[new_id]['Total']), D('1265.00'))

    def test_retry_after_a_discarded_attempt_is_a_new_request(self):
        self.xero.force_tax_delta = D('0.01')
        self.xero.honour_tax_amount = False
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'DEAD')
        self.xero.force_tax_delta = D('0')
        self.xero.honour_tax_amount = True
        with no_commit_delay(self):
            resp = self.api().post(f'/api/v1/integrations/accounting/connection/sync/{link.pk}/retry/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(self.xinv(inv)['Status'], 'AUTHORISED')
