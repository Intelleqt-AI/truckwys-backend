"""Contract tests: QuickBooksAdapter against recorded-style QBO responses.

The fixtures in fixtures/qbo/ are modelled on the Intuit Accounting API v3
reference examples (Invoice / CreditMemo / Bill with TxnTaxDetail, a Payment
with several LinkedTxn lines incl. a credit memo, TaxCode / TaxRate, Account,
Item, Class, Department, CompanyInfo, Preferences, a CDC response with
Deleted objects, Fault bodies, the token response, a BalanceSheet report,
both webhook formats). A replay transport serves them by method + path (and
query text), so these tests pin down (a) how the adapter parses QBO's JSON,
(b) the exact requests it sends and (c) how errors map to the neutral
exceptions.
"""
import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
from django.test import TestCase, override_settings
from django.utils import timezone
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from core.accounting.base import (
    AuthError, Contact, DocLine, Document, NotFound, PermanentError, RateLimited, TokenSet, TransientError,
)
from core.accounting.http import use_transport
from core.accounting.ratelimit import Limits, RateLimiter, redis_client

D = Decimal
FIXTURES = Path(__file__).parent / 'fixtures' / 'qbo'
REALM = '9341455130166501'
API = f'sandbox-quickbooks.api.intuit.com/v3/company/{REALM}'
SETTINGS = dict(QBO_CLIENT_ID='fake-client', QBO_CLIENT_SECRET='fake-secret', QBO_ENVIRONMENT='sandbox',
                QBO_REDIRECT_URI='https://api.truckwys.test/api/v1/integrations/quickbooks/callback/',
                QBO_MINOR_VERSION='75', QBO_WEBHOOK_VERIFIER_TOKEN='verifier')


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class Replay(BaseAdapter):
    """Serves fixtures for (METHOD, path regex[, query regex]); records every request."""

    def __init__(self):
        super().__init__()
        self.routes = []
        self.sent = []

    def on(self, method, path_regex, name, status=200, query=None):
        self.routes.insert(0, (method, re.compile(path_regex), re.compile(query) if query else None, name, status))
        return self

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        path = unquote(parts.path)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.sent.append(request)
        for method, rx, qrx, name, status in self.routes:
            if method != request.method or not rx.search(f'{parts.netloc}{path}'):
                continue
            if qrx is not None and not qrx.search(params.get('query', '')):
                continue
            if name == 'TIMEOUT':
                raise requests.exceptions.ReadTimeout('replayed timeout', request=request)
            data = fixture(name) if isinstance(name, str) else name
            headers = {}
            if isinstance(data, dict) and 'status' in data and 'body' in data:
                status, headers, data = data['status'], data['headers'], data['body']
            resp = requests.Response()
            resp.status_code = status
            resp.headers = CaseInsensitiveDict(headers)
            resp._content = json.dumps(data).encode() if not isinstance(data, str) else data.encode()
            resp.headers.setdefault('Content-Type', 'application/json')
            resp.url, resp.request, resp.encoding = request.url, request, 'utf-8'
            return resp
        raise AssertionError(f'Replay: no fixture for {request.method} {request.url}')

    def close(self):
        pass

    def last(self, method=None, contains=''):
        return [r for r in self.sent if (method is None or r.method == method) and contains in r.url][-1]

    @staticmethod
    def params(req):
        return dict(parse_qsl(urlsplit(req.url).query, keep_blank_values=True))

    @staticmethod
    def body(req):
        raw = req.body.decode() if isinstance(req.body, bytes) else req.body
        return json.loads(raw)


class ContractBase(TestCase):
    def setUp(self):
        self._settings = override_settings(**SETTINGS)
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.replay = Replay()
        self._t = use_transport(self.replay)
        self._t.__enter__()
        self.addCleanup(self._t.__exit__, None, None, None)
        from core.accounting.quickbooks import QuickBooksAdapter
        from core.accounting.tokens import store_tokens
        from core.models import AccountingConnection, Company
        self.company = Company.objects.create(company_name='Contract Haulage')
        self.conn = AccountingConnection.objects.create(
            company=self.company, provider='QBO', status=AccountingConnection.ACTIVE, tenant_id=REALM,
            tenant_name='Golden Haulage (Pty) Ltd')
        tok = fixture('token.json')
        store_tokens(self.conn, TokenSet(access_token=tok['access_token'], refresh_token='stored-refresh-token',
                                         expires_in=3600))
        self.access_token = tok['access_token']
        self.adapter = QuickBooksAdapter(self.conn)
        self.ns = f'test-qbo-contract-{uuid.uuid4().hex}'
        self.adapter.http.limiter = RateLimiter('QBO', Limits(per_minute=1000, per_day=None, concurrent=20),
                                                namespace=self.ns)
        self.addCleanup(self._flush)

    def _flush(self):
        try:
            r = redis_client()
            for k in r.scan_iter(f'{self.ns}:*'):
                r.delete(k)
        except Exception:
            pass

    def api(self, method, path, name, status=200, query=None):
        self.replay.on(method, rf'^{re.escape(API)}{path}$', name, status, query)

    def queries(self):
        return [Replay.params(r).get('query', '') for r in self.replay.sent if '/query' in r.url]


