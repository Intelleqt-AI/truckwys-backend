"""Contract tests: XeroAdapter against recorded-style Xero responses.

The fixtures in fixtures/xero/ are modelled on the Xero Accounting API docs
examples (/Date(ms+0000)/ dates, ValidationException bodies, paged lists, a
DELETED payment, credit note / overpayment / prepayment allocations,
rate-limit headers). A tiny replay transport serves them by method + path, so
these tests pin down (a) how the adapter parses Xero's JSON, (b) the exact
requests it sends and (c) how HTTP errors map to the neutral exceptions.
"""
import base64
import json
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit

import requests
from django.test import TestCase, override_settings
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from core.accounting.base import (
    AuthError, Contact, DocLine, Document, NotFound, PermanentError, RateLimited, TokenSet, TransientError,
)
from core.accounting.http import use_transport
from core.accounting.ratelimit import Limits, RateLimiter, redis_client

D = Decimal
FIXTURES = Path(__file__).parent / 'fixtures' / 'xero'
API = '/api.xro/2.0'
SETTINGS = dict(XERO_CLIENT_ID='fake-client', XERO_CLIENT_SECRET='fake-secret',
                XERO_REDIRECT_URI='https://api.truckwys.test/api/v1/integrations/xero/callback/', XERO_SCOPES='')

INVOICE = '243216c5-369e-4056-ac67-05388f86dc81'
INVOICE_OTHER = '4f7a9c2e-8b1d-4e3f-a5c6-7d8e9f0a1b2c'
CN = '0032e4a6-1f2c-4a5b-9c7d-8e9f0a1b2c3d'
OVP = 'ed7f6a49-8a1c-4c3e-b3d2-6f5e4d3c2b1a'
PRE = 'b1a2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d'
PAY1 = 'b26fd49a-cbae-470a-a8f8-bcbc119e0379'
PAY2 = '7e1c3a5b-9d2f-4b6e-8a0c-1e3f5a7b9c2d'
CONTACT = '9b9ba9e5-e907-4b4e-8210-54d82b0aa479'
A1, A2, A3 = ('c7ba3c1b-1a2b-4c3d-8e4f-5a6b7c8d9e0f', 'd8cb4d2c-2b3c-4d4e-9f5a-6b7c8d9e0f1a',
              'e9dc5e3d-3c4d-4e5f-a06b-7c8d9e0f1a2b')
A4, A5 = 'fa0e6f4e-4d5e-4f6a-b17c-8d9e0f1a2b3c', '0b1f7a5f-5e6f-4a7b-c28d-9e0f1a2b3c4d'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class Replay(BaseAdapter):
    """Serves fixtures for (METHOD, path regex) routes; records every request.
    A fixture with top-level 'status' + 'body' is a recorded error response."""

    def __init__(self):
        super().__init__()
        self.routes = []
        self.sent = []

    def on(self, method, path_regex, name, status=200):
        import re
        self.routes.insert(0, (method, re.compile(path_regex), name, status))
        return self

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        path = unquote(parts.path)
        self.sent.append(request)
        for method, rx, name, status in self.routes:
            if method == request.method and rx.search(f'{parts.netloc}{path}'):
                if name == 'TIMEOUT':
                    raise requests.exceptions.ReadTimeout('replayed timeout', request=request)
                data = fixture(name)
                headers = {}
                if isinstance(data, dict) and 'status' in data and 'body' in data:
                    status, headers, data = data['status'], data['headers'], data['body']
                resp = requests.Response()
                resp.status_code = status
                resp.headers = CaseInsensitiveDict(headers)
                if isinstance(data, str):
                    resp._content = data.encode()
                else:
                    resp._content = json.dumps(data).encode()
                    resp.headers.setdefault('Content-Type', 'application/json; charset=utf-8')
                resp.url, resp.request, resp.encoding = request.url, request, 'utf-8'
                return resp
        raise AssertionError(f'Replay: no fixture for {request.method} {request.url}')

    def close(self):
        pass

    def last(self, method=None, contains=''):
        rows = [r for r in self.sent if (method is None or r.method == method) and contains in r.url]
        return rows[-1]

    @staticmethod
    def params(req):
        return dict(parse_qsl(urlsplit(req.url).query, keep_blank_values=True))


