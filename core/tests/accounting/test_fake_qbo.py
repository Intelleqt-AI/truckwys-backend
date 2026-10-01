"""The fake QBO ledger itself: its arithmetic and API rules must be QBO's, or
the flow and golden tests prove nothing. Plain unittest (no database)."""
import base64
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

import requests

from core.tests.accounting.fake_qbo import DEFAULT_REALM, FakeQBO, QBOFault, parse_query

D = Decimal
API = f'https://sandbox-quickbooks.api.intuit.com/v3/company/{DEFAULT_REALM}'


def line(amount=None, item='1', tax='3', qty=None, unit=None):
    return FakeQBO.sales_line(amount, item=item, tax_code=tax, qty=qty, unit=unit)


class FakeBase(TestCase):
    def setUp(self):
        self.qbo = FakeQBO(client_id='cid', client_secret='secret')
        self.c = self.qbo.company()
        self.cust = self.qbo.create_customer('Acme Mining', email='ar@acme.test', tax_id='4123456789')
        self.s = requests.Session()
        self.s.mount('https://', self.qbo)
        tok = self.qbo.issue_tokens()
        self.access, self.refresh = tok['access_token'], tok['refresh_token']

    def get(self, path, **params):
        params.setdefault('minorversion', '75')
        return self.s.get(f'{API}{path}', params=params, headers={'Authorization': f'Bearer {self.access}',
                                                                  'Accept': 'application/json'})

    def post(self, path, body, **params):
        params.setdefault('minorversion', '75')
        return self.s.post(f'{API}{path}', params=params, json=json.loads(json.dumps(body, default=str)),
                           headers={'Authorization': f'Bearer {self.access}', 'Accept': 'application/json'})

    def query(self, sql):
        return self.get('/query', query=sql).json()


class TaxArithmeticTests(FakeBase):
    def test_tax_is_per_rate_on_the_summed_net_unless_tax_lines_are_given(self):
        three = [line(D('10.10')) for _ in range(3)]
        inv = self.qbo.invoice(self.qbo.create_invoice(self.cust, three, date(2026, 9, 1), 'A-1'))
        # Per rate: 30.30 x 15 % = 4.545 -> 4.55. TruckWys per line: 3 x round(1.515) = 4.56.
        self.assertEqual((inv['TxnTaxDetail']['TotalTax'], inv['TotalAmt']), (D('4.55'), D('34.85')))
        tl = inv['TxnTaxDetail']['TaxLine'][0]['TaxLineDetail']
        self.assertEqual((tl['TaxRateRef']['value'], tl['NetAmountTaxable'], tl['TaxPercent']), ('1', D('30.30'), D('15')))
        kept = self.qbo.invoice(self.qbo.create_invoice(
            self.cust, three, date(2026, 9, 1), 'A-2',
            tax_lines=[{'Amount': D('4.56'), 'TaxLineDetail': {'TaxRateRef': {'value': '1'}}}]))
        self.assertEqual((kept['TxnTaxDetail']['TotalTax'], kept['TotalAmt']), (D('4.56'), D('34.86')))
        self.qbo.honour_tax_override = False
        ignored = self.qbo.invoice(self.qbo.create_invoice(
            self.cust, three, date(2026, 9, 1), 'A-3',
            tax_lines=[{'Amount': D('4.56'), 'TaxLineDetail': {'TaxRateRef': {'value': '1'}}}]))
        self.assertEqual(ignored['TxnTaxDetail']['TotalTax'], D('4.55'))

    def test_mixed_rates_are_taxed_separately(self):
        inv = self.qbo.invoice(self.qbo.create_invoice(self.cust, [line(D('100.00')), line(D('4200.00'), tax='4'),
                                                                   line(D('0.05'))], date(2026, 9, 1)))
        lines = {t['TaxLineDetail']['TaxRateRef']['value']: t['Amount'] for t in inv['TxnTaxDetail']['TaxLine']}
        self.assertEqual(lines, {'1': D('15.01'), '3': D('0.00')})       # 100.05 x 15 % = 15.0075
        sub = [l for l in inv['Line'] if l['DetailType'] == 'SubTotalLineDetail'][0]
        self.assertEqual((sub['Amount'], inv['TotalAmt']), (D('4300.05'), D('4315.06')))

    def test_qty_times_unit_price_must_equal_amount(self):
        with self.assertRaises(QBOFault) as ctx:
            self.qbo.create_invoice(self.cust, [line(D('90.00'), qty=D('3'), unit=D('33.335'))], date(2026, 9, 1))
        self.assertEqual(ctx.exception.code, '6070')
        ok = self.qbo.invoice(self.qbo.create_invoice(self.cust, [line(None, qty=D('3'), unit=D('33.335'))],
                                                      date(2026, 9, 1)))
        self.assertEqual(ok['Line'][0]['Amount'], D('100.01'))            # 100.005 rounded half up

    def test_lines_need_a_tax_code_and_a_sales_item(self):
        with self.assertRaises(QBOFault) as ctx:
            self.qbo.create_invoice(self.cust, [{'DetailType': 'SalesItemLineDetail', 'Amount': D('1'),
                                                 'SalesItemLineDetail': {'ItemRef': {'value': '1'}}}], date(2026, 9, 1))
        self.assertEqual(ctx.exception.code, '6000')
        with self.assertRaises(QBOFault) as ctx:
            self.qbo.create_invoice(self.cust, [line(D('1'), tax='7')], date(2026, 9, 1))   # purchases-only code
        self.assertEqual(ctx.exception.code, '6000')
        with self.assertRaises(QBOFault) as ctx:
            self.qbo.create_invoice(self.cust, [line(D('1'), item='99')], date(2026, 9, 1))
        self.assertEqual(ctx.exception.code, '2500')

    def test_tax_inclusive_bill(self):
        vendor = self.qbo.create_vendor('N4 Tolls')

        def bill(override):
            body = {'VendorRef': {'value': vendor}, 'TxnDate': '2026-09-03', 'GlobalTaxCalculation': 'TaxInclusive',
                    'Line': [{'DetailType': 'AccountBasedExpenseLineDetail', 'Amount': D('1000.00'),
                              'AccountBasedExpenseLineDetail': {'AccountRef': {'value': '21'},
                                                                'TaxCodeRef': {'value': '3'},
                                                                'TaxInclusiveAmt': D('1150.01')}}]}
            if override:
                body['TxnTaxDetail'] = {'TaxLine': [{'Amount': D('150.00'),
                                                     'TaxLineDetail': {'TaxRateRef': {'value': '2'}}}]}
            return self.c.render('Bill', self.c.save_txn('Bill', body))
        calc = bill(False)          # 1150.01 x 15/115 = 150.0013 -> 150.00; total = the inclusive amount
        self.assertEqual((calc['TxnTaxDetail']['TotalTax'], calc['TotalAmt']), (D('150.00'), D('1150.01')))
        kept = bill(True)           # explicit: net 1000.00 + 150.00
        self.assertEqual((kept['TxnTaxDetail']['TotalTax'], kept['TotalAmt'], kept['Balance']),
                         (D('150.00'), D('1150.00'), D('1150.00')))