# ====================================================================== OAuth

class OAuthContractTests(ContractBase):
    def test_authorization_url(self):
        from core.accounting.quickbooks import QuickBooksAdapter
        url = QuickBooksAdapter.authorization_url('signed-state')
        parts = urlsplit(url)
        self.assertEqual(f'{parts.scheme}://{parts.netloc}{parts.path}', 'https://appcenter.intuit.com/connect/oauth2')
        q = dict(parse_qsl(parts.query))
        self.assertEqual(q, {'client_id': 'fake-client', 'response_type': 'code', 'state': 'signed-state',
                             'scope': 'com.intuit.quickbooks.accounting openid profile email',
                             'redirect_uri': SETTINGS['QBO_REDIRECT_URI']})
        self.assertIn('scope=com.intuit.quickbooks.accounting%20openid', url)

    def test_code_exchange_uses_basic_auth_and_keeps_the_refresh_lifetime(self):
        from core.accounting.quickbooks import QuickBooksAdapter
        self.replay.on('POST', r'^oauth\.platform\.intuit\.com/oauth2/v1/tokens/bearer$', 'token.json')
        tokens = QuickBooksAdapter.exchange_code('AB11code', realmId=REALM, state='x')
        req = self.replay.last('POST', 'tokens/bearer')
        self.assertEqual(req.headers['Authorization'], 'Basic ' + base64.b64encode(b'fake-client:fake-secret').decode())
        self.assertEqual(dict(parse_qsl(req.body)), {'grant_type': 'authorization_code', 'code': 'AB11code',
                                                     'redirect_uri': SETTINGS['QBO_REDIRECT_URI']})
        self.assertEqual((tokens.expires_in, tokens.refresh_expires_in), (3600, 8726400))
        self.assertTrue(tokens.refresh_token.startswith('AB11'))

    def test_refused_refresh_is_an_auth_error(self):
        self.replay.on('POST', r'tokens/bearer$', 'token_invalid_grant.json')
        with self.assertRaises(AuthError):
            self.adapter.refresh('dead-token')

    def test_401_refreshes_once_then_retries_with_the_new_token(self):
        self.replay.on('POST', r'tokens/bearer$', 'token.json')
        self.api('GET', '/preferences', 'preferences.json')
        self.api('GET', '/preferences', 'fault_auth_401.json')
        # First call 401 (route added last wins), then the token is refreshed;
        # swap the route so the retry succeeds.
        orig = self.replay.send
        calls = {'n': 0}

        def send(request, **kw):
            if '/preferences' in request.url:
                calls['n'] += 1
                if calls['n'] == 2:
                    self.replay.routes.pop(0)
            return orig(request, **kw)
        self.replay.send = send
        self.assertEqual(self.adapter.sync_blockers(), [])
        self.assertEqual(calls['n'], 2)
        self.assertTrue(any('tokens/bearer' in r.url for r in self.replay.sent))

    def test_revoke_posts_the_refresh_token(self):
        self.replay.on('POST', r'^developer\.api\.intuit\.com/v2/oauth2/tokens/revoke$', {'status': 200, 'headers': {},
                                                                                          'body': ''})
        self.adapter.revoke()
        req = self.replay.last('POST', 'revoke')
        self.assertEqual(Replay.body(req), {'token': 'stored-refresh-token'})
        self.assertTrue(req.headers['Authorization'].startswith('Basic '))


# ====================================================================== company + settings

