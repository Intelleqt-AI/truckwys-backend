"""The fake Xero's own arithmetic against hand-worked examples, and every
XeroAdapter call served end-to-end by it (so a gap in the fake shows up here,
not as a confusing failure in a flow test)."""
import uuid
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings

from core.accounting.base import AuthError, PermanentError, RateLimited, TransientError
from core.accounting.http import use_transport
from core.accounting.ratelimit import Limits, RateLimiter, redis_client
from core.tests.accounting.fake_xero import FakeXero, XeroValidationError

D = Decimal
SETTINGS = dict(XERO_CLIENT_ID='fake-client', XERO_CLIENT_SECRET='fake-secret',
                XERO_REDIRECT_URI='https://api.truckwys.test/api/v1/integrations/xero/callback/')


def line(desc, qty, unit, account='200', tax='OUTPUT2', **extra):
    row = {'Description': desc, 'Quantity': str(qty), 'UnitAmount': str(unit), 'AccountCode': account,
           'TaxType': tax}
    row.update(extra)
    return row


class FakeArithmeticTests(SimpleTestCase):
    """Hand-worked figures; all ROUND_HALF_UP to the cent, tax per line."""

    def setUp(self):
        self.xero = FakeXero()
        self.cid = self.xero.create_contact('Acme Mining (Pty) Ltd')

    def inv(self, lines, unitdp=4, lat='Exclusive', type='ACCREC', status='AUTHORISED'):
        iid = self.xero.create_invoice(self.cid, lines, date(2025, 7, 1), type=type, status=status, unitdp=unitdp,
                                       line_amount_types=lat)
        return self.xero.invoice(iid)

    def test_tax_is_rounded_per_line_not_on_the_total(self):
        # 3 lines of R0.10 at 15%: 0.015 -> 0.02 each = 0.06 (on the total it would be 0.045 -> 0.05)
        inv = self.inv([line('a', 1, '0.10'), line('b', 1, '0.10'), line('c', 1, '0.10')])
        self.assertEqual([l['TaxAmount'] for l in inv.lines], [D('0.02')] * 3)
        self.assertEqual((inv.sub_total, inv.total_tax, inv.total), (D('0.30'), D('0.06'), D('0.36')))

    def test_line_amount_rounds_half_up(self):
        # 3 x 33.335 = 100.005 -> 100.01; VAT 15.0015 -> 15.00
        inv = self.inv([line('Pallets', 3, '33.335')])
        self.assertEqual(inv.lines[0]['LineAmount'], D('100.01'))
        self.assertEqual((inv.total_tax, inv.total), (D('15.00'), D('115.01')))

    def test_discount_rate_vs_discount_amount(self):
        # rate: 100.005 x 0.9 = 90.0045 -> 90.00 ; amount: 100.005 - 10 = 90.005 -> 90.01
        by_rate = self.inv([line('Pallets', 3, '33.335', DiscountRate='10')])
        by_amount = self.inv([line('Pallets', 3, '33.335', DiscountAmount='10.00')])
        self.assertEqual(by_rate.lines[0]['LineAmount'], D('90.00'))
        self.assertEqual(by_rate.total_tax, D('13.50'))
        self.assertEqual(by_amount.lines[0]['LineAmount'], D('90.01'))
        self.assertEqual(by_amount.total_tax, D('13.50'))      # 13.5015
        self.assertEqual(by_amount.total, D('103.51'))

    def test_inclusive_bill_tax_is_the_tax_fraction(self):
        bill = self.inv([line('Diesel', 1, '1150.00', account='473', tax='INPUT2'),
                         line('Tyre levy', 1, '99.99', account='473', tax='INPUT2')], lat='Inclusive', type='ACCPAY')
        # 1150 x 15/115 = 150.00 ; 99.99 x 15/115 = 13.0422 -> 13.04
        self.assertEqual([l['TaxAmount'] for l in bill.lines], [D('150.00'), D('13.04')])
        self.assertEqual((bill.sub_total, bill.total_tax, bill.total), (D('1086.95'), D('163.04'), D('1249.99')))

    def test_tax_amount_override_is_honoured_unless_switched_off(self):
        inv = self.inv([line('Freight', 1, '100.00', TaxAmount='14.99')])
        self.assertEqual((inv.total_tax, inv.total), (D('14.99'), D('114.99')))
        self.xero.honour_tax_amount = False
        inv = self.inv([line('Freight', 1, '100.00', TaxAmount='14.99')])
        self.assertEqual((inv.total_tax, inv.total), (D('15.00'), D('115.00')))

    def test_unitdp_2_rounds_the_price_first(self):
        four = self.inv([line('Per km', 1000, '1.23456')], unitdp=4)
        two = self.inv([line('Per km', 1000, '1.23456')], unitdp=2)
        self.assertEqual(four.lines[0]['UnitAmount'], D('1.2346'))
        self.assertEqual(four.lines[0]['LineAmount'], D('1234.60'))
        self.assertEqual(two.lines[0]['UnitAmount'], D('1.23'))
        self.assertEqual(two.lines[0]['LineAmount'], D('1230.00'))

    def test_force_tax_delta_skews_totals(self):
        self.xero.force_tax_delta = D('0.01')
        inv = self.inv([line('Freight', 1, '100.00')])
        self.assertEqual((inv.sub_total, inv.total_tax, inv.total), (D('100.00'), D('15.01'), D('115.01')))

    def test_amount_due_and_status_follow_payments_and_allocations(self):
        inv = self.inv([line('Freight', 1, '1000.00')])          # 1150.00
        pid = self.xero.record_payment(inv.id, D('1000.00'), date(2025, 7, 10))
        self.assertEqual((inv.status, inv.amount_due), ('AUTHORISED', D('150.00')))
        cn = self.xero.create_credit_note(self.cid, [line('Short delivery', 1, '150.00')], date(2025, 7, 11),
                                          line_amount_types='Inclusive')
        # inclusive: tax = 150 x 15/115 = 19.5652 -> 19.57, net 130.43
        cnote = self.xero.credit_note(cn)
        self.assertEqual((cnote.sub_total, cnote.total_tax, cnote.total), (D('130.43'), D('19.57'), D('150.00')))
        aid = self.xero.allocate('CREDIT_NOTE', cn, inv.id, D('150.00'), date(2025, 7, 12))
        self.assertEqual((inv.status, inv.amount_due, inv.amount_credited), ('PAID', D('0.00'), D('150.00')))
        before = inv.updated
        self.xero.advance(minutes=1)
        self.xero.remove_allocation('CREDIT_NOTE', cn, aid)
        self.assertEqual((inv.status, inv.amount_due), ('AUTHORISED', D('150.00')))
        self.assertGreater(inv.updated, before)
        with self.assertRaises(XeroValidationError):
            self.xero.void_invoice(inv.id)                       # a payment is still applied
        self.xero.delete_payment(pid)
        self.assertEqual(self.xero.payment(pid).status, 'DELETED')
        self.assertEqual(inv.amount_due, D('1150.00'))
        self.xero.void_invoice(inv.id)
        self.assertEqual((inv.status, inv.amount_due), ('VOIDED', D('0.00')))

    def test_payment_cannot_exceed_amount_due_or_go_to_a_non_bank_account(self):
        inv = self.inv([line('Freight', 1, '100.00')])           # 115.00
        with self.assertRaisesRegex(XeroValidationError, 'exceeds the amount outstanding'):
            self.xero.record_payment(inv.id, D('115.01'), date(2025, 7, 2))
        with self.assertRaisesRegex(XeroValidationError, 'Account type is invalid'):
            self.xero.record_payment(inv.id, D('10.00'), date(2025, 7, 2), account_code='200')
        self.xero.record_payment(inv.id, D('10.00'), date(2025, 7, 2), account_code='881')  # EnablePaymentsToAccount

    def test_allocation_caps(self):
        a = self.inv([line('Freight', 1, '100.00')])             # 115.00 due
        b = self.inv([line('Freight', 1, '100.00')])
        cn = self.xero.create_credit_note(self.cid, [line('Credit', 1, '173.92')], date(2025, 7, 5))   # 200.01
        self.assertEqual(self.xero.credit_note(cn).total, D('200.01'))
        with self.assertRaisesRegex(XeroValidationError, 'outstanding on the invoice'):
            self.xero.allocate('CREDIT_NOTE', cn, a.id, D('115.01'), date(2025, 7, 5))
        self.xero.allocate('CREDIT_NOTE', cn, a.id, D('115.00'), date(2025, 7, 5))
        self.assertEqual(self.xero.credit_note(cn).remaining_credit, D('85.01'))
        with self.assertRaisesRegex(XeroValidationError, 'remaining credit'):
            self.xero.allocate('CREDIT_NOTE', cn, b.id, D('85.02'), date(2025, 7, 5))
        self.xero.allocate('CREDIT_NOTE', cn, b.id, D('85.01'), date(2025, 7, 5))
        self.assertEqual(self.xero.credit_note(cn).status, 'PAID')
        op = self.xero.create_overpayment(self.cid, D('50.00'), date(2025, 7, 6))
        with self.assertRaisesRegex(XeroValidationError, 'remaining credit'):
            self.xero.allocate('OVERPAYMENT', op, b.id, D('29.99') + D('20.02'), date(2025, 7, 6))

    def test_reports_mirror(self):
        inv = self.inv([line('Freight', 1, '1000.00'), line('Fuel', 1, '100.00', account='201')])   # 1265.00
        self.xero.record_payment(inv.id, D('265.00'), date(2025, 7, 20))
        op = self.xero.create_overpayment(self.cid, D('40.00'), date(2025, 7, 25))
        self.inv([line('Diesel', 1, '500.00', account='449', tax='ZERORATEDINPUT')], type='ACCPAY')
        self.assertEqual(self.xero.balance_sheet_ar(date(2025, 7, 15)), D('1265.00'))
        self.assertEqual(self.xero.balance_sheet_ar(date(2025, 7, 31)), D('960.00'))   # 1000 - 40
        self.xero.allocate('OVERPAYMENT', op, inv.id, D('40.00'), date(2025, 8, 1))
        self.assertEqual(self.xero.balance_sheet_ar(date(2025, 8, 1)), D('960.00'))
        self.assertEqual(self.xero.aged_receivables(date(2025, 8, 1)), {self.cid: D('960.00')})
        pl = self.xero.profit_and_loss(date(2025, 7, 1), date(2025, 7, 31))
        self.assertEqual(pl['income'], {'200': D('1000.00'), '201': D('100.00')})
        self.assertEqual(pl['expenses'], {'449': D('500.00')})
        self.assertEqual(pl['net_profit'], D('600.00'))
        self.assertEqual(self.xero.tax_totals(date(2025, 7, 1), date(2025, 7, 31)),
                         {'OUTPUT2': {'net': D('1100.00'), 'tax': D('165.00')}})
        self.assertEqual(self.xero.tax_totals(date(2025, 7, 1), date(2025, 7, 31), side='ACCPAY'),
                         {'ZERORATEDINPUT': {'net': D('500.00'), 'tax': D('0.00')}})

    def test_contact_names_are_unique_across_active_contacts(self):
        with self.assertRaisesRegex(XeroValidationError, 'already assigned to another contact'):
            self.xero.create_contact('ACME MINING (PTY) LTD')

    def test_validation_of_codes_and_tracking(self):
        with self.assertRaisesRegex(XeroValidationError, "Account code '999'"):
            self.inv([line('x', 1, 1, account='999')])
        with self.assertRaisesRegex(XeroValidationError, "Account code '090'"):
            self.inv([line('x', 1, 1, account='090')])            # bank accounts can't take lines
        with self.assertRaisesRegex(XeroValidationError, "TaxType code 'OUTPUT3'"):
            self.inv([line('x', 1, 1, tax='OUTPUT3')])            # archived 14%
        with self.assertRaisesRegex(XeroValidationError, "TaxType code 'INPUT2'"):
            self.inv([line('x', 1, 1, tax='INPUT2')])             # purchase rate on a revenue account
        with self.assertRaisesRegex(XeroValidationError, 'tracking option'):
            self.inv([line('x', 1, 1, Tracking=[{'Name': 'Region', 'Option': 'Limpopo'}])])
        inv = self.inv([line('x', 1, 1, Tracking=[{'Name': 'Region', 'Option': 'kzn'}])])
        self.assertEqual(inv.lines[0]['Tracking'][0]['Option'], 'KZN')