class ContractBase(TestCase):
    def setUp(self):
        self._settings = override_settings(**SETTINGS)
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.replay = Replay()
        self._t = use_transport(self.replay)
        self._t.__enter__()
        self.addCleanup(self._t.__exit__, None, None, None)
        from core.accounting.tokens import store_tokens
        from core.accounting.xero import XeroAdapter
        from core.models import AccountingConnection, Company
        self.tenant = f'contract-{uuid.uuid4()}'
        company = Company.objects.create(company_name='Contract Haulage')
        self.conn = AccountingConnection.objects.create(
            company=company, provider='XERO', status=AccountingConnection.ACTIVE, tenant_id=self.tenant,
            tenant_name='Golden Haulage (Pty) Ltd', provider_connection_id='e1eede29-f875-4a5d-8470-17f6a29a88b1',
            short_code='!gH7kQ')
        tok = fixture('token.json')
        store_tokens(self.conn, TokenSet(access_token=tok['access_token'], refresh_token='stored-refresh-token',
                                         expires_in=1800))
        self.access_token = tok['access_token']
        self.adapter = XeroAdapter(self.conn)
        self.ns = f'test-xero-contract-{uuid.uuid4().hex}'
        self.adapter.http.limiter = RateLimiter('XERO', Limits(per_minute=1000, per_day=None, concurrent=20),
                                                namespace=self.ns)
        self.addCleanup(self._flush)

    def _flush(self):
        try:
            r = redis_client()
            for k in r.scan_iter(f'{self.ns}:*'):
                r.delete(k)
        except Exception:
            pass

    def api(self, method, path, name, status=200):
        self.replay.on(method, rf'^api\.xero\.com{API}{path}$', name, status)


# ====================================================================== (a) parsing