class SettingsContractTests(ContractBase):
    def test_list_orgs_reads_the_callback_realm(self):
        self.conn.tenant_id = ''
        self.api('GET', f'/companyinfo/{REALM}', 'companyinfo.json')
        self.api('GET', '/preferences', 'preferences.json')
        [org] = self.adapter.list_orgs({'realmId': REALM, 'state': 's'})
        self.assertEqual((org.tenant_id, org.name, org.base_currency, org.country),
                         (REALM, 'Golden Haulage (Pty) Ltd', 'ZAR', 'ZA'))
        self.assertTrue(all(Replay.params(r)['minorversion'] == '75' for r in self.replay.sent))
        self.assertTrue(all(r.headers['Accept'] == 'application/json' for r in self.replay.sent))
        self.assertEqual(self.adapter.list_orgs({}), [])   # no realm: nothing authorised

    def test_multicurrency_usd_company_and_missing_custom_numbers(self):
        self.api('GET', f'/companyinfo/{REALM}', 'companyinfo.json')
        self.api('GET', '/preferences', 'preferences_usd_no_custom_numbers.json')
        [org] = self.adapter.list_orgs({'realmId': REALM})
        self.assertEqual(org.base_currency, 'USD')   # refused by the core's ZAR guard
        [blocker] = self.adapter.sync_blockers()
        self.assertIn('Custom transaction numbers', blocker)

    def test_tax_codes_become_neutral_tax_rates(self):
        self.api('GET', '/query', 'taxrate_query.json', query='FROM TaxRate')
        self.api('GET', '/query', 'taxcode_query.json', query='FROM TaxCode')
        rates = {r.code: r for r in self.adapter.get_tax_rates()}
        self.assertEqual((rates['3'].name, rates['3'].rate, rates['3'].revenue, rates['3'].expenses),
                         ('15.0% S', D('15.00'), True, True))
        self.assertEqual((rates['7'].rate, rates['7'].revenue, rates['7'].expenses), (D('15.00'), False, True))
        self.assertEqual((rates['8'].rate, rates['8'].revenue, rates['8'].expenses), (D('0.00'), True, True))
        self.assertEqual(rates['12'].rate, D('15.00'))   # a group: the sum of its rates
        self.assertEqual(self.queries()[0], 'SELECT * FROM TaxRate STARTPOSITION 1 MAXRESULTS 1000')

    def test_accounts_plus_sales_items(self):
        self.api('GET', '/query', 'account_query.json', query='FROM Account')
        self.api('GET', '/query', 'item_query.json', query='FROM Item')
        accts = {a.code: a for a in self.adapter.get_accounts()}
        self.assertEqual((accts['35'].name, accts['35'].is_bank, accts['35'].account_class), ('1000 FNB Business Cheque',
                                                                                            True, 'ASSET'))
        self.assertEqual((accts['79'].name, accts['79'].type, accts['79'].account_class),
                         ('Freight Income', 'Income', 'REVENUE'))
        self.assertEqual({c for c in accts if c.startswith('item:')}, {'item:1', 'item:5'})   # not inventory / group / no income
        self.assertEqual((accts['item:1'].type, accts['item:1'].account_class), ('ITEM', 'REVENUE'))

    def test_tracking_is_class_and_location(self):
        self.api('GET', '/preferences', 'preferences.json')
        self.api('GET', '/query', 'class_query.json', query='FROM Class')
        self.api('GET', '/query', 'department_query.json', query='FROM Department')
        cats = self.adapter.get_tracking()
        self.assertEqual([(c.id, c.name, [o.name for o in c.options]) for c in cats],
                         [('class', 'Class', ['ND 123-456']), ('location', 'Location', ['Johannesburg'])])
        self.api('POST', '/class', 'class_created.json')
        self.assertEqual(self.adapter.ensure_tracking_option('class', 'ND 123-456'), 'ND 123-456')
        self.assertEqual(self.adapter.ensure_tracking_option('class', 'GP 77-12 XY'), 'GP 77-12 XY')
        req = self.replay.last('POST', '/class')
        self.assertEqual(Replay.body(req), {'Name': 'GP 77-12 XY'})
        self.assertTrue(Replay.params(req)['requestid'].startswith('tw'))


# ====================================================================== contacts