class PaymentTests(FakeBase):
    def setUp(self):
        super().setUp()
        self.i1 = self.qbo.create_invoice(self.cust, [line(D('1000.00'))], date(2026, 9, 1), 'I-1')   # 1150.00
        self.i2 = self.qbo.create_invoice(self.cust, [line(D('200.00'))], date(2026, 9, 2), 'I-2')    # 230.00

    def test_one_payment_for_two_invoices_with_linked_txn_both_sides(self):
        pid = self.qbo.record_payment({self.i1: D('500'), self.i2: D('230')}, date(2026, 9, 5))
        p = self.qbo.payment(pid)
        self.assertEqual((p['TotalAmt'], p['UnappliedAmt']), (D('730.00'), D('0.00')))
        self.assertEqual(self.qbo.invoice(self.i1)['Balance'], D('650.00'))
        self.assertEqual(self.qbo.invoice(self.i2)['Balance'], D('0.00'))
        self.assertEqual(self.qbo.invoice(self.i1)['LinkedTxn'], [{'TxnId': pid, 'TxnType': 'Payment'}])
        self.qbo.delete_payment(pid)
        self.assertEqual(self.qbo.invoice(self.i1)['Balance'], D('1150.00'))
        self.assertTrue(self.qbo.is_deleted('Payment', pid))

    def test_unapplied_money_and_applying_it_later(self):
        pid = self.qbo.record_payment({self.i2: D('230')}, date(2026, 9, 5), total=D('300'))
        self.assertEqual(self.qbo.payment(pid)['UnappliedAmt'], D('70.00'))
        free = self.qbo.create_unapplied_payment(self.cust, D('100'), date(2026, 9, 6))
        self.assertEqual(self.qbo.payment(free)['UnappliedAmt'], D('100.00'))
        self.qbo.apply_payment(free, self.i1, D('60'))
        self.assertEqual((self.qbo.payment(free)['UnappliedAmt'], self.qbo.invoice(self.i1)['Balance']),
                         (D('40.00'), D('1090.00')))
        with self.assertRaises(QBOFault):
            self.qbo.apply_payment(free, self.i1, D('41'))   # more than is unapplied

    def test_credit_memo_applied_with_a_zero_payment(self):
        cm = self.qbo.create_credit_memo(self.cust, [line(D('100.00'))], date(2026, 9, 3), 'CM-1')   # 115.00
        app = self.qbo.apply_credit(cm, self.i1, D('115'), date(2026, 9, 4))
        p = self.qbo.payment(app)
        self.assertEqual((p['TotalAmt'], p['UnappliedAmt']), (D('0.00'), D('0.00')))
        self.assertEqual(self.qbo.credit_memo(cm)['RemainingCredit'], D('0.00'))
        self.assertEqual(self.qbo.credit_memo(cm)['LinkedTxn'], [{'TxnId': app, 'TxnType': 'Payment'}])
        self.assertEqual(self.qbo.invoice(self.i1)['Balance'], D('1035.00'))
        with self.assertRaises(QBOFault) as ctx:      # linked: delete the application first
            self.c.delete('CreditMemo', self.c.get('CreditMemo', cm))
        self.assertEqual(ctx.exception.code, '6480')

    def test_overpaying_an_invoice_line_and_other_customers_are_refused(self):
        with self.assertRaises(QBOFault):
            self.qbo.record_payment({self.i2: D('231')}, date(2026, 9, 5))
        other = self.qbo.create_customer('Other Co')
        with self.assertRaises(QBOFault):
            self.qbo.record_payment({self.i2: D('10')}, date(2026, 9, 5), customer_id=other)

    def test_void_invoice(self):
        self.qbo.record_payment({self.i2: D('10')}, date(2026, 9, 5))
        with self.assertRaises(QBOFault):
            self.qbo.void_invoice(self.i2)       # payment linked
        self.qbo.void_invoice(self.i1)
        inv = self.qbo.invoice(self.i1)
        self.assertEqual((inv['TotalAmt'], inv['Balance'], inv['PrivateNote']), (D('0'), D('0'), 'Voided'))