class ParseTests(ContractBase):

    def test_settings(self):
        self.api('GET', '/TaxRates', 'tax_rates.json')
        self.api('GET', '/Accounts', 'accounts.json')
        self.api('GET', '/TrackingCategories', 'tracking_categories.json')
        rates = {r.code: r for r in self.adapter.get_tax_rates()}
        self.assertEqual(rates['OUTPUT2'].rate, D('15.00'))
        self.assertTrue(rates['OUTPUT2'].revenue)
        self.assertFalse(rates['OUTPUT2'].expenses)
        self.assertEqual((rates['OUTPUT3'].rate, rates['OUTPUT3'].status), (D('14.00'), 'ARCHIVED'))
        accounts = {a.code: a for a in self.adapter.get_accounts()}
        self.assertEqual(sorted(accounts), ['090', '200', '201', '210', '449', '610', '881'])   # Petty Cash: no code
        self.assertTrue(accounts['090'].is_bank and accounts['881'].is_bank)
        self.assertFalse(accounts['200'].is_bank)
        self.assertEqual((accounts['210'].status, accounts['449'].account_class), ('ARCHIVED', 'EXPENSE'))
        cats = {c.name: c for c in self.adapter.get_tracking()}
        self.assertEqual([o.name for o in cats['Region'].options], ['Gauteng', 'KZN'])     # archived dropped
        self.assertEqual(cats['Vehicle'].options, [])

    def test_orgs_from_connections_and_organisation(self):
        self.replay.on('GET', r'^api\.xero\.com/connections$', 'connections.json')
        self.api('GET', '/Organisation', 'organisation.json')
        orgs = self.adapter.list_orgs()
        self.assertEqual(len(orgs), 1)                               # PRACTICEMANAGER skipped
        o = orgs[0]
        self.assertEqual((o.tenant_id, o.name, o.base_currency, o.country, o.short_code, o.connection_id),
                         ('0c3f7d9e-2a4b-4e6f-9a1b-3c5d7e9f1a2b', 'Golden Haulage (Pty) Ltd', 'ZAR', 'ZA',
                          '!gH7kQ', 'e1eede29-f875-4a5d-8470-17f6a29a88b1'))
        conn_req = self.replay.last('GET', '/connections')
        self.assertEqual(Replay.params(conn_req), {'authEventId': '6a0c2e4b-8d1f-4b3a-9c5e-7f9a1b3c5d7e'})
        self.assertEqual(self.replay.last('GET', '/Organisation').headers['Xero-tenant-id'],
                         '0c3f7d9e-2a4b-4e6f-9a1b-3c5d7e9f1a2b')

    def test_contacts(self):
        self.api('GET', '/Contacts', 'contacts.json')
        found = self.adapter.find_contacts(vat_number='4123456789')
        self.assertEqual([c.external_id for c in found], [CONTACT])   # the archived one is dropped
        c = found[0]
        self.assertEqual((c.name, c.email, c.vat_number, c.registration_number, c.reference, c.is_customer),
                         ('Acme Mining (Pty) Ltd', 'ar@acme-mining.co.za', '4123456789', '2011/004455/07',
                          'TW-C-17', True))
        self.assertEqual([c.external_id for c in self.adapter.list_contacts()], [CONTACT])

    def test_invoice_state_with_every_kind_of_settlement(self):
        self.api('GET', f'/Invoices/{INVOICE}', 'invoice_paid.json')
        self.api('GET', f'/CreditNotes/{CN}', 'credit_note.json')
        self.api('GET', f'/Overpayments/{OVP}', 'overpayment.json')
        self.api('GET', f'/Prepayments/{PRE}', 'prepayment.json')
        s = self.adapter.get_invoice_state(INVOICE)
        self.assertEqual((s.number, s.status, s.contact_id, s.issue_date), ('INV-0042', 'PAID', CONTACT,
                                                                            date(2025, 7, 1)))
        self.assertEqual((s.sub_total, s.total_tax, s.total, s.amount_due, s.amount_paid, s.amount_credited),
                         (D('10000.00'), D('1500.00'), D('11500.00'), D('0.00'), D('8000.00'), D('3500.00')))
        self.assertEqual(s.updated_at, datetime(2025, 7, 15, 13, 4, 55, 610000, tzinfo=dt_timezone.utc))
        got = sorted((x.kind, x.external_id, x.source_id, x.amount, x.date, x.source_number, x.reference)
                     for x in s.settlements)
        self.assertEqual(got, sorted([
            ('PAYMENT', PAY1, PAY1, D('8000.00'), date(2025, 7, 10), '', 'EFT ACME 0710'),
            ('CREDIT_NOTE', A1, CN, D('1150.00'), date(2025, 7, 12), 'CN-0007', ''),   # A2/A3: other invoice
            ('OVERPAYMENT', A4, OVP, D('1500.00'), date(2025, 7, 14), '', ''),
            ('PREPAYMENT', A5, PRE, D('850.00'), date(2025, 7, 15), 'Deposit PO-889', ''),
        ]))
        self.assertEqual(sum((x.amount for x in s.settlements), D0), s.total)

    def test_bulk_states_and_sales_documents(self):
        self.api('GET', '/Invoices', 'invoices_list.json')
        self.api('GET', '/CreditNotes', 'credit_notes_list.json')
        states = self.adapter.get_invoice_states([INVOICE, INVOICE_OTHER, ''])
        self.assertEqual([x.number for x in states], ['INV-0042', 'INV-0043', 'INV-0044', 'INV-0045', 'INV-0046'])
        self.assertEqual(Replay.params(self.replay.last('GET', '/Invoices')),
                         {'IDs': f'{INVOICE},{INVOICE_OTHER}', 'page': '1'})
        docs = self.adapter.list_sales_documents(date(2025, 7, 1), date(2025, 7, 31))
        self.assertEqual([(d.kind, d.number, d.status) for d in docs], [
            ('INVOICE', 'INV-0042', 'PAID'), ('INVOICE', 'INV-0045', 'AUTHORISED'), ('INVOICE', 'INV-0046', 'VOIDED'),
            ('CREDIT_NOTE', 'CN-0007', 'AUTHORISED')])
        cn = docs[-1]
        self.assertEqual((cn.issue_date, cn.total, cn.amount_due), (date(2025, 7, 12), D('2300.00'), D('650.00')))

    def test_payments_since_reports_deleted_payments(self):
        self.api('GET', '/Payments', 'payments_list.json')
        rows = self.adapter.list_payments_since(datetime(2025, 7, 10, 10, 30, 15, tzinfo=dt_timezone(timedelta(hours=2))))
        self.assertEqual([(p.external_id, p.status, p.amount, p.date, p.invoice_external_id, p.kind) for p in rows], [
            (PAY1, 'ACTIVE', D('8000.00'), date(2025, 7, 10), INVOICE, 'PAYMENT'),
            (PAY2, 'DELETED', D('500.00'), date(2025, 7, 11), INVOICE, 'PAYMENT')])
        self.assertEqual(rows[1].updated_at, datetime(2025, 7, 12, 6, 55, 41, 3000, tzinfo=dt_timezone.utc))

    def test_allocations_and_unallocated_credits(self):
        self.api('GET', '/CreditNotes', 'credit_notes_list.json')
        self.api('GET', '/Overpayments', 'overpayments_list.json')
        self.api('GET', '/Prepayments', 'prepayments_list.json')
        changes = self.adapter.list_credit_note_allocations(None)
        got = [(c.kind, c.external_id, c.source_id, c.invoice_external_id, c.amount, c.date) for c in changes]
        self.assertIn(('CREDIT_NOTE', A1, CN, INVOICE, D('1150.00'), date(2025, 7, 12)), got)
        self.assertIn(('OVERPAYMENT', A4, OVP, INVOICE, D('1500.00'), date(2025, 7, 14)), got)
        self.assertIn(('PREPAYMENT', A5, PRE, INVOICE, D('850.00'), date(2025, 7, 15)), got)
        self.assertEqual(changes[0].updated_at, datetime(2025, 7, 16, 8, 1, 7, 333000, tzinfo=dt_timezone.utc))
        credits = self.adapter.list_unallocated_credits()
        self.assertEqual(sorted((c.kind, c.external_id, c.contact_id, c.remaining, c.number) for c in credits), [
            ('CREDIT_NOTE', CN, CONTACT, D('650.00'), 'CN-0007'), ('OVERPAYMENT', OVP, CONTACT, D('500.00'), '')])

    def test_allocation_flagged_deleted_is_not_reported_as_active(self):
        """Xero's Allocation carries IsDeleted. get_invoice_state and
        get_credit_note_detail skip deleted allocations; the allocation poll
        must not report one as ACTIVE either."""
        self.api('GET', '/CreditNotes', 'credit_notes_list.json')
        self.api('GET', '/Overpayments', 'overpayments_list.json')
        self.api('GET', '/Prepayments', 'prepayments_list.json')
        a3 = [c for c in self.adapter.list_credit_note_allocations(None) if c.external_id == A3]
        self.assertTrue(all(c.status == 'DELETED' for c in a3), a3)

    def test_credit_note_detail(self):
        self.api('GET', f'/CreditNotes/{CN}', 'credit_note.json')
        d = self.adapter.get_credit_note_detail(CN)
        self.assertEqual((d['number'], d['date'], d['total'], d['remaining']),
                         ('CN-0007', date(2025, 7, 12), D('2300.00'), D('650.00')))
        self.assertEqual([(a['invoice_id'], a['amount']) for a in d['allocations']],
                         [(INVOICE, D('1150.00')), (INVOICE_OTHER, D('500.00'))])          # IsDeleted dropped
        self.assertEqual(d['lines'][1], {'description': 'Fuel surcharge credit', 'net': D('1000.00'),
                                         'tax': D('150.00'), 'tax_code': 'OUTPUT2'})
        self.assertEqual(Replay.params(self.replay.last('GET')), {'unitdp': '4'})

    def test_balance_sheet_debtors(self):
        self.api('GET', '/Reports/BalanceSheet', 'balance_sheet.json')
        self.assertEqual(self.adapter.debtors_at(date(2025, 7, 31)), D('48250.75'))
        self.assertEqual(Replay.params(self.replay.last('GET')), {'date': '2025-07-31', 'standardLayout': 'true'})

    def test_receivables_by_contact(self):
        self.api('GET', '/Invoices', 'invoices_list.json')
        self.api('GET', '/CreditNotes', 'credit_notes_list.json')
        self.api('GET', '/Overpayments', 'overpayments_list.json')
        self.api('GET', '/Prepayments', 'prepayments_list.json')
        # every row of the list counts (the replay ignores where): 0 + 11500 + 0 + 75 + 0 ... minus credits
        out = self.adapter.receivables_by_contact()
        self.assertEqual(out, {CONTACT: D('0.00') + D('11500.00') + D('0.00') + D('75.00') + D('0.00')
                               - D('650.00') - D('500.00')})
        wheres = [Replay.params(r).get('where') for r in self.replay.sent]
        self.assertIn('Type=="ACCREC"&&Status=="AUTHORISED"', wheres)