class ContactContractTests(ContractBase):
    def test_find_by_email_and_name_and_never_by_vat(self):
        self.api('GET', '/query', 'customer_query.json', query='FROM Customer')
        self.assertEqual(self.adapter.find_contacts(vat_number='4123456789'), [])
        self.assertEqual(self.replay.sent, [])
        [c] = self.adapter.find_contacts(email="o'neil@acme.test")
        self.assertEqual(self.queries()[-1], "SELECT * FROM Customer WHERE PrimaryEmailAddr = 'o\\'neil@acme.test' "
                                             'MAXRESULTS 100')
        self.assertEqual((c.external_id, c.name, c.email, c.vat_number), ('58', 'Acme Mining (Pty) Ltd',
                                                                           'ar@acme.test', ''))   # masked: not used
        self.api('GET', '/query', {'QueryResponse': {}, 'time': 'x'}, query='FROM Vendor')
        self.assertEqual(self.adapter.find_contacts(name='acme mining', kind='SUPPLIER'), [])
        self.assertEqual(self.queries()[-1], "SELECT * FROM Vendor WHERE DisplayName LIKE '%acme mining%' MAXRESULTS 100")

    def test_create_customer_and_vendor_request_shapes(self):
        self.api('POST', '/customer', 'customer.json')
        self.adapter.upsert_contact(Contact(name='Acme Mining: (Pty) Ltd', email='ar@acme.test', vat_number='4123456789',
                                            registration_number='2001/012345/07', reference='TW-C-12'), kind='CUSTOMER')
        body = Replay.body(self.replay.last('POST', '/customer'))
        self.assertEqual(body, {'DisplayName': 'Acme Mining- (Pty) Ltd', 'CompanyName': 'Acme Mining- (Pty) Ltd',
                                'PrimaryEmailAddr': {'Address': 'ar@acme.test'}, 'PrimaryTaxIdentifier': '4123456789',
                                'Notes': 'TruckWys TW-C-12 · Reg 2001/012345/07'})
        self.api('POST', '/vendor', 'vendor_created.json')
        v = self.adapter.upsert_contact(Contact(name='N4 Toll Concession', vat_number='4111111111',
                                                reference='TW-S-4'), kind='SUPPLIER')
        body = Replay.body(self.replay.last('POST', '/vendor'))
        self.assertEqual((body['TaxIdentifier'], body['AcctNum']), ('4111111111', 'TW-S-4'))
        self.assertEqual((v.external_id, v.is_supplier, v.reference), ('61', True, 'TW-S-4'))

    def test_duplicate_display_name_is_a_permanent_error_with_the_reason(self):
        self.api('POST', '/customer', 'fault_duplicate_name.json')
        with self.assertRaises(PermanentError) as ctx:
            self.adapter.upsert_contact(Contact(name='Acme Mining (Pty) Ltd'), kind='CUSTOMER')
        self.assertNotIsInstance(ctx.exception, NotFound)
        self.assertIn('Duplicate Name Exists Error', str(ctx.exception))
        self.assertIn('(code 6240)', str(ctx.exception))


# ====================================================================== documents

def invoice_doc(**kw):
    lines = [DocLine(description='Freight JHB-DBN', quantity=D('1'), unit_price=D('18500.00'), net_amount=D('18500.00'),
                     tax_amount=D('2775.00'), account_code='item:1', tax_code='3',
                     tracking=[('Class', 'ND 123-456'), ('Location', 'Johannesburg')]),
             DocLine(description='Pallet handling', quantity=D('3'), unit_price=D('33.335'), net_amount=D('90.00'),
                     tax_amount=D('13.50'), account_code='item:1', tax_code='3', discount_percent=D('10')),
             DocLine(description='Cross-border leg', quantity=D('1'), unit_price=D('4200.00'), net_amount=D('4200.00'),
                     tax_amount=D('0.00'), account_code='item:1', tax_code='4')]
    base = dict(kind='INVOICE', number='INV-00001', contact_id='58', issue_date=date(2026, 9, 5),
                due_date=date(2026, 10, 5), lines=lines, reference='Load LD-0001', sub_total=D('22790.00'),
                total_tax=D('2788.50'), total=D('25578.50'))
    base.update(kw)
    return Document(**base)