class AdapterAgainstFakeBase(TestCase):
    tenant = None

    def setUp(self):
        self._settings = override_settings(**SETTINGS)
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.xero = FakeXero(now=datetime(2025, 7, 1, 8, 0, tzinfo=dt_timezone.utc))
        self._t = use_transport(self.xero)
        self._t.__enter__()
        self.addCleanup(self._t.__exit__, None, None, None)
        self.adapter, self.conn = self.make_adapter()

    def make_adapter(self):
        from core.accounting.tokens import store_tokens
        from core.accounting.xero import XeroAdapter
        from core.models import AccountingConnection, Company
        company = Company.objects.create(company_name=f'Fake Xero Co {uuid.uuid4().hex[:6]}')
        conn = AccountingConnection.objects.create(
            company=company, provider='XERO', status=AccountingConnection.ACTIVE, tenant_id=self.xero.tenant_id,
            tenant_name='Golden Haulage (Pty) Ltd', provider_connection_id='')
        store_tokens(conn, self.xero.token_set())
        conn.provider_connection_id = self.xero.connection_id()
        conn.save()
        adapter = XeroAdapter(conn)
        ns = f'test-fakexero-{uuid.uuid4().hex}'
        adapter.http.limiter = RateLimiter('XERO', Limits(per_minute=10000, per_day=None, concurrent=50),
                                           namespace=ns)
        self.addCleanup(self._flush, ns)
        return adapter, conn

    @staticmethod
    def _flush(ns):
        try:
            r = redis_client()
            for k in r.scan_iter(f'{ns}:*'):
                r.delete(k)
        except Exception:
            pass

    def doc(self, kind, number, lines, contact_id, *, inclusive=False, due=None, reference=''):
        from core.accounting.base import DocLine, Document
        return Document(kind=kind, number=number, contact_id=contact_id, issue_date=date(2025, 7, 1),
                        due_date=due, reference=reference, amounts_include_tax=inclusive,
                        lines=[DocLine(description=d, quantity=D(q), unit_price=D(u), net_amount=D('0'),
                                       tax_amount=D(t), account_code=a, tax_code=tc, tracking=tr)
                               for d, q, u, t, a, tc, tr in lines])