D0 = D('0.00')


# ====================================================================== (b) request shapes

class RequestShapeTests(ContractBase):

    def sales_doc(self):
        return Document(kind='INVOICE', number='INV-0050', contact_id=CONTACT, issue_date=date(2025, 7, 20),
                        due_date=date(2025, 8, 19), reference='PO-889', lines=[
                            DocLine(description='Freight JHB-DBN 34t', quantity=D('1'), unit_price=D('8695.6522'),
                                    net_amount=D('8695.65'), tax_amount=D('1304.35'), account_code='200',
                                    tax_code='OUTPUT2', tracking=[('Region', 'Gauteng')]),
                            DocLine(description='Pallets', quantity=D('3.000'), unit_price=D('33.3350'),
                                    net_amount=D('90.00'), tax_amount=D('13.50'), account_code='260',
                                    tax_code='OUTPUT2', discount_percent=D('10'))])

    def test_create_invoice_request(self):
        self.api('PUT', '/Invoices', 'invoice_draft_created.json')
        res = self.adapter.push_invoice(self.sales_doc(), idempotency_key='tw-inv-50-v1')
        self.assertEqual((res.external_id, res.external_number, res.status, res.total),
                         ('8e5c2e1a-7b3d-4f9a-b2c6-1d4e7f0a3b5c', 'INV-0050', 'DRAFT', D('11500.00')))
        req = self.replay.last('PUT')
        self.assertEqual(Replay.params(req), {'unitdp': '4', 'summarizeErrors': 'false'})
        self.assertEqual(req.headers['Xero-tenant-id'], self.tenant)
        self.assertEqual(req.headers['Idempotency-Key'], 'tw-inv-50-v1')
        self.assertEqual(req.headers['Authorization'], f'Bearer {self.access_token}')
        self.assertEqual(req.headers['Content-Type'], 'application/json')
        self.assertEqual(req.headers['Accept'], 'application/json')
        body = json.loads(req.body)
        inv = body['Invoices'][0]
        self.assertEqual({k: inv[k] for k in ('Type', 'Status', 'Date', 'DueDate', 'LineAmountTypes', 'InvoiceNumber',
                                              'Reference', 'CurrencyCode', 'Contact')},
                         {'Type': 'ACCREC', 'Status': 'DRAFT', 'Date': '2025-07-20', 'DueDate': '2025-08-19',
                          'LineAmountTypes': 'Exclusive', 'InvoiceNumber': 'INV-0050', 'Reference': 'PO-889',
                          'CurrencyCode': 'ZAR', 'Contact': {'ContactID': CONTACT}})
        self.assertEqual(inv['LineItems'][0], {
            'Description': 'Freight JHB-DBN 34t', 'Quantity': '1', 'UnitAmount': '8695.6522', 'AccountCode': '200',
            'TaxType': 'OUTPUT2', 'TaxAmount': '1304.35', 'Tracking': [{'Name': 'Region', 'Option': 'Gauteng'}]})
        self.assertEqual((inv['LineItems'][1]['Quantity'], inv['LineItems'][1]['DiscountRate']), ('3', '10'))
        self.assertNotIn('DiscountAmount', inv['LineItems'][1])

    def test_bill_request_has_no_reference_and_is_inclusive(self):
        self.api('PUT', '/Invoices', 'invoice_draft_created.json')
        bill = Document(kind='BILL', number='SUP-77', contact_id=CONTACT, issue_date=date(2025, 7, 20), due_date=None,
                        reference='expense 12', amounts_include_tax=True, lines=[
                            DocLine(description='Tolls', quantity=D('1'), unit_price=D('115.00'),
                                    net_amount=D('100.00'), tax_amount=D('15.00'), account_code='450',
                                    tax_code='INPUT2', discount_amount=D('0'))])
        self.adapter.push_bill(bill, idempotency_key='bill-77')
        inv = json.loads(self.replay.last('PUT').body)['Invoices'][0]
        self.assertEqual((inv['Type'], inv['InvoiceNumber'], inv['LineAmountTypes']), ('ACCPAY', 'SUP-77', 'Inclusive'))
        self.assertNotIn('Reference', inv)   # ACCPAY has no Reference in Xero
        self.assertNotIn('DiscountAmount', inv['LineItems'][0])

    def test_find_document_where_clauses(self):
        self.api('GET', '/Invoices', 'invoices_list.json')
        self.api('GET', '/CreditNotes', 'credit_notes_list.json')
        self.adapter.find_document('INVOICE', 'INV-0042')
        self.assertEqual(Replay.params(self.replay.last('GET')), {'InvoiceNumbers': 'INV-0042', 'where': 'Type=="ACCREC"'})
        self.adapter.find_document('CREDIT_NOTE', 'CN "7"')
        self.assertEqual(Replay.params(self.replay.last('GET'))['where'], 'CreditNoteNumber=="CN \\"7\\""')
        self.adapter.find_document('BILL', 'SUP-77', contact_id=CONTACT)
        self.assertEqual(Replay.params(self.replay.last('GET'))['where'],
                         f'Type=="ACCPAY"&&InvoiceNumber=="SUP-77"&&Contact.ContactID==Guid("{CONTACT}")')

    def test_finalise_and_void_bodies(self):
        self.api('POST', f'/Invoices/{INVOICE}', 'invoice_paid.json')
        self.adapter.finalise_document('INVOICE', INVOICE)
        req = self.replay.last('POST')
        self.assertEqual(json.loads(req.body), {'Invoices': [{'InvoiceID': INVOICE, 'Status': 'AUTHORISED'}]})
        self.assertEqual(Replay.params(req), {'unitdp': '4'})

    def test_allocation_payment_and_overpayment_requests(self):
        self.api('PUT', f'/CreditNotes/{CN}/Allocations', 'credit_note.json')
        self.adapter.allocate_credit_note(CN, INVOICE, D('1150'), date(2025, 7, 12))
        self.assertEqual(json.loads(self.replay.last('PUT').body), {'Allocations': [
            {'Invoice': {'InvoiceID': INVOICE}, 'Amount': '1150.00', 'Date': '2025-07-12'}]})

        self.api('PUT', '/Payments', 'payment_created.json')
        res = self.adapter.push_payment(invoice_external_id=INVOICE, amount=D('1150'), on=date(2025, 7, 3),
                                        account_code='090', reference='TW receipt RCPT-0001', idempotency_key='rcpt-1')
        self.assertEqual((res.external_id, res.status), ('9d2f4b6e-8a0c-4e1f-b3d5-7f9b1d3f5a7c', 'AUTHORISED'))
        req = self.replay.last('PUT')
        self.assertEqual(req.headers['Idempotency-Key'], 'rcpt-1')
        self.assertEqual(json.loads(req.body), {'Payments': [{
            'Invoice': {'InvoiceID': INVOICE}, 'Account': {'Code': '090'}, 'Date': '2025-07-03', 'Amount': '1150.00',
            'Reference': 'TW receipt RCPT-0001'}]})

        self.api('PUT', '/BankTransactions', 'bank_transaction_overpayment.json')
        res = self.adapter.push_overpayment(contact_id=CONTACT, amount=D('350'), on=date(2025, 7, 3),
                                            account_code='090', reference='TW receipt RCPT-0001 excess',
                                            idempotency_key='rcpt-1-excess')
        self.assertEqual(res.external_id, OVP)                      # the Overpayment, not the BankTransaction
        bt = json.loads(self.replay.last('PUT').body)['BankTransactions'][0]
        self.assertEqual((bt['Type'], bt['LineAmountTypes'], bt['BankAccount'], bt['LineItems'][0]['LineAmount']),
                         ('RECEIVE-OVERPAYMENT', 'NoTax', {'Code': '090'}, '350.00'))
        self.assertEqual(self.replay.last('PUT').headers['Idempotency-Key'], 'rcpt-1-excess')

    def test_contact_upsert_and_tracking_option(self):
        self.api('POST', '/Contacts', 'contacts.json')
        self.adapter.upsert_contact(Contact(name='Acme Mining (Pty) Ltd', email='ar@acme-mining.co.za',
                                            vat_number='4123456789', reference='TW-C-17'))
        req = self.replay.last('POST')
        self.assertTrue(req.headers['Idempotency-Key'].startswith('contact-TW-C-17-'))
        self.assertEqual(json.loads(req.body), {'Contacts': [{
            'Name': 'Acme Mining (Pty) Ltd', 'EmailAddress': 'ar@acme-mining.co.za', 'TaxNumber': '4123456789',
            'ContactNumber': 'TW-C-17'}]})
        self.api('GET', '/TrackingCategories', 'tracking_categories.json')
        self.api('PUT', '/TrackingCategories/f2a4c6e8-0b1d-4f3a-a5c7-e9b1d3f5a7c9/Options', 'tracking_option_created.json')
        self.assertEqual(self.adapter.ensure_tracking_option('f2a4c6e8-0b1d-4f3a-a5c7-e9b1d3f5a7c9', 'CA 123-456'),
                         'CA 123-456')
        self.assertEqual(json.loads(self.replay.last('PUT').body), {'Name': 'CA 123-456'})

    def test_list_filters_and_if_modified_since(self):
        self.api('GET', '/Payments', 'payments_list.json')
        self.api('GET', '/Overpayments', 'overpayments_list.json')
        self.api('GET', '/Prepayments', 'prepayments_list.json')
        since = datetime(2025, 7, 10, 10, 30, 15, tzinfo=dt_timezone(timedelta(hours=2)))
        self.adapter.list_payments_since(since)
        req = self.replay.last('GET', '/Payments')
        self.assertEqual(req.headers['If-Modified-Since'], '2025-07-10T08:30:15')     # UTC, no zone suffix
        self.assertEqual(Replay.params(req), {'where': 'PaymentType=="ACCRECPAYMENT"', 'page': '1'})
        self.adapter.list_receipts(date(2025, 7, 1), date(2025, 7, 31))
        self.assertEqual(Replay.params(self.replay.last('GET', '/Payments'))['where'],
                         'PaymentType=="ACCRECPAYMENT"&&Status=="AUTHORISED"&&'
                         'Date>=DateTime(2025,07,01)&&Date<=DateTime(2025,07,31)')
        self.assertEqual(Replay.params(self.replay.last('GET', '/Overpayments'))['where'],
                         'Type=="RECEIVE-OVERPAYMENT"&&Date>=DateTime(2025,07,01)&&Date<=DateTime(2025,07,31)')
        self.assertNotIn('If-Modified-Since', self.replay.last('GET', '/Overpayments').headers)

    def test_authorization_url_encodes_scope_spaces_as_percent20(self):
        from core.accounting.xero import AUTHORIZE_URL, DEFAULT_SCOPES, XeroAdapter
        url = XeroAdapter.authorization_url('st4te')
        self.assertTrue(url.startswith(AUTHORIZE_URL + '?'))
        query = urlsplit(url).query
        self.assertIn('scope=openid%20profile%20email%20offline_access%20', query)
        self.assertNotIn('+', query)
        q = parse_qs(query)
        self.assertEqual(q['scope'], [DEFAULT_SCOPES])
        self.assertEqual((q['response_type'], q['client_id'], q['state'], q['redirect_uri']),
                         (['code'], ['fake-client'], ['st4te'], [SETTINGS['XERO_REDIRECT_URI']]))

    def test_token_endpoint_uses_basic_auth_and_a_form_body(self):
        from core.accounting.xero import XeroAdapter
        self.replay.on('POST', r'^identity\.xero\.com/connect/token$', 'token.json')
        tokens = XeroAdapter.exchange_code('c0de')
        req = self.replay.last('POST')
        self.assertEqual(req.headers['Authorization'],
                         'Basic ' + base64.b64encode(b'fake-client:fake-secret').decode())
        self.assertEqual(req.headers['Content-Type'], 'application/x-www-form-urlencoded')
        self.assertEqual(dict(parse_qsl(req.body)), {'grant_type': 'authorization_code', 'code': 'c0de',
                                                     'redirect_uri': SETTINGS['XERO_REDIRECT_URI']})
        self.assertEqual((tokens.expires_in, tokens.refresh_token[:8]), (1800, '7b4f2c9e'))
        self.assertTrue(tokens.scope.startswith('openid'))
        self.adapter.refresh('old-refresh')
        self.assertEqual(dict(parse_qsl(self.replay.last('POST').body)),
                         {'grant_type': 'refresh_token', 'refresh_token': 'old-refresh'})

    def test_revoke_deletes_the_connection_then_revokes_the_refresh_token(self):
        self.replay.on('DELETE', r'^api\.xero\.com/connections/e1eede29-f875-4a5d-8470-17f6a29a88b1$', 'error_404.json')
        self.replay.on('POST', r'^identity\.xero\.com/connect/revocation$', 'token.json')
        self.adapter.revoke()        # 404 on the connection is fine (already gone)
        req = self.replay.last('POST')
        self.assertEqual(dict(parse_qsl(req.body)), {'token': 'stored-refresh-token'})
        self.assertTrue(req.headers['Authorization'].startswith('Basic '))

    def test_web_urls(self):
        url = self.adapter.web_url('INVOICE', INVOICE)
        self.assertTrue(url.startswith('https://go.xero.com/organisationlogin/default.aspx?shortcode=!gH7kQ&redirecturl='))
        self.assertIn(INVOICE, unquote(url))