class DocumentContractTests(ContractBase):
    def tax_routes(self):
        self.api('GET', '/query', 'taxrate_query.json', query='FROM TaxRate')
        self.api('GET', '/query', 'taxcode_query.json', query='FROM TaxCode')

    def test_invoice_request_shape_and_result(self):
        self.tax_routes()
        self.api('GET', '/query', 'class_query.json', query='FROM Class')
        self.api('GET', '/query', 'department_query.json', query='FROM Department')
        self.api('POST', '/invoice', 'invoice_created.json')
        res = self.adapter.push_invoice(invoice_doc(), idempotency_key='tw-1-INVOICE-9-abc')
        req = self.replay.last('POST', '/invoice')
        params = Replay.params(req)
        self.assertEqual(params['minorversion'], '75')
        self.assertTrue(params['requestid'].startswith('tw') and len(params['requestid']) <= 50)
        body = Replay.body(req)
        self.assertEqual({k: body[k] for k in ('DocNumber', 'TxnDate', 'DueDate', 'GlobalTaxCalculation', 'PrivateNote',
                                                'CustomerRef', 'DepartmentRef')},
                         {'DocNumber': 'INV-00001', 'TxnDate': '2026-09-05', 'DueDate': '2026-10-05',
                          'GlobalTaxCalculation': 'TaxExcluded', 'PrivateNote': 'Load LD-0001',
                          'CustomerRef': {'value': '58'}, 'DepartmentRef': {'value': '1'}})
        self.assertEqual(body['Line'][0], {
            'DetailType': 'SalesItemLineDetail', 'Amount': 18500, 'Description': 'Freight JHB-DBN',
            'SalesItemLineDetail': {'ItemRef': {'value': '1'}, 'Qty': 1, 'UnitPrice': 18500,
                                    'TaxCodeRef': {'value': '3'}, 'ClassRef': {'value': '5000000000000123401'}}})
        # 3 x 33.335 less 10 % = 90.00: QBO has no line discount -> 3 x 30 (exact).
        self.assertEqual((body['Line'][1]['Amount'], body['Line'][1]['SalesItemLineDetail']['Qty'],
                          body['Line'][1]['SalesItemLineDetail']['UnitPrice']), (90, 3, 30))
        # One TaxLine per rate = Σ TruckWys per-line VAT.
        self.assertEqual(body['TxnTaxDetail'], {'TotalTax': 2788.5, 'TaxLine': [
            {'DetailType': 'TaxLineDetail', 'Amount': 2788.5,
             'TaxLineDetail': {'TaxRateRef': {'value': '1'}, 'PercentBased': True, 'TaxPercent': 15,
                               'NetAmountTaxable': 18590}},
            {'DetailType': 'TaxLineDetail', 'Amount': 0,
             'TaxLineDetail': {'TaxRateRef': {'value': '3'}, 'PercentBased': True, 'TaxPercent': 0,
                               'NetAmountTaxable': 4200}}]})
        self.assertEqual((res.external_id, res.external_number, res.version, res.status),
                         ('130', 'INV-00001', '0', 'OPEN'))
        self.assertEqual((res.sub_total, res.total_tax, res.total), (D('22790.00'), D('2788.50'), D('25578.50')))
        self.assertEqual(res.url, 'https://app.sandbox.qbo.intuit.com/app/invoice?txnId=130')

    def test_multi_rate_tax_code_is_refused(self):
        self.tax_routes()
        doc = invoice_doc(lines=[DocLine(description='x', quantity=D('1'), unit_price=D('100'), net_amount=D('100'),
                                         tax_amount=D('15'), account_code='item:1', tax_code='12')])
        with self.assertRaises(PermanentError) as ctx:
            self.adapter.push_invoice(doc)
        self.assertIn('several tax rates', str(ctx.exception))

    def test_bill_request_shape(self):
        self.tax_routes()
        self.api('POST', '/bill', 'bill.json')
        doc = Document(kind='BILL', number='TOLL-778', contact_id='61', issue_date=date(2026, 9, 3),
                       due_date=date(2026, 9, 3), reference='EXP-1-1', amounts_include_tax=True,
                       lines=[DocLine(description='N4 tolls [EXP-1-1]', quantity=D('1'), unit_price=D('1150.00'),
                                      net_amount=D('1150.00'), tax_amount=D('150.00'), account_code='80',
                                      tax_code='3')],
                       sub_total=D('1000.00'), total_tax=D('150.00'), total=D('1150.00'))
        res = self.adapter.push_bill(doc, idempotency_key='k')
        body = Replay.body(self.replay.last('POST', '/bill'))
        self.assertEqual((body['VendorRef'], body['GlobalTaxCalculation'], body['DocNumber']),
                         ({'value': '61'}, 'TaxInclusive', 'TOLL-778'))
        self.assertEqual(body['Line'][0], {'DetailType': 'AccountBasedExpenseLineDetail', 'Amount': 1000,
                                           'Description': 'N4 tolls [EXP-1-1]',
                                           'AccountBasedExpenseLineDetail': {'AccountRef': {'value': '80'},
                                                                             'TaxCodeRef': {'value': '3'},
                                                                             'TaxInclusiveAmt': 1150}})
        self.assertEqual(body['TxnTaxDetail']['TaxLine'][0]['TaxLineDetail']['TaxRateRef'], {'value': '2'})  # purchase rate
        self.assertEqual((res.total, res.total_tax, res.sub_total), (D('1150.00'), D('150.00'), D('1000.00')))

    def test_find_document_by_number_and_void_detection(self):
        self.api('GET', '/query', 'invoice_query_docnumber.json', query="DocNumber = 'INV-00001'")
        found = self.adapter.find_document('INVOICE', 'INV-00001')
        self.assertEqual((found.external_id, found.total), ('130', D('25578.50')))
        self.assertEqual(self.queries()[-1], "SELECT * FROM Invoice WHERE DocNumber = 'INV-00001'")
        self.api('GET', '/query', 'invoice_query_ids.json', query='Id IN')
        states = {s.external_id: s for s in self.adapter.get_invoice_states(['130', '131'])}
        self.assertEqual(self.queries()[-1],
                         "SELECT * FROM Invoice WHERE Id IN ('130','131') STARTPOSITION 1 MAXRESULTS 1000")
        self.assertEqual((states['131'].status, states['130'].status), ('VOIDED', 'OPEN'))
        self.assertEqual((states['130'].amount_due, states['130'].amount_paid + states['130'].amount_credited),
                         (D('20578.50'), D('5000.00')))

    def test_void_and_delete_send_id_and_sync_token(self):
        self.api('GET', '/invoice/130', 'invoice.json')
        self.api('POST', '/invoice', 'invoice.json')
        self.adapter.void_invoice('130')
        req = self.replay.last('POST', '/invoice')
        self.assertEqual((Replay.params(req)['operation'], Replay.body(req)), ('void', {'Id': '130', 'SyncToken': '3'}))
        self.adapter.discard_document('INVOICE', '130')
        req = self.replay.last('POST', '/invoice')
        self.assertEqual(Replay.params(req)['operation'], 'delete')

    def test_credit_memo_application_is_a_zero_payment(self):
        self.api('GET', '/creditmemo/150', 'creditmemo_ours.json')
        self.api('POST', '/payment', 'payment_credit_application.json')
        self.adapter.allocate_credit_note('150', '130', D('1000.00'), date(2026, 9, 20))
        body = Replay.body(self.replay.last('POST', '/payment'))
        self.assertEqual((body['TotalAmt'], body['TxnDate'], body['CustomerRef']), (0, '2026-09-20', {'value': '58'}))
        self.assertEqual(body['Line'], [{'Amount': 1000, 'LinkedTxn': [{'TxnId': '130', 'TxnType': 'Invoice'}]},
                                        {'Amount': 1000, 'LinkedTxn': [{'TxnId': '150', 'TxnType': 'CreditMemo'}]}])

    def test_backfill_payment_ids_are_per_invoice(self):
        self.api('GET', '/invoice/130', 'invoice.json')
        self.api('POST', '/payment', 'payment_multi.json')
        res = self.adapter.push_payment(invoice_external_id='130', amount=D('4000.00'), on=date(2026, 9, 12),
                                        account_code='35', reference='TruckWys PAY-00001', idempotency_key='k')
        self.assertEqual(res.external_id, '201:130')
        body = Replay.body(self.replay.last('POST', '/payment'))
        self.assertEqual((body['DepositToAccountRef'], body['PaymentRefNum']), ({'value': '35'}, 'TruckWys PAY-00001'))
        res = self.adapter.push_overpayment(contact_id='58', amount=D('56.83'), on=date(2026, 7, 27), account_code='35',
                                            reference='TruckWys PAY-4 (overpayment)', idempotency_key='k2')
        self.assertEqual(res.external_id, '201')
        self.assertEqual(Replay.body(self.replay.last('POST', '/payment'))['Line'], [])