class AdapterAgainstFakeTests(AdapterAgainstFakeBase):

    def test_settings_contacts_and_orgs(self):
        a = self.adapter
        orgs = a.list_orgs()
        self.assertEqual([(o.name, o.base_currency, o.country) for o in orgs],
                         [('Golden Haulage (Pty) Ltd', 'ZAR', 'ZA')])
        self.assertEqual(orgs[0].connection_id, self.xero.connection_id())
        rates = {r.code: r for r in a.get_tax_rates()}
        self.assertEqual(rates['OUTPUT2'].rate, D('15.00'))
        self.assertTrue(rates['OUTPUT2'].revenue)
        self.assertEqual(rates['OUTPUT3'].status, 'ARCHIVED')
        accounts = {x.code: x for x in a.get_accounts()}
        self.assertTrue(accounts['090'].is_bank)
        self.assertTrue(accounts['881'].is_bank)
        self.assertNotIn(None, accounts)                       # Petty Cash has no code
        cats = {c.name: c for c in a.get_tracking()}
        self.assertEqual([o.name for o in cats['Region'].options], ['Gauteng', 'KZN', 'Western Cape'])
        self.assertEqual(a.ensure_tracking_option(cats['Vehicle'].id, 'CA 123-456'), 'CA 123-456')
        self.assertEqual(a.ensure_tracking_option(cats['Vehicle'].id, 'ca 123-456'), 'CA 123-456')
        self.xero.add_tracking_options('Vehicle', [f'V{i}' for i in range(99)])
        self.assertIsNone(a.ensure_tracking_option(cats['Vehicle'].id, 'One too many'))

        from core.accounting.base import Contact
        c = a.upsert_contact(Contact(name='Acme Mining (Pty) Ltd', email='ar@acme.test', vat_number='4123456789',
                                     reference='TW-C-1'))
        self.assertTrue(c.external_id)
        self.assertEqual([x.external_id for x in a.find_contacts(vat_number='4123456789')], [c.external_id])
        self.assertEqual([x.external_id for x in a.find_contacts(email='AR@acme.test')], [c.external_id])
        self.assertEqual([x.external_id for x in a.find_contacts(name='acme')], [c.external_id])
        self.assertEqual([x.name for x in a.find_contacts(external_id=c.external_id)], ['Acme Mining (Pty) Ltd'])
        self.assertEqual(a.find_contacts(external_id=str(uuid.uuid4())), [])
        with self.assertRaisesRegex(PermanentError, 'already assigned to another contact'):
            a.upsert_contact(Contact(name='Acme Mining (Pty) Ltd', reference='TW-C-2'))
        self.assertEqual(len(list(a.list_contacts())), 1)
        self.assertEqual(self.xero.unknown, [])

    def test_document_lifecycle_payments_and_reconciliation(self):
        from core.accounting.base import Contact
        a, xero = self.adapter, self.xero
        cid = a.upsert_contact(Contact(name='Acme Mining (Pty) Ltd', reference='TW-C-1')).external_id
        region = [('Region', 'Gauteng')]
        doc = self.doc('INVOICE', 'INV-1001', [
            ('Freight JHB-DBN', '1', '18500.00', '2775.00', '200', 'OUTPUT2', region),
            ('Pallet handling', '3', '33.3350', '13.50', '260', 'OUTPUT2', []),
            ('Cross-border leg', '1', '4200.00', '0.00', '205', 'ZERORATEDOUTPUT', []),
        ], cid, due=date(2025, 7, 31))
        doc.lines[1].discount_percent = D('10')
        res = a.push_invoice(doc, idempotency_key='inv-1001-v1')
        self.assertEqual((res.status, res.sub_total, res.total_tax, res.total),
                         ('DRAFT', D('22790.00'), D('2788.50'), D('25578.50')))
        put = xero.calls_to('PUT', r'^/Invoices$')[-1]
        self.assertEqual(put.params, {'unitdp': '4', 'summarizeErrors': 'false'})
        self.assertEqual(put.headers['Idempotency-Key'], 'inv-1001-v1')
        # replay: same key -> same document, nothing new
        again = a.push_invoice(doc, idempotency_key='inv-1001-v1')
        self.assertEqual(again.external_id, res.external_id)
        self.assertEqual(len(xero.invoices()), 1)
        # a different key with the same number is a duplicate
        with self.assertRaisesRegex(PermanentError, 'Invoice # must be unique'):
            a.push_invoice(doc, idempotency_key='inv-1001-v2')
        # update the draft, then authorise
        res = a.push_invoice(doc, external_id=res.external_id)
        self.assertEqual(res.status, 'DRAFT')
        fin = a.finalise_document('INVOICE', res.external_id)
        self.assertEqual(fin.status, 'AUTHORISED')
        self.assertEqual(a.find_document('INVOICE', 'INV-1001').external_id, res.external_id)
        self.assertEqual(a.get_document('INVOICE', res.external_id).total, D('25578.50'))
        inv_id = res.external_id

        # a credit note, pushed, authorised, allocated
        cn = self.doc('CREDIT_NOTE', 'CN-1001', [('Pallet handling', '3', '33.3350', '13.50', '260', 'OUTPUT2', [])],
                      cid)
        cn.lines[0].discount_percent = D('10')
        cres = a.push_credit_note(cn, idempotency_key='cn-1001')
        a.finalise_document('CREDIT_NOTE', cres.external_id)
        a.allocate_credit_note(cres.external_id, inv_id, D('103.50'), date(2025, 7, 2))
        detail = a.get_credit_note_detail(cres.external_id)
        self.assertEqual((detail['total'], detail['remaining']), (D('103.50'), D('0.00')))
        self.assertEqual(detail['allocations'][0]['invoice_id'], inv_id)
        self.assertEqual(a.find_document('CREDIT_NOTE', 'CN-1001').external_id, cres.external_id)

        # payments in Xero, then read back
        xero.advance(minutes=5)
        p1 = xero.record_payment(inv_id, D('20000.00'), date(2025, 7, 10), reference='EFT 1')
        op = xero.create_overpayment(cid, D('6000.00'), date(2025, 7, 11))
        alloc = xero.allocate('OVERPAYMENT', op, inv_id, D('5475.00'), date(2025, 7, 11))
        state = a.get_invoice_state(inv_id)
        self.assertEqual(state.status, 'PAID')
        self.assertEqual(state.amount_due, D('0.00'))
        cn_alloc = xero.credit_note(cres.external_id).allocations[0].id
        kinds = sorted((s.kind, s.external_id, s.source_id, s.amount) for s in state.settlements)
        self.assertEqual(kinds, sorted([('PAYMENT', p1, p1, D('20000.00')),
                                        ('OVERPAYMENT', alloc, op, D('5475.00')),
                                        ('CREDIT_NOTE', cn_alloc, cres.external_id, D('103.50'))]))
        self.assertEqual(state.issue_date, date(2025, 7, 1))
        self.assertEqual([s.number for s in a.get_invoice_states([inv_id])], ['INV-1001'])

        since = datetime(2025, 7, 1, 8, 4, tzinfo=dt_timezone.utc)
        pays = a.list_payments_since(since)
        self.assertEqual([(p.external_id, p.status, p.amount) for p in pays], [(p1, 'ACTIVE', D('20000.00'))])
        xero.advance(minutes=5)
        xero.delete_payment(p1)
        pays = a.list_payments_since(datetime(2025, 7, 1, 8, 9, tzinfo=dt_timezone.utc))
        self.assertEqual([(p.external_id, p.status) for p in pays], [(p1, 'DELETED')])
        changes = a.list_credit_note_allocations(since)
        self.assertIn(('OVERPAYMENT', alloc, inv_id), [(c.kind, c.external_id, c.invoice_external_id)
                                                        for c in changes])
        credits = a.list_unallocated_credits()
        self.assertEqual([(c.kind, c.remaining) for c in credits], [('OVERPAYMENT', D('525.00'))])
        self.assertEqual(a.receivables_by_contact(), {cid: D('20000.00') - D('525.00')})
        self.assertEqual(a.debtors_at(date(2025, 7, 31)), xero.balance_sheet_ar(date(2025, 7, 31)))
        self.assertEqual(a.debtors_at(date(2025, 7, 31)), D('19475.00'))
        docs = a.list_sales_documents(date(2025, 7, 1), date(2025, 7, 31))
        self.assertEqual(sorted(d.kind for d in docs), ['CREDIT_NOTE', 'INVOICE'])
        receipts = a.list_receipts(date(2025, 7, 1), date(2025, 7, 31))
        self.assertEqual([(r.kind, r.amount) for r in receipts], [('OVERPAYMENT', D('6000.00'))])
        where = xero.calls_to('GET', r'^/Payments$')[-1].params['where']
        self.assertEqual(where, 'PaymentType=="ACCRECPAYMENT"&&Status=="AUTHORISED"&&'
                                'Date>=DateTime(2025,07,01)&&Date<=DateTime(2025,07,31)')

        # voiding: the overpayment allocation blocks it
        with self.assertRaisesRegex(PermanentError, 'cannot be voided'):
            a.void_invoice(inv_id)
        xero.remove_allocation('OVERPAYMENT', op, alloc)
        a.void_credit_note(cres.external_id)                  # removes its allocation first
        self.assertEqual(xero.credit_note(cres.external_id).status, 'VOIDED')
        a.void_invoice(inv_id)
        self.assertEqual(xero.invoice(inv_id).status, 'VOIDED')
        self.assertEqual(xero.unknown, [])

    def test_bills_backfill_and_drafts(self):
        from core.accounting.base import Contact
        a, xero = self.adapter, self.xero
        sid = a.upsert_contact(Contact(name='Engen Garage', reference='TW-S-1')).external_id
        bill = self.doc('BILL', 'SUP-77', [('Diesel', '1', '2300.00', '0.00', '449', 'ZERORATEDINPUT', []),
                                           ('Tolls', '1', '115.00', '15.00', '450', 'INPUT2', [])],
                        sid, inclusive=True, due=date(2025, 7, 31))
        res = a.push_bill(bill, idempotency_key='bill-77')
        a.finalise_document('BILL', res.external_id)
        bill.lines[0].unit_price = D('2400.00')
        res2 = a.push_bill(bill, external_id=res.external_id)   # AUTHORISED, unpaid: editable
        self.assertEqual((res2.status, res2.total, res2.total_tax), ('AUTHORISED', D('2515.00'), D('15.00')))
        self.assertTrue(xero.invoice(res.external_id).contact_id == sid and xero.contact(sid).is_supplier)
        a.void_bill(res.external_id)
        self.assertEqual(xero.invoice(res.external_id).status, 'VOIDED')

        cid = a.upsert_contact(Contact(name='Acme', reference='TW-C-9')).external_id
        inv = a.push_invoice(self.doc('INVOICE', 'INV-9', [('x', '1', '100.00', '15.00', '200', 'OUTPUT2', [])],
                                      cid), idempotency_key='inv-9')
        a.finalise_document('INVOICE', inv.external_id)
        pay = a.push_payment(invoice_external_id=inv.external_id, amount=D('100.00'), on=date(2025, 7, 3),
                             account_code='090', reference='hist', idempotency_key='pay-9')
        self.assertEqual(xero.payment(pay.external_id).amount, D('100.00'))
        with self.assertRaisesRegex(PermanentError, 'exceeds the amount outstanding'):
            a.push_payment(invoice_external_id=inv.external_id, amount=D('15.01'), on=date(2025, 7, 3),
                           account_code='090', reference='hist', idempotency_key='pay-9b')
        ovp = a.push_overpayment(contact_id=cid, amount=D('35.00'), on=date(2025, 7, 3), account_code='090',
                                 reference='excess', idempotency_key='ovp-9')
        self.assertEqual(xero.overpayment(ovp.external_id).remaining_credit, D('35.00'))

        draft = a.push_invoice(self.doc('INVOICE', 'INV-10', [('x', '1', '1.00', '0.15', '200', 'OUTPUT2', [])],
                                        cid), idempotency_key='inv-10')
        a.discard_document('INVOICE', draft.external_id)
        self.assertEqual(xero.invoice(draft.external_id).status, 'DELETED')
        self.assertIsNone(a.find_document('INVOICE', 'INV-10'))
        self.assertEqual(xero.unknown, [])

    def test_find_bill_by_its_number_and_supplier(self):
        """ACCPAY bills have no Reference in Xero: the supplier's number is the
        InvoiceNumber, unique per supplier only."""
        from core.accounting.base import Contact
        a = self.adapter
        sid = a.upsert_contact(Contact(name='Engen', reference='S')).external_id
        other = a.upsert_contact(Contact(name='Sasol', reference='S2')).external_id
        res = a.push_bill(self.doc('BILL', 'SUP-1', [('x', '1', '115', '15', '450', 'INPUT2', [])], sid,
                                   inclusive=True), idempotency_key='b1')
        a.push_bill(self.doc('BILL', 'SUP-1', [('y', '1', '23', '3', '450', 'INPUT2', [])], other,
                             inclusive=True), idempotency_key='b2')
        self.assertEqual(a.find_document('BILL', 'SUP-1', contact_id=sid).external_id, res.external_id)
        self.assertEqual(self.xero.calls_to('GET', r'^/Invoices$')[-1].params['where'],
                         f'Type=="ACCPAY"&&InvoiceNumber=="SUP-1"&&Contact.ContactID==Guid("{sid}")')
        self.assertIsNone(a.find_document('BILL', 'SUP-2', contact_id=sid))