# ====================================================================== (c) errors

class ErrorMappingTests(ContractBase):

    def test_validation_exception_is_permanent_with_every_message(self):
        self.api('POST', f'/Invoices/{INVOICE}', 'invoice_validation_exception.json')
        self.api('GET', f'/Invoices/{INVOICE}', 'invoice_paid.json')
        with self.assertRaises(PermanentError) as ctx:
            self.adapter.discard_document('INVOICE', INVOICE)
        msg = str(ctx.exception)
        self.assertIn('This document cannot be edited as it has a payment or credit note allocated to it.', msg)
        self.assertIn("Account code '999' is not a valid code for this document.", msg)
        self.assertEqual(ctx.exception.status, 400)
        self.assertNotIsInstance(ctx.exception, NotFound)

    def test_summarize_errors_false_element_errors_are_permanent(self):
        self.api('PUT', '/Invoices', 'invoice_has_errors.json')
        doc = Document(kind='INVOICE', number='INV-0042', contact_id=CONTACT, issue_date=date(2025, 7, 1),
                       due_date=None, lines=[])
        with self.assertRaisesRegex(PermanentError, 'Invoice # must be unique.'):
            self.adapter.push_invoice(doc, idempotency_key='k')

    def test_duplicate_contact_name(self):
        self.api('POST', '/Contacts', 'contact_duplicate_name.json')
        with self.assertRaisesRegex(PermanentError, 'is already assigned to another contact'):
            self.adapter.upsert_contact(Contact(name='Acme Mining (Pty) Ltd', reference='TW-C-18'))

    def test_401_refreshes_once_then_auth_error(self):
        self.api('GET', '/Accounts', 'error_401.json')
        self.replay.on('POST', r'^identity\.xero\.com/connect/token$', 'token.json')
        with self.assertRaises(AuthError) as ctx:
            self.adapter.get_accounts()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual([r.method for r in self.replay.sent], ['GET', 'POST', 'GET'])
        self.assertEqual(dict(parse_qsl(self.replay.sent[1].body))['refresh_token'], 'stored-refresh-token')

    def test_403_is_auth_error_without_refresh(self):
        self.api('GET', '/Accounts', 'error_403.json')
        with self.assertRaisesRegex(AuthError, 'AuthenticationUnsuccessful'):
            self.adapter.get_accounts()
        self.assertEqual(len(self.replay.sent), 1)

    def test_404(self):
        self.api('GET', f'/Invoices/{INVOICE}', 'error_404.json')
        with self.assertRaises(NotFound):
            self.adapter.get_invoice_state(INVOICE)
        self.api('GET', f'/Contacts/{CONTACT}', 'error_404.json')
        self.assertEqual(self.adapter.find_contacts(external_id=CONTACT), [])

    def test_429_rate_limited_and_limiter_blocked(self):
        self.api('GET', '/Accounts', 'error_429.json')
        with self.assertRaises(RateLimited) as ctx:
            self.adapter.get_accounts()
        self.assertEqual((ctx.exception.retry_after, ctx.exception.scope, ctx.exception.status), (45.0, 'minute', 429))
        limiter = self.adapter.http.limiter
        self.assertGreater(limiter.blocked_for(self.tenant), 40)
        n = len(self.replay.sent)
        with self.assertRaises(RateLimited) as ctx:
            self.adapter.get_accounts()                      # refused locally, no call made
        self.assertEqual(ctx.exception.scope, 'blocked')
        self.assertEqual(len(self.replay.sent), n)

    def test_5xx_and_timeouts_are_transient(self):
        self.api('GET', '/Accounts', 'error_500.json')
        with self.assertRaises(TransientError) as ctx:
            self.adapter.get_accounts()
        self.assertEqual(ctx.exception.status, 500)
        self.api('GET', '/Accounts', 'error_503.json')
        with self.assertRaises(TransientError) as ctx:
            self.adapter.get_accounts()
        self.assertEqual((ctx.exception.status, ctx.exception.retry_after), (503, 120.0))
        self.api('GET', '/Accounts', 'TIMEOUT')
        with self.assertRaises(TransientError) as ctx:
            self.adapter.get_accounts()
        self.assertEqual(ctx.exception.retry_after, 30)

    def test_invalid_grant_on_refresh_is_auth_error(self):
        self.replay.on('POST', r'^identity\.xero\.com/connect/token$', 'token_invalid_grant.json')
        with self.assertRaisesRegex(AuthError, 'invalid_grant'):
            self.adapter.refresh('dead-refresh-token')