# ====================================================================== payments back

class PaymentsContractTests(ContractBase):
    def setUp(self):
        super().setUp()
        from core.models import ExternalLink
        ExternalLink.objects.create(company=self.company, connection=self.conn, provider='QBO',
                                    object_type='CREDIT_NOTE', local_id=1, external_id='150', status='SYNCED')

    def test_invoice_state_splits_money_from_credit(self):
        self.api('GET', '/invoice/130', 'invoice.json')
        self.api('GET', '/payment/201', 'payment_multi.json')
        self.api('GET', '/payment/202', 'payment_credit_application.json')
        self.api('GET', '/creditmemo/77', 'creditmemo.json')
        st = self.adapter.get_invoice_state('130')
        got = [(s.kind, s.external_id, s.amount, s.date, s.source_id, s.source_number) for s in st.settlements]
        self.assertEqual(got, [
            ('CREDIT_NOTE', '201:77:130', D('115.00'), date(2026, 9, 12), '77', 'QCM-1'),   # foreign credit memo
            ('PAYMENT', '201:130', D('3885.00'), date(2026, 9, 12), '201', ''),
            ('CREDIT_NOTE', '202:150:130', D('1000.00'), date(2026, 9, 20), '150', ''),     # ours: not read again
        ])
        self.assertEqual(st.settlements[1].reference, 'EFT 991')
        self.assertEqual((st.total, st.total_tax, st.sub_total, st.amount_due, st.amount_paid, st.amount_credited),
                         (D('25578.50'), D('2788.50'), D('22790.00'), D('20578.50'), D('3885.00'), D('1115.00')))
        self.assertFalse(any('/creditmemo/150' in r.url for r in self.replay.sent))

    def test_cdc_changes_including_deletions(self):
        self.api('GET', '/cdc', 'cdc.json')
        since = timezone.now() - timedelta(days=2)
        changes = self.adapter.list_payments_since(since) + self.adapter.list_credit_note_allocations(since)
        self.assertEqual(len([r for r in self.replay.sent if '/cdc' in r.url]), 1)   # one CDC call for both
        req = self.replay.last('GET', '/cdc')
        self.assertEqual(Replay.params(req)['entities'], 'Payment,CreditMemo,Invoice')
        self.assertTrue(Replay.params(req)['changedSince'].endswith('+00:00'))
        got = [(c.kind, c.status, c.external_id, c.invoice_external_id, c.amount) for c in changes]
        self.assertEqual(got, [
            ('PAYMENT', 'DELETED', '199', '', D('0')),
            ('CREDIT_NOTE', 'ACTIVE', '201:77:130', '130', D('115.00')),
            ('PAYMENT', 'ACTIVE', '201:130', '130', D('3885.00')),
            ('PAYMENT', 'ACTIVE', '201:140', '140', D('2015.00')),
            ('PAYMENT', 'DELETED', '', '133', D('0')),
        ])

    def test_old_cursor_falls_back_to_a_query(self):
        self.api('GET', '/query', {'QueryResponse': {}, 'time': 'x'}, query='MetaData.LastUpdatedTime')
        self.adapter.list_payments_since(timezone.now() - timedelta(days=45))
        self.assertFalse(any('/cdc' in r.url for r in self.replay.sent))
        self.assertTrue(all("MetaData.LastUpdatedTime >= '" in q for q in self.queries()))
        self.assertTrue(self.conn.events.filter(level='WARNING', message__contains='30-day').exists())

    def test_credit_note_detail_of_a_foreign_credit_memo(self):
        self.api('GET', '/creditmemo/77', 'creditmemo.json')
        self.api('GET', '/payment/201', 'payment_multi.json')
        self.api('GET', '/query', 'taxrate_query.json', query='FROM TaxRate')
        self.api('GET', '/query', 'taxcode_query.json', query='FROM TaxCode')
        detail = self.adapter.get_credit_note_detail('77')
        self.assertEqual(detail, {'number': 'QCM-1', 'date': date(2026, 9, 11), 'total': D('115.00'),
                                  'remaining': D('0.00'),
                                  'allocations': [{'invoice_id': '130', 'amount': D('115.00'), 'date': date(2026, 9, 12)}],
                                  'lines': [{'description': 'Damaged pallet', 'net': D('100.00'), 'tax': D('15.00'),
                                             'tax_code': '3'}]})

    def test_debtors_from_the_balance_sheet(self):
        self.api('GET', '/reports/BalanceSheet', 'balance_sheet.json')
        self.assertEqual(self.adapter.debtors_at(date(2026, 8, 31)), D('234110.01'))
        p = Replay.params(self.replay.last('GET', 'BalanceSheet'))
        self.assertEqual((p['start_date'], p['end_date'], p['accounting_method']), ('2026-08-31', '2026-08-31', 'Accrual'))

    def test_invoices_for_a_payment_event(self):
        self.api('GET', '/payment/201', 'payment_multi.json')
        self.assertEqual(self.adapter.invoices_for_event('PAYMENT', '201', 'Create'), ['130', '140'])
        self.assertEqual(self.adapter.invoices_for_event('PAYMENT', '999', 'Delete'), [])   # nothing TruckWys-side