class AdapterTokenAndFailureTests(AdapterAgainstFakeBase):

    def test_expired_access_token_is_refreshed_and_the_refresh_token_rotates(self):
        from core.utils.crypto import decrypt_secret
        old_refresh = decrypt_secret(self.conn.refresh_token)
        self.xero.expire_access_tokens()
        self.adapter.get_accounts()                          # 401 -> refresh -> retry
        self.assertEqual([c.method + ' ' + c.path for c in self.xero.calls[-3:]],
                         ['GET /Accounts', 'POST /connect/token', 'GET /Accounts'])
        self.conn.refresh_from_db()
        new_refresh = decrypt_secret(self.conn.refresh_token)
        self.assertNotEqual(new_refresh, old_refresh)
        with self.assertRaises(AuthError):
            self.adapter.refresh(old_refresh)               # rotated: the old one is dead
        self.adapter.refresh(new_refresh)

    def test_fake_clock_expires_tokens_after_30_minutes(self):
        self.xero.advance(minutes=31)
        self.adapter.get_tax_rates()
        self.assertTrue(self.xero.calls_to('POST', r'^/connect/token$'))

    def test_revoked_tokens_force_reauth(self):
        self.xero.revoke_all_tokens()
        with self.assertRaises(AuthError):
            self.adapter.get_accounts()
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, 'NEEDS_REAUTH')

    def test_injected_failures(self):
        self.xero.fail_next('GET', r'^/Accounts$', status=429)
        with self.assertRaises(RateLimited) as ctx:
            self.adapter.get_accounts()
        self.assertEqual(ctx.exception.retry_after, 30)
        self.assertGreater(self.adapter.http.limiter.blocked_for(self.conn.tenant_id), 0)
        self.adapter.http.limiter.r.delete(self.adapter.http.limiter._k(self.conn.tenant_id, 'blocked'))
        self.xero.fail_next('GET', r'^/Accounts$', status=503)
        with self.assertRaises(TransientError):
            self.adapter.get_accounts()
        self.xero.timeout_next('GET', r'^/TaxRates$')
        with self.assertRaises(TransientError):
            self.adapter.get_tax_rates()
        self.assertEqual(len(self.adapter.get_accounts()), 34)   # Petty Cash has no code

    def test_timeout_after_processing_then_idempotent_retry(self):
        from core.accounting.base import Contact
        cid = self.adapter.upsert_contact(Contact(name='Acme', reference='C')).external_id
        doc = self.doc('INVOICE', 'INV-7', [('x', '1', '100.00', '15.00', '200', 'OUTPUT2', [])], cid)
        self.xero.timeout_next('PUT', r'^/Invoices$', after_processing=True)
        with self.assertRaises(TransientError):
            self.adapter.push_invoice(doc, idempotency_key='inv-7')
        self.assertEqual(len(self.xero.invoices()), 1)
        res = self.adapter.push_invoice(doc, idempotency_key='inv-7')
        self.assertEqual(len(self.xero.invoices()), 1)
        self.assertEqual(res.external_id, self.xero.find_invoice('INV-7')['InvoiceID'])

    def test_unknown_endpoint_is_loud(self):
        with self.assertRaisesRegex(AssertionError, 'unknown endpoint GET'):
            self.adapter._api('GET', '/Quotes')
        with self.assertRaisesRegex(AssertionError, 'does not support query parameter'):
            self.adapter._api('GET', '/Invoices', params={'bogus': '1'})
        with self.assertRaisesRegex(AssertionError, 'where field'):
            self.adapter._api('GET', '/Invoices', params={'where': 'Colour=="red"'})

    def test_other_tenants_and_disconnect(self):
        xero2 = FakeXero(orgs=[{'tenant_id': 't-za', 'name': 'ZA Co'},
                               {'tenant_id': 't-gb', 'name': 'UK Co', 'currency': 'GBP', 'country': 'GB'}])
        with use_transport(xero2):
            code = xero2.authorize(redirect_uri=SETTINGS['XERO_REDIRECT_URI'])
            from core.accounting.xero import XeroAdapter
            tokens = XeroAdapter.exchange_code(code)
            self.assertEqual(xero2.calls[-1].path, '/connect/token')
            with self.assertRaises(PermanentError):
                XeroAdapter.exchange_code(code)                 # codes are single use
            from core.accounting.tokens import store_tokens
            self.conn.tenant_id = 't-gb'
            store_tokens(self.conn, tokens)
            adapter = XeroAdapter(self.conn)
            adapter.http.limiter = None
            orgs = adapter.list_orgs()
            self.assertEqual({(o.tenant_id, o.base_currency) for o in orgs}, {('t-za', 'ZAR'), ('t-gb', 'GBP')})
            self.assertEqual(xero2.calls_to('GET', r'^/connections$')[-1].params['authEventId'],
                             xero2.connections[0]['authEventId'])
            adapter.connection.provider_connection_id = next(o.connection_id for o in orgs if o.tenant_id == 't-gb')
            adapter.revoke()
            self.assertEqual(xero2.connections, [])
            with self.assertRaises(AuthError):
                adapter.get_accounts()