class ReportTests(FakeBase):
    def test_ar_balance_sheet_and_ageing_by_date(self):
        i1 = self.qbo.create_invoice(self.cust, [line(D('1000.00'))], date(2026, 7, 10))        # 1150
        other = self.qbo.create_customer('Bravo')
        self.qbo.create_invoice(other, [line(D('100.00'))], date(2026, 8, 10))                 # 115
        self.qbo.record_payment({i1: D('150')}, date(2026, 7, 20), total=D('200'))             # 50 unapplied
        self.qbo.create_credit_memo(self.cust, [line(D('100.00'))], date(2026, 8, 5))          # 115, unapplied
        self.assertEqual(self.qbo.balance_sheet_ar(date(2026, 7, 31)), D('950.00'))
        self.assertEqual(self.qbo.aged_receivables(date(2026, 8, 31)), {self.cust: D('835.00'), other: D('115.00')})
        body = self.get('/reports/BalanceSheet', start_date='2026-08-31', end_date='2026-08-31',
                        accounting_method='Accrual').json()
        from core.accounting.quickbooks import _find_ar
        self.assertEqual(_find_ar(body['Rows']), D('950.00'))
        empty = self.get('/reports/BalanceSheet', start_date='2026-06-30', end_date='2026-06-30').json()
        self.assertIsNone(_find_ar(empty['Rows']))   # QBO omits zero rows

    def test_profit_and_loss_by_income_account(self):
        self.qbo.create_invoice(self.cust, [line(D('1000.00')), line(D('50.00'), item='2')], date(2026, 9, 1))
        self.qbo.create_credit_memo(self.cust, [line(D('100.00'))], date(2026, 9, 2))
        pl = self.qbo.profit_and_loss(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(pl['income'], {'10': D('900.00'), '11': D('50.00')})
        report = self.get('/reports/ProfitAndLoss', start_date='2026-09-01', end_date='2026-09-30').json()
        self.assertEqual(report['Rows']['Row'][0]['Summary']['ColData'][1]['value'], '950.00')

    def test_tax_totals_by_code(self):
        self.qbo.create_invoice(self.cust, [line(D('100.00')), line(D('50.00'), tax='4')], date(2026, 9, 1))
        self.qbo.create_credit_memo(self.cust, [line(D('10.00'))], date(2026, 9, 2))
        t = self.qbo.tax_totals(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(t['3'], {'net': D('90.00'), 'tax': D('13.50')})
        self.assertEqual(t['4'], {'net': D('50.00'), 'tax': D('0.00')})


class ApiRuleTests(FakeBase):
    def test_numbering_needs_custom_transaction_numbers(self):
        self.assertEqual(self.qbo.invoice(self.qbo.create_invoice(self.cust, [line(D('1'))], date(2026, 9, 1),
                                                                  'INV-7'))['DocNumber'], 'INV-7')
        with self.assertRaises(QBOFault) as ctx:
            self.qbo.create_invoice(self.cust, [line(D('1'))], date(2026, 9, 1), 'INV-7')
        self.assertEqual(ctx.exception.code, '6140')
        self.qbo.set_preference(custom_txn_numbers=False)
        renumbered = self.qbo.invoice(self.qbo.create_invoice(self.cust, [line(D('1'))], date(2026, 9, 1), 'INV-8'))
        self.assertEqual(renumbered['DocNumber'], '1001')

    def test_display_names_are_unique_across_customers_vendors_and_employees(self):
        self.qbo.create_employee('Thandi Mokoena')
        for maker in (self.qbo.create_customer, self.qbo.create_vendor):
            with self.assertRaises(QBOFault) as ctx:
                maker('thandi mokoena')
            self.assertEqual(ctx.exception.code, '6240')
        resp = self.post('/vendor', {'DisplayName': 'Acme Mining'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['Fault']['Error'][0]['code'], '6240')

    def test_tax_ids_are_masked_on_read(self):
        row = self.get(f'/customer/{self.cust}').json()['Customer']
        self.assertEqual(row['PrimaryTaxIdentifier'], 'XXXXXX6789')

    def test_query_language_subset(self):
        for name in ('Bravo Bulk', 'Carry Co', 'Delta'):
            self.qbo.create_customer(name)
        rows = self.query("SELECT * FROM Customer WHERE DisplayName LIKE '%bulk%'")['QueryResponse']['Customer']
        self.assertEqual([r['DisplayName'] for r in rows], ['Bravo Bulk'])
        rows = self.query("SELECT * FROM Customer WHERE PrimaryEmailAddr = 'AR@acme.test'")['QueryResponse']['Customer']
        self.assertEqual([r['Id'] for r in rows], [self.cust])
        page = self.query('SELECT * FROM Customer STARTPOSITION 2 MAXRESULTS 2')['QueryResponse']
        self.assertEqual((page['startPosition'], page['maxResults'], [r['DisplayName'] for r in page['Customer']]),
                         (2, 2, ['Bravo Bulk', 'Carry Co']))
        ids = self.query("SELECT * FROM Account WHERE Id IN ('1', '27')")['QueryResponse']['Account']
        self.assertEqual([r['Id'] for r in ids], ['1'])                    # inactive hidden by default
        ids = self.query("SELECT * FROM Account WHERE Active = false")['QueryResponse']['Account']
        self.assertEqual([r['Id'] for r in ids], ['27'])
        self.assertEqual(self.query("SELECT * FROM Invoice WHERE DocNumber = 'nope'")['QueryResponse'], {})
        resp = self.get('/query', query="SELECT * FROM Payment WHERE UnappliedAmt > '0'")
        self.assertEqual((resp.status_code, resp.json()['Fault']['Error'][0]['code']), (400, '4001'))
        with self.assertRaises(QBOFault):
            parse_query("SELECT * FROM Customer WHERE Id = '1' OR Id = '2'")
        esc = parse_query(r"SELECT * FROM Customer WHERE DisplayName = 'O\'Brien Haulage'")
        self.assertEqual(esc.conds, [('DisplayName', '=', "O'Brien Haulage")])

    def test_deleted_objects_read_as_object_not_found(self):
        i = self.qbo.create_invoice(self.cust, [line(D('1'))], date(2026, 9, 1))
        sync = self.qbo.invoice(i)['SyncToken']
        stale = self.post('/invoice', {'Id': i, 'SyncToken': '99'}, operation='delete')
        self.assertEqual(stale.json()['Fault']['Error'][0]['code'], '5010')
        self.assertEqual(self.post('/invoice', {'Id': i, 'SyncToken': sync}, operation='delete').status_code, 200)
        resp = self.get(f'/invoice/{i}')
        self.assertEqual((resp.status_code, resp.json()['Fault']['Error'][0]['code']), (400, '610'))

    def test_cdc_with_deletions_and_the_30_day_window(self):
        start = self.qbo.now
        i = self.qbo.create_invoice(self.cust, [line(D('1'))], date(2026, 9, 1))
        pid = self.qbo.record_payment({i: D('1.15')}, date(2026, 9, 2))
        self.qbo.advance(minutes=5)
        mark = self.qbo.now
        self.qbo.delete_payment(pid)
        body = self.get('/cdc', entities='Payment,Invoice', changedSince=mark.isoformat()).json()
        resp = {k: v for qr in body['CDCResponse'][0]['QueryResponse'] for k, v in qr.items() if k in ('Payment', 'Invoice')}
        self.assertEqual(resp['Payment'], [{'domain': 'QBO', 'status': 'Deleted', 'Id': pid,
                                            'MetaData': {'LastUpdatedTime': resp['Payment'][0]['MetaData']['LastUpdatedTime']}}])
        self.assertEqual([r['Id'] for r in resp['Invoice']], [i])           # its balance changed
        old = self.get('/cdc', entities='Payment', changedSince=(start - timedelta(days=31)).isoformat())
        self.assertEqual(old.status_code, 400)

    def test_requestid_replays_the_first_answer(self):
        body = {'CustomerRef': {'value': self.cust}, 'Line': [line(D('5'))], 'TxnDate': '2026-09-01'}
        a = self.post('/invoice', body, requestid='r-1').json()['Invoice']
        b = self.post('/invoice', body, requestid='r-1').json()['Invoice']
        self.assertEqual(a['Id'], b['Id'])
        self.assertEqual(len(self.qbo.invoices()), 1)

    def test_minorversion_and_auth_are_required(self):
        with self.assertRaises(AssertionError):
            self.s.get(f'{API}/preferences', headers={'Authorization': f'Bearer {self.access}',
                                                      'Accept': 'application/json'})
        resp = self.s.get(f'{API}/preferences', params={'minorversion': '75'},
                          headers={'Authorization': 'Bearer nope', 'Accept': 'application/json'})
        self.assertEqual((resp.status_code, resp.json()['fault']['error'][0]['code']), (401, '3200'))
        other = self.s.get('https://sandbox-quickbooks.api.intuit.com/v3/company/123/preferences',
                           params={'minorversion': '75'},
                           headers={'Authorization': f'Bearer {self.access}', 'Accept': 'application/json'})
        self.assertEqual(other.status_code, 403)


class TokenTests(FakeBase):
    def token(self, **form):
        auth = base64.b64encode(b'cid:secret').decode()
        return self.s.post('https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer', data=form,
                           headers={'Authorization': f'Basic {auth}', 'Accept': 'application/json'})

    def test_code_exchange_refresh_rotation_and_revoke(self):
        code = self.qbo.authorize(redirect_uri='https://x.test/cb')
        self.assertEqual(self.token(grant_type='authorization_code', code=code,
                                    redirect_uri='https://other.test/cb').status_code, 400)
        code = self.qbo.authorize(redirect_uri='https://x.test/cb')
        first = self.token(grant_type='authorization_code', code=code, redirect_uri='https://x.test/cb').json()
        self.assertEqual((first['expires_in'], first['x_refresh_token_expires_in']), (3600, 8726400))
        second = self.token(grant_type='refresh_token', refresh_token=first['refresh_token']).json()
        self.assertNotEqual(second['refresh_token'], first['refresh_token'])
        dead = self.token(grant_type='refresh_token', refresh_token=first['refresh_token'])
        self.assertEqual((dead.status_code, dead.json()['error']), (400, 'invalid_grant'))
        self.s.post('https://developer.api.intuit.com/v2/oauth2/tokens/revoke',
                    json={'token': second['refresh_token']})
        self.assertEqual(self.token(grant_type='refresh_token', refresh_token=second['refresh_token']).status_code,
                         400)

    def test_access_tokens_expire_with_the_fake_clock(self):
        self.assertEqual(self.get('/preferences').status_code, 200)
        self.qbo.advance(seconds=3601)
        self.assertEqual(self.get('/preferences').status_code, 401)


class WebhookPayloadTests(TestCase):
    def test_both_formats_are_signed_and_parse(self):
        from core.accounting.quickbooks import parse_webhook, verify_webhook_signature
        qbo = FakeQBO(now=datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc))
        for fmt in ('classic', 'cloudevents'):
            body, sig = qbo.webhook_payload([{'name': 'Payment', 'id': '42', 'operation': 'Delete'}], fmt=fmt,
                                            key='k')
            self.assertTrue(verify_webhook_signature(body, sig, key='k'))
            self.assertFalse(verify_webhook_signature(body, sig, key='other'))
            [ev] = parse_webhook(json.loads(body))
            self.assertEqual((ev['tenant_id'], ev['resource_type'], ev['resource_id'], ev['event_type']),
                             (DEFAULT_REALM, 'Payment', '42', 'Delete'), fmt)
            self.assertEqual(ev['event_at'], datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc))