# ====================================================================== errors

class ErrorMappingTests(ContractBase):
    def call(self):
        return self.adapter.sync_blockers()

    def test_fault_codes_map_to_neutral_errors(self):
        self.assertIn('Make sure all your transactions have a VAT rate before you save. (code 6000)',
                      self._message('fault_business_validation.json'))
        cases = [('fault_object_not_found.json', NotFound), ('fault_stale_object.json', TransientError),
                 ('fault_business_validation.json', PermanentError), ('fault_system_500.json', TransientError),
                 ('fault_throttle_in_400.json', RateLimited)]
        for name, exc in cases:
            with self.subTest(name):
                self.adapter._prefs = None
                self.api('GET', '/preferences', name)
                with self.assertRaises(exc) as ctx:
                    self.call()
                self.assertIs(type(ctx.exception), exc)

    def _message(self, name):
        self.adapter._prefs = None
        self.api('GET', '/preferences', name)
        try:
            self.call()
        except Exception as exc:
            return str(exc)
        return ''

    def test_429_honours_retry_after_or_waits_a_minute(self):
        fx = fixture('fault_throttle_429.json')
        self.api('GET', '/preferences', fx)
        with self.assertRaises(RateLimited) as ctx:
            self.call()
        self.assertEqual(ctx.exception.retry_after, 60)
        self.adapter.http.limiter.r.delete(f'{self.ns}:QBO:{REALM}:blocked')
        fx['headers']['Retry-After'] = '17'
        self.api('GET', '/preferences', fx)
        with self.assertRaises(RateLimited) as ctx:
            self.call()
        self.assertEqual(ctx.exception.retry_after, 17)

    def test_timeout_is_transient(self):
        self.api('GET', '/preferences', 'TIMEOUT')
        with self.assertRaises(TransientError):
            self.call()


class LinksAndWebhookTests(ContractBase):
    def test_links_by_environment(self):
        self.assertEqual(self.adapter.web_url('CREDIT_NOTE', '77'),
                         'https://app.sandbox.qbo.intuit.com/app/creditmemo?txnId=77')
        with override_settings(QBO_ENVIRONMENT='production'):
            self.assertEqual(self.adapter.web_url('CONTACT_CUSTOMER', '58'),
                             'https://app.qbo.intuit.com/app/customerdetail?nameId=58')
            self.assertEqual(self.adapter.web_url('BILL', '310'), 'https://app.qbo.intuit.com/app/bill?txnId=310')
            self.assertEqual(self.adapter.org_url(), 'https://app.qbo.intuit.com/app/homepage')
            from core.accounting.quickbooks import api_base
            self.assertEqual(api_base(), 'https://quickbooks.api.intuit.com')

    def test_webhook_signature_and_both_formats(self):
        from core.accounting.quickbooks import parse_webhook, verify_webhook_signature
        raw = (FIXTURES / 'webhook_classic.json').read_bytes()
        sig = base64.b64encode(hmac.new(b'verifier', raw, hashlib.sha256).digest()).decode()
        self.assertTrue(verify_webhook_signature(raw, sig))
        self.assertFalse(verify_webhook_signature(raw + b' ', sig))
        self.assertFalse(verify_webhook_signature(raw, ''))
        classic = parse_webhook(fixture('webhook_classic.json'))
        self.assertEqual([(e['tenant_id'], e['resource_type'], e['resource_id'], e['event_type']) for e in classic],
                         [(REALM, 'Payment', '201', 'Create'), (REALM, 'Invoice', '130', 'Update'),
                          (REALM, 'CreditMemo', '77', 'Delete')])
        self.assertEqual(classic[0]['event_at'], datetime(2026, 9, 12, 15, 0, 1, tzinfo=dt_timezone.utc))
        cloud = parse_webhook(fixture('webhook_cloudevents.json'))
        self.assertEqual([(e['tenant_id'], e['resource_type'], e['resource_id'], e['event_type']) for e in cloud],
                         [(REALM, 'Payment', '201', 'Create'), (REALM, 'CreditMemo', '77', 'Delete')])
        # The same change in either format has the same dedupe key.
        self.assertEqual(classic[0]['dedupe_key'], cloud[0]['dedupe_key'])
