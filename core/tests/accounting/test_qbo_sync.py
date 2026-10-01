"""End-to-end: TruckWys <-> the fake QBO ledger (core/tests/accounting/fake_qbo.py).

The same scenarios as test_xero_sync.py, adapted to QuickBooks Online:
connect via realmId, currency guard, mapping to items, the custom
transaction numbers gate, pushes (create -> verify -> keep / delete), the
tax override, duplicate DocNumbers, credit memo application via zero
payments, bills, 429 / 5xx / timeout idempotency, token rotation, re-auth,
webhooks (classic + CloudEvents, bad signature), payments applied to two
invoices, deletions via CDC, unapplied payments applied later, credit memos
raised in QBO, reconciliation and disconnect.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.accounting import connection as conn_svc, pull, reconciliation, sync
from core.accounting.http import use_transport
from core.models import (
    AccountingConnection, AccountingWebhookEvent, CreditNote, Customer, Expense, ExternalLink, Invoice, Payment,
    Supplier,
)
from core.tests.accounting.fake_qbo import FakeQBO
from core.tests.accounting.qbo_helpers import QBO_REDIRECT, connect, map_everything, no_commit_delay, qbo_settings

D = Decimal
CUTOVER = date(2026, 9, 1)
WEBHOOK_URL = '/api/v1/integrations/quickbooks/webhooks/'


def make_user(username, company, role='ADMIN'):
    u = get_user_model().objects.create_user(username=username, email=f'{username}@qs.test', password='x')
    u.role, u.company = role, company
    u.save()
    return u


class QBOFlowBase(TestCase):
    """A connected, mapped company with a cut-over, on a fresh fake QBO."""
    connect_on_setup = True
    map_on_setup = True
    cutover = CUTOVER

    def setUp(self):
        from core.models import Company
        self._settings = qbo_settings()
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.qbo = self.make_fake()
        self._transport = use_transport(self.qbo)
        self._transport.__enter__()
        self.addCleanup(self._transport.__exit__, None, None, None)
        self.co = Company.objects.create(company_name='Flow Haulage (Pty) Ltd', vat_number='4987654321')
        self.admin = make_user(f'qflow_admin_{self.co.pk}', self.co)
        self.cust = Customer.objects.create(company=self.co, name='Acme Mining (Pty) Ltd', email='ar@acme.test',
                                            vat_number='4123456789', credit_score=80)
        self.conn = None
        if self.connect_on_setup:
            self.conn, outcome = connect(self.co, self.admin, self.qbo)
            self.assertEqual(outcome, 'connected')
            if self.map_on_setup:
                map_everything(self.conn)
                s = dict(self.conn.settings)
                s['cutover_date'] = self.cutover.isoformat()
                self.conn.settings = s
                self.conn.save()

    def make_fake(self):
        return FakeQBO(verifier_token='test-verifier-token')

    # ---- helpers
    def api(self, user=None):
        c = APIClient(HTTP_HOST='localhost')
        c.force_authenticate(user or self.admin)
        return c

    def issue(self, lines=None, customer=None, issue=date(2026, 9, 5), terms='NET30'):
        from core.services.invoice_lines import apply_lines, due_date_for, terms_days_for
        from core.services.numbering import provisional_number
        inv = Invoice(company=self.co, customer=customer or self.cust, invoice_number=provisional_number(),
                      issue_date=issue, due_date=due_date_for(issue, terms), payment_terms=terms,
                      terms_days=terms_days_for(terms), status='DRAFT', subtotal=0, total_amount=0, balance=0)
        apply_lines(inv, lines or [
            {'description': 'Freight JHB-DBN', 'quantity': '1', 'unit_price': '18500.00', 'tax_code': 'STANDARD'},
            {'description': 'Pallet handling', 'quantity': '3', 'unit_price': '33.335', 'discount_percent': '10',
             'tax_code': 'STANDARD', 'revenue_type': 'OTHER'},
            {'description': 'Fuel surcharge', 'quantity': '1', 'unit_price': '1275.50', 'tax_code': 'STANDARD',
             'revenue_type': 'FUEL_SURCHARGE'},
            {'description': 'Cross-border leg', 'quantity': '1', 'unit_price': '4200.00', 'tax_code': 'ZERO_RATED'},
        ])
        with no_commit_delay(self):
            inv.mark_as_sent()
        inv.refresh_from_db()
        return inv

    def link(self, object_type, local_id):
        return ExternalLink.objects.filter(connection=self.conn, object_type=object_type, local_id=local_id).first()

    def qinv(self, inv):
        return self.qbo.find_invoice(inv.invoice_number)

    def webhook(self, entities, fmt='classic', key='test-verifier-token', expect=200):
        body, sig = self.qbo.webhook_payload(entities, fmt=fmt, key=key)
        with no_commit_delay(self):
            resp = APIClient(HTTP_HOST='localhost').generic('POST', WEBHOOK_URL, body,
                                                            content_type='application/json',
                                                            HTTP_INTUIT_SIGNATURE=sig)
        self.assertEqual(resp.status_code, expect)
        return resp

    def poll(self):
        self.conn.refresh_from_db()
        return pull.poll_payments(self.conn)

    def later(self, minutes=1):
        """Move the fake clock past the last poll cursor (CDC is inclusive)."""
        self.qbo.now = datetime.now(dt_timezone.utc) + timedelta(minutes=minutes)


# ====================================================================== connect

class ConnectTests(QBOFlowBase):
    connect_on_setup = False

    def test_company_connects_by_realm_and_reads_its_settings(self):
        conn, outcome = connect(self.co, self.admin, self.qbo)
        self.assertEqual(outcome, 'connected')
        self.assertEqual((conn.status, conn.tenant_id, conn.base_currency, conn.country),
                         ('ACTIVE', self.qbo.realm, 'ZAR', 'ZA'))
        self.assertEqual(conn.tenant_name, 'Golden Haulage (Pty) Ltd')
        self.assertTrue(conn.access_token.startswith('enc:'))
        self.assertTrue(conn.refresh_token.startswith('enc:'))
        self.assertIsNotNone(conn.refresh_token_expires_at)
        opts = conn.settings['options']
        items = {a['code']: a for a in opts['accounts'] if a['type'] == 'ITEM'}
        self.assertEqual(set(items), {'item:1', 'item:2', 'item:3', 'item:4', 'item:5'})   # not the inventory item
        self.assertTrue(any(a['code'] == '1' and a['is_bank'] for a in opts['accounts']))
        self.assertFalse(any(a['code'] == '27' for a in opts['accounts']))                  # inactive account
        rates = {t['code']: t for t in opts['tax_rates']}
        self.assertEqual((rates['3']['rate'], rates['3']['revenue'], rates['3']['expenses']), ('15.00', True, True))
        self.assertEqual((rates['7']['revenue'], rates['7']['expenses']), (False, True))   # purchases only
        self.assertEqual([c['id'] for c in opts['tracking_categories']], ['class', 'location'])
        self.assertEqual(opts['blockers'], [])
        # Every API call carried the minor version.
        api_calls = [c for c in self.qbo.calls if c.path.startswith('/')
                     and not c.path.startswith('/oauth2') and not c.path.startswith('/v2/')]
        self.assertTrue(api_calls and all(c.params.get('minorversion') == '75' for c in api_calls))
        body = self.api().get('/api/v1/integrations/accounting/connection/').json()
        self.assertEqual((body['status'], body['provider']), ('ACTIVE', 'QBO'))
        self.assertFalse(body['readiness']['sync_enabled'])
        self.assertIn('sandbox.qbo.intuit.com', body['web_url'])

    def test_providers_lists_quickbooks_as_available(self):
        rows = {p['provider']: p for p in self.api().get('/api/v1/integrations/accounting/providers/').json()['providers']}
        self.assertEqual((rows['QBO']['availability'], rows['QBO']['configured']), ('available', True))
        self.assertEqual(rows['QBO']['slug'], 'quickbooks')

    def test_a_non_zar_company_is_refused_and_revoked(self):
        self.qbo = FakeQBO(companies=[{'realm': '4620816365000000', 'name': 'Flow USA', 'country': 'US',
                                       'currency': 'USD', 'multicurrency': True}])
        with use_transport(self.qbo):
            with self.assertRaises(conn_svc.ConnectError) as ctx:
                connect(self.co, self.admin, self.qbo)
        self.assertEqual(ctx.exception.code, 'currency_not_supported')
        self.assertFalse(AccountingConnection.objects.filter(company=self.co, status='ACTIVE').exists())
        self.assertTrue(self.qbo.calls_to('POST', r'/tokens/revoke$'))

    def test_full_callback_through_the_view(self):
        from urllib.parse import parse_qs, urlparse
        start = self.api().post('/api/v1/integrations/accounting/quickbooks/connect/').json()
        self.assertIn('/api/v1/integrations/accounting/quickbooks/start/?ticket=', start['auth_url'])
        browser = APIClient(HTTP_HOST='localhost')
        u = urlparse(start['auth_url'])
        hop = browser.get(f'{u.path}?{u.query}')
        self.assertEqual(hop.status_code, 302)
        self.assertTrue(hop['Location'].startswith('https://appcenter.intuit.com/connect/oauth2?'))
        self.assertIn('scope=com.intuit.quickbooks.accounting%20openid%20profile%20email', hop['Location'])
        state = parse_qs(urlparse(hop['Location']).query)['state'][0]
        code = self.qbo.authorize(redirect_uri=QBO_REDIRECT)
        resp = browser.get('/api/v1/integrations/quickbooks/callback/',
                           {'code': code, 'state': state, 'realmId': self.qbo.realm})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('provider=quickbooks&result=connected', resp['Location'])
        conn = AccountingConnection.objects.get(company=self.co, status='ACTIVE')
        self.assertEqual((conn.provider, conn.tenant_id), ('QBO', self.qbo.realm))
        resp = APIClient(HTTP_HOST='localhost').get('/api/v1/integrations/quickbooks/callback/',
                                                    {'error': 'access_denied', 'state': state})
        self.assertIn('reason=denied', resp['Location'])

    def test_custom_transaction_numbers_off_blocks_sync_until_turned_on(self):
        self.qbo.set_preference(custom_txn_numbers=False)
        self.conn, _ = connect(self.co, self.admin, self.qbo)
        blockers = self.conn.settings['options']['blockers']
        self.assertEqual(len(blockers), 1)
        self.assertIn('Custom transaction numbers', blockers[0])
        map_everything(self.conn)
        resp = self.api().post('/api/v1/integrations/accounting/connection/backfill/', {'cutover_date': '2026-09-01'},
                               format='json')
        self.assertEqual((resp.status_code, resp.json()['code']), (400, 'provider_settings'))
        s = dict(self.conn.settings)
        s['cutover_date'] = CUTOVER.isoformat()
        self.conn.settings = s
        self.conn.save()
        body = self.api().get('/api/v1/integrations/accounting/connection/').json()
        self.assertIn(blockers[0], body['readiness']['blocking_reasons'])
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'BLOCKED')
        self.assertIn('Custom transaction numbers', link.last_error)
        self.assertEqual(self.qbo.invoices(), [])
        # Turned on in QBO, then "Refresh": the waiting invoice goes, with our number.
        self.qbo.set_preference(custom_txn_numbers=True)
        with no_commit_delay(self):
            resp = self.api().post('/api/v1/integrations/accounting/connection/refresh-options/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(self.qinv(inv)['DocNumber'], inv.invoice_number)


class MappingTests(QBOFlowBase):
    map_on_setup = False

    def test_revenue_types_map_to_items_and_items_only_to_revenue(self):
        url = '/api/v1/integrations/accounting/connection/mapping/'
        state = self.api().get(url).json()
        self.assertEqual(state['suggestions']['tax_sales'].get('STANDARD'), '3')
        self.assertIsNone(state['suggestions']['tax_purchases'].get('STANDARD'))   # 15 % S and 15 % CG
        resp = self.api().put(url, {'revenue_types': {'FREIGHT': '10'}, 'expense_categories': {'FUEL': 'item:1'},
                                    'receipts_account': 'item:1', 'tax_sales': {'ZERO_RATED': '3'},
                                    'tax_purchases': {'STANDARD': '7'}}, format='json')
        self.assertEqual(resp.status_code, 400)
        errors = resp.json()['errors']
        self.assertIn('product/service', errors['revenue_types.FREIGHT'])
        self.assertIn('product/service', errors['expense_categories.FUEL'])
        self.assertIn('receipts_account', errors)
        self.assertIn('needs a 0.00% rate', errors['tax_sales.ZERO_RATED'])
        self.assertNotIn('tax_purchases.STANDARD', errors)   # capital goods 15 %: allowed on purchases
        resp = self.api().put(url, {'tax_sales': {'STANDARD': '7'}}, format='json')
        self.assertIn('can\'t be used on sales', resp.json()['errors']['tax_sales.STANDARD'])
        state = map_everything(self.conn)
        self.assertTrue(state['complete'], state['missing'])


# ====================================================================== pushing

class PushTests(QBOFlowBase):
    def test_issued_invoice_is_pushed_and_verified(self):
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        q = self.qinv(inv)
        self.assertEqual(q['DocNumber'], inv.invoice_number)
        self.assertEqual((D(q['TotalAmt']), D(q['TxnTaxDetail']['TotalTax'])), (inv.total_amount, inv.vat_amount))
        self.assertEqual(link.external_id, q['Id'])
        self.assertEqual(link.external_version, q['SyncToken'])
        lines = [l for l in q['Line'] if l['DetailType'] == 'SalesItemLineDetail']
        self.assertEqual([l['SalesItemLineDetail']['ItemRef']['value'] for l in lines], ['1', '4', '2', '1'])
        self.assertEqual([l['SalesItemLineDetail']['TaxCodeRef']['value'] for l in lines], ['3', '3', '3', '4'])
        # 3 x 33.335 less 10% = 90.00 -> sent as 3 x 30.00 (exact), Amount = our net.
        pallet = lines[1]
        self.assertEqual((D(pallet['Amount']), D(pallet['SalesItemLineDetail']['Qty']),
                          D(pallet['SalesItemLineDetail']['UnitPrice'])), (D('90.00'), D('3'), D('30')))
        # One POST /invoice (no draft step) carrying requestid and the explicit tax lines.
        posts = self.qbo.calls_to('POST', r'^/invoice$')
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].params.get('requestid'))
        sent = posts[0].json
        self.assertEqual(sent['GlobalTaxCalculation'], 'TaxExcluded')
        self.assertEqual(sum(D(t['Amount']) for t in sent['TxnTaxDetail']['TaxLine']), inv.vat_amount)
        # The customer was created with our VAT number (read back masked).
        cust = self.qbo.customer(q['CustomerRef']['value'])
        self.assertEqual(cust['PrimaryTaxIdentifier'], 'XXXXXX6789')
        self.assertEqual(self.qbo.company().rows['Customer'][cust['Id']]['PrimaryTaxIdentifier'], '4123456789')
        body = self.api().get(f'/api/v1/invoices/{inv.pk}/').json()
        self.assertEqual(body['accounting_sync']['status'], 'SYNCED')
        self.assertEqual(body['accounting_sync']['url'],
                         f'https://app.sandbox.qbo.intuit.com/app/invoice?txnId={q["Id"]}')

    def test_inexact_discount_is_sent_as_one_times_net(self):
        inv = self.issue(lines=[{'description': 'Loading', 'quantity': '3', 'unit_price': '10.00',
                                 'discount_amount': '5.00', 'tax_code': 'STANDARD'}])
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        line = next(l for l in self.qinv(inv)['Line'] if l['DetailType'] == 'SalesItemLineDetail')
        self.assertEqual((D(line['Amount']), D(line['SalesItemLineDetail']['Qty']),
                          D(line['SalesItemLineDetail']['UnitPrice'])), (D('25.00'), D('1'), D('25.00')))
        self.assertIn('3 x 10.0000 less 5.00', line['Description'])

    def test_tax_override_makes_per_line_vat_stick_and_a_mismatch_is_deleted(self):
        three = [{'description': f'Admin fee {i}', 'quantity': '1', 'unit_price': '10.10', 'tax_code': 'STANDARD'}
                 for i in range(3)]
        inv = self.issue(lines=three)
        self.assertEqual(inv.vat_amount, D('4.56'))                  # 3 x round(1.515)
        self.assertEqual(D(self.qinv(inv)['TxnTaxDetail']['TotalTax']), D('4.56'))
        # Without the override QBO's per-rate rule gives 4.55: never kept.
        self.qbo.honour_tax_override = False
        inv2 = self.issue(lines=three)
        link = self.link('INVOICE', inv2.pk)
        self.assertEqual(link.status, 'DEAD')
        self.assertIn('calculated different totals', link.last_error)
        self.assertIn('VAT TruckWys 4.56 vs 4.55', link.last_error)
        self.assertIsNone(self.qinv(inv2))                           # deleted at once
        self.assertTrue(self.qbo.calls_to('POST', r'^/invoice$')[-1].params.get('operation') == 'delete')

    def test_invoice_already_typed_into_qbo_is_linked_not_duplicated(self):
        inv = self.issue(issue=date(2026, 8, 30))   # before the cut-over: not pushed automatically
        cid = self.qbo.create_customer('Acme Mining (Pty) Ltd', email='ar@acme.test')
        self.qbo.add_invoice(cid, [FakeQBO.sales_line(inv.subtotal)], inv.issue_date, inv.invoice_number,
                             tax_lines=[{'Amount': inv.vat_amount, 'TaxLineDetail': {'TaxRateRef': {'value': '1'}}}])
        s = dict(self.conn.settings)
        s['cutover_date'] = '2026-08-01'
        self.conn.settings = s
        self.conn.save()
        link = sync.get_or_create_link(self.conn, 'INVOICE', inv.pk)
        sync.run_link(link.pk)
        link.refresh_from_db()
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        self.assertTrue(link.meta.get('matched_existing'))
        self.assertEqual(len([i for i in self.qbo.invoices() if i['DocNumber'] == inv.invoice_number]), 1)
        # The customer matched by e-mail (VAT can't be compared: QBO masks it).
        self.assertEqual(self.link('CONTACT_CUSTOMER', self.cust.pk).match_method, 'email')

    def test_same_number_with_different_totals_is_dead_with_both_figures(self):
        inv = self.issue(issue=date(2026, 8, 30))
        cid = self.qbo.create_customer('Acme Mining (Pty) Ltd', email='ar@acme.test')
        self.qbo.add_invoice(cid, [FakeQBO.sales_line(D('10.00'))], inv.issue_date, inv.invoice_number)
        s = dict(self.conn.settings)
        s['cutover_date'] = '2026-08-01'
        self.conn.settings = s
        self.conn.save()
        link = sync.get_or_create_link(self.conn, 'INVOICE', inv.pk)
        sync.run_link(link.pk)
        link.refresh_from_db()
        self.assertEqual(link.status, 'DEAD')
        self.assertIn('different totals', link.last_error)
        self.assertIn(str(inv.total_amount), link.last_error)

    def test_name_only_match_waits_for_a_person(self):
        existing = self.qbo.create_customer('ACME MINING')
        other = Customer.objects.create(company=self.co, name='Acme Mining (Pty) Ltd.', email='other@acme.test',
                                        credit_score=80)
        inv = self.issue(customer=other)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'BLOCKED')
        clink = self.link('CONTACT_CUSTOMER', other.pk)
        self.assertEqual((clink.status, clink.match_method), ('SUGGESTED', 'name'))
        with no_commit_delay(self):
            resp = self.api().post(f'/api/v1/integrations/accounting/connection/contacts/{clink.pk}/confirm/',
                                   {'external_id': existing}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(self.qinv(inv)['CustomerRef']['value'], existing)

    def test_display_name_used_by_a_vendor_becomes_a_suggestion(self):
        self.qbo.create_vendor('Acme Mining (Pty) Ltd')   # DisplayName is unique across customers and vendors
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'BLOCKED')
        self.assertIn('already has a contact named like', link.last_error)
        clink = self.link('CONTACT_CUSTOMER', self.cust.pk)
        self.assertEqual(clink.status, 'SUGGESTED')
        self.assertIn('Duplicate Name Exists Error', clink.last_error)
        # The search in the wizard is per kind (customers here).
        found = self.api().get('/api/v1/integrations/accounting/connection/contacts/search/',
                               {'q': 'acme', 'kind': 'SUPPLIER'}).json()['results']
        self.assertEqual([r['name'] for r in found], ['Acme Mining (Pty) Ltd'])

    def test_credit_note_is_applied_with_a_zero_payment_then_voided(self):
        from core.services.credit_notes import create_credit_note, void_credit_note
        inv = self.issue()
        line = inv.lines.get(position=2)
        with no_commit_delay(self):
            cn = create_credit_note(inv, user=self.admin, reason='Surcharge waived', issue_date=date(2026, 9, 10),
                                    lines=[{'invoice_line': line.pk, 'description': 'Surcharge waived',
                                            'quantity': '1', 'unit_price': '1275.50', 'tax_code': 'STANDARD'}])
        link = self.link('CREDIT_NOTE', cn.pk)
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        cm = self.qbo.credit_memo(link.external_id)
        self.assertEqual((cm['DocNumber'], D(cm['TotalAmt']), D(cm['RemainingCredit'])),
                         (cn.credit_note_number, cn.total_amount, D('0.00')))
        self.assertEqual(cm['Line'][0]['SalesItemLineDetail']['ItemRef']['value'], '2')
        apps = [p for p in self.qbo.payments() if D(p['TotalAmt']) == 0]
        self.assertEqual(len(apps), 1)
        self.assertEqual({(l['LinkedTxn'][0]['TxnType'], D(l['Amount'])) for l in apps[0]['Line']},
                         {('Invoice', cn.total_amount), ('CreditMemo', cn.total_amount)})
        self.assertEqual(D(self.qinv(inv)['Balance']), inv.total_amount - cn.total_amount)
        # The credit application is not money: the poll records nothing.
        self.poll()
        self.assertFalse(Payment.objects.filter(invoice=inv).exists())
        with no_commit_delay(self):
            void_credit_note(cn, user=self.admin, reason='issued in error')
        self.assertTrue(self.qbo.is_deleted('CreditMemo', link.external_id))
        self.assertTrue(self.qbo.is_deleted('Payment', apps[0]['Id']))
        self.assertEqual(D(self.qinv(inv)['Balance']), inv.total_amount)
        self.assertEqual(self.link('CREDIT_NOTE', cn.pk).status, 'VOIDED')

    def test_voided_invoice_is_voided_in_qbo(self):
        from core.services.credit_notes import void_invoice
        inv = self.issue()
        with no_commit_delay(self):
            void_invoice(inv, user=self.admin, reason='raised twice')
        q = self.qinv(inv)
        self.assertEqual((D(q['TotalAmt']), q['PrivateNote']), (D('0'), 'Voided'))
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'VOIDED')

    def test_supplier_expense_becomes_a_bill_follows_edits_and_is_deleted(self):
        from types import SimpleNamespace
        from core.models import Vehicle
        from core.serializers import ExpenseSerializer
        map_everything(self.conn, tracking={'vehicle_category_id': 'class', 'branch_category_id': 'location',
                                            'branch_option': 'Johannesburg'})
        truck = Vehicle.objects.create(company=self.co, vin='VINQBO1', plate='ND 123-456', make='MAN', model='TGS',
                                       year=2021, type='Truck', capacity=D('30000'), fuel_type='Diesel',
                                       status='AVAILABLE')
        sup = Supplier.objects.create(company=self.co, name='N4 Toll Concession', vat_number='4111111111')
        ser = ExpenseSerializer(data={'category': 'TOLLS', 'description': 'N4 tolls', 'amount': '1150.00',
                                      'expense_date': '2026-09-03', 'tax_code': 'STANDARD', 'supplier': sup.pk,
                                      'vehicle': truck.pk, 'expense_number': f'EXP-{self.co.pk}-1',
                                      'receipt_number': 'TOLL-778'},
                                context={'request': SimpleNamespace(user=self.admin), 'company': self.co})
        ser.is_valid(raise_exception=True)
        with no_commit_delay(self):
            exp = ser.save(company=self.co, created_by=self.admin)
        link = self.link('BILL', exp.pk)
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        bill = self.qbo.bill(link.external_id)
        self.assertEqual((bill['DocNumber'], bill['GlobalTaxCalculation']), ('TOLL-778', 'TaxInclusive'))
        self.assertEqual((D(bill['TotalAmt']), D(bill['TxnTaxDetail']['TotalTax'])), (D('1150.00'), D('150.00')))
        line = bill['Line'][0]
        self.assertEqual(line['AccountBasedExpenseLineDetail']['AccountRef']['value'], '21')
        self.assertEqual(D(line['Amount']), D('1000.00'))
        # Vehicle -> a Class created on demand; branch -> Location (Department).
        cls = {c['Name']: c['Id'] for c in self.qbo.classes()}
        self.assertEqual(line['AccountBasedExpenseLineDetail']['ClassRef']['value'], cls['ND 123-456'])
        self.assertEqual(bill['DepartmentRef']['value'], '1')
        self.assertEqual(bill['VendorRef']['value'], self.link('CONTACT_SUPPLIER', sup.pk).external_id)
        old_id = link.external_id
        with no_commit_delay(self):
            exp.amount = D('1265.00')
            exp.vat_amount = D('165.00')
            exp.save()
        # A posted bill is never changed in place: a verified replacement is
        # posted, then the old one is deleted (QBO bills can't be voided).
        link.refresh_from_db()
        self.assertNotEqual(link.external_id, old_id)
        self.assertTrue(self.qbo.is_deleted('Bill', old_id))
        bill = self.qbo.bill(link.external_id)
        self.assertEqual((D(bill['TotalAmt']), D(bill['TxnTaxDetail']['TotalTax'])), (D('1265.00'), D('165.00')))
        with no_commit_delay(self):
            exp.status = 'PENDING'
            exp.save()
            exp.reject(self.admin)
        self.assertTrue(self.qbo.is_deleted('Bill', link.external_id))
        self.assertEqual(self.link('BILL', exp.pk).status, 'VOIDED')


# ====================================================================== failures, retries, tokens

class RetryTests(QBOFlowBase):
    def unblock(self):
        from core.accounting.ratelimit import limiter_for
        lim = limiter_for('QBO')
        lim.r.delete(f'{lim.ns}:{self.conn.tenant_id}:blocked')

    def test_429_waits_for_retry_after_then_succeeds(self):
        self.qbo.fail_next('POST', r'^/invoice$', status=429, headers={'Retry-After': '42'})
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual((link.status, link.attempts), ('ERROR', 0))
        wait = (link.next_attempt_at - timezone.now()).total_seconds()
        self.assertTrue(35 <= wait <= 43, wait)
        from core.accounting.ratelimit import limiter_for
        self.assertGreater(limiter_for('QBO').blocked_for(self.conn.tenant_id), 30)
        self.unblock()
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(sync.retry_due(), 1)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(len([i for i in self.qbo.invoices() if i['DocNumber'] == inv.invoice_number]), 1)

    def test_throttle_without_retry_after_waits_a_minute(self):
        self.qbo.fail_next('POST', r'^/invoice$', status=429)   # QBO's ThrottleExceeded body, no header
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        wait = (link.next_attempt_at - timezone.now()).total_seconds()
        self.assertEqual(link.status, 'ERROR')
        self.assertTrue(55 <= wait <= 61, wait)
        self.unblock()

    def test_outage_backs_off_and_a_lost_response_does_not_duplicate(self):
        self.qbo.fail_next('POST', r'^/invoice$', status=503)
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual((link.status, link.attempts), ('ERROR', 1))
        # QBO created it but the answer never arrived.
        self.qbo.timeout_next('POST', r'^/invoice$', after_processing=True)
        for _ in range(2):
            ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            sync.retry_due()
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(len([i for i in self.qbo.invoices() if i['DocNumber'] == inv.invoice_number]), 1)

    def test_same_requestid_replays_instead_of_creating_twice(self):
        from core.accounting.registry import get_adapter
        from core.accounting import documents
        inv = self.issue(issue=date(2026, 8, 20))     # not pushed (pre cut-over)
        adapter = get_adapter(self.conn)
        from core.accounting.contacts import ensure_contact
        cid = ensure_contact(self.conn, 'CONTACT_CUSTOMER', self.cust, adapter)
        doc = documents.build_invoice(self.conn, inv, cid, documents.Tracker(self.conn, adapter))
        a = adapter.push_invoice(doc, idempotency_key='same-key')
        b = adapter.push_invoice(doc, idempotency_key='same-key')
        self.assertEqual(a.external_id, b.external_id)
        self.assertEqual(len(self.qbo.invoices()), 1)

    def test_expired_access_token_is_refreshed_and_the_new_refresh_token_stored(self):
        from core.utils.crypto import decrypt_secret
        old = decrypt_secret(self.conn.refresh_token)
        self.qbo.expire_access_tokens()
        AccountingConnection.objects.filter(pk=self.conn.pk).update(
            access_token_expires_at=timezone.now() + timedelta(minutes=20))   # we think it's still valid
        inv = self.issue()
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.conn.refresh_from_db()
        new = decrypt_secret(self.conn.refresh_token)
        self.assertNotEqual(new, old)
        self.assertEqual(new, self.qbo.current_refresh_token())

    def test_revoked_refresh_token_needs_reauth_and_resumes_after_reconnect(self):
        self.qbo.revoke_all_tokens()
        AccountingConnection.objects.filter(pk=self.conn.pk).update(
            access_token_expires_at=timezone.now() - timedelta(minutes=1))
        inv = self.issue()
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, 'NEEDS_REAUTH')
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'PENDING')
        resp = self.api().post('/api/v1/payments/', {'invoice': inv.pk, 'amount': '1.00', 'payment_date': '2026-09-06',
                                                     'payment_method': 'EFT'}, format='json')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['provider_name'], 'QuickBooks Online')
        with no_commit_delay(self):
            conn, outcome = connect(self.co, self.admin, self.qbo)
        self.assertEqual((conn.pk, outcome, conn.status), (self.conn.pk, 'connected', 'ACTIVE'))
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')

    def test_reconnecting_a_different_company_is_refused(self):
        self.qbo.companies['1111222233334444'] = type(self.qbo.company())(
            self.qbo, {'realm': '1111222233334444', 'name': 'Other Co', 'currency': 'ZAR'})
        AccountingConnection.objects.filter(pk=self.conn.pk).update(status='NEEDS_REAUTH')
        with self.assertRaises(conn_svc.ConnectError) as ctx:
            connect(self.co, self.admin, self.qbo, realm='1111222233334444')
        self.assertEqual(ctx.exception.code, 'org_mismatch')


# ====================================================================== payments back

class PaymentsBackTests(QBOFlowBase):
    def setUp(self):
        super().setUp()
        self.inv = self.issue()
        self.qid = self.link('INVOICE', self.inv.pk).external_id
        self.cid = self.qinv(self.inv)['CustomerRef']['value']

    def test_webhook_payment_arrives_and_deletion_via_cdc_reverses_it(self):
        pid = self.qbo.record_payment({self.qid: D('5000.00')}, date(2026, 9, 12), reference='EFT 991')
        self.webhook([{'name': 'Payment', 'id': pid, 'operation': 'Create'}])
        p = Payment.objects.get(invoice=self.inv, source='QBO')
        self.assertEqual((p.external_id, p.amount, p.payment_date, p.reference_number),
                         (f'{pid}:{self.qid}', D('5000.00'), date(2026, 9, 12), 'EFT 991'))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, 'PARTIALLY_PAID')
        self.webhook([{'name': 'Payment', 'id': pid, 'operation': 'Create'}])   # same delivery: deduplicated
        self.assertEqual(Payment.objects.filter(invoice=self.inv).count(), 1)
        self.assertEqual(AccountingWebhookEvent.objects.filter(provider='QBO').count(), 1)
        # First poll (cursor = cut-over, older than CDC's 30 days): query path.
        self.poll()
        self.assertTrue(any('MetaData.LastUpdatedTime' in q for q in self.qbo.queries('Payment')))
        # Deleted in QBO: the next poll (CDC) sees the tombstone.
        self.later()
        self.qbo.delete_payment(pid)
        self.poll()
        self.assertTrue(self.qbo.calls_to('GET', r'^/cdc$'))
        self.assertFalse(Payment.objects.filter(invoice=self.inv).exists())
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.paid_amount, self.inv.balance), (D('0.00'), self.inv.total_amount))

    def test_cloudevents_webhook_and_delete_event(self):
        pid = self.qbo.record_payment({self.qid: D('100.00')}, date(2026, 9, 12))
        self.webhook([{'name': 'Payment', 'id': pid, 'operation': 'Create'}], fmt='cloudevents')
        self.assertEqual(Payment.objects.get(invoice=self.inv).amount, D('100.00'))
        ev = AccountingWebhookEvent.objects.get(provider='QBO')
        self.assertEqual((ev.tenant_id, ev.resource_type, ev.event_type), (self.qbo.realm, 'Payment', 'Create'))
        self.qbo.delete_payment(pid)
        self.webhook([{'name': 'Payment', 'id': pid, 'operation': 'Delete'}], fmt='cloudevents')
        self.assertFalse(Payment.objects.filter(invoice=self.inv).exists())

    def test_bad_signature_is_401_and_nothing_is_stored(self):
        self.webhook([{'name': 'Payment', 'id': '1', 'operation': 'Create'}], key='wrong', expect=401)
        resp = APIClient(HTTP_HOST='localhost').generic('POST', WEBHOOK_URL, b'{}', content_type='application/json')
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(AccountingWebhookEvent.objects.exists())

    def test_one_payment_for_two_invoices_then_deleted(self):
        inv2 = self.issue(lines=[{'description': 'Freight', 'quantity': '1', 'unit_price': '2000', 'tax_code': 'STANDARD'}],
                          issue=date(2026, 9, 6))
        q2 = self.link('INVOICE', inv2.pk).external_id
        pid = self.qbo.record_payment({self.qid: D('1000.00'), q2: inv2.total_amount}, date(2026, 9, 15))
        self.poll()
        rows = {p.invoice_id: p for p in Payment.objects.filter(source='QBO', company=self.co)}
        self.assertEqual((rows[self.inv.pk].external_id, rows[self.inv.pk].amount), (f'{pid}:{self.qid}', D('1000.00')))
        self.assertEqual((rows[inv2.pk].external_id, rows[inv2.pk].amount), (f'{pid}:{q2}', inv2.total_amount))
        inv2.refresh_from_db()
        self.assertEqual(inv2.status, 'PAID')
        self.later()
        self.qbo.delete_payment(pid)
        self.poll()
        self.assertFalse(Payment.objects.filter(source='QBO', company=self.co).exists())
        inv2.refresh_from_db()
        self.assertEqual(inv2.balance, inv2.total_amount)

    def test_unapplied_payment_later_applied(self):
        pid = self.qbo.create_unapplied_payment(self.cid, D('3000.00'), date(2026, 9, 14))
        self.poll()
        self.assertFalse(Payment.objects.filter(invoice=self.inv).exists())
        from core.accounting.registry import get_adapter
        credits = get_adapter(self.conn).list_unallocated_credits()
        self.assertEqual([(c.kind, c.external_id, c.remaining) for c in credits], [('OVERPAYMENT', pid, D('3000.00'))])
        self.later()
        self.qbo.apply_payment(pid, self.qid, D('2500.00'))
        self.poll()
        p = Payment.objects.get(invoice=self.inv)
        self.assertEqual((p.external_id, p.amount, p.payment_date), (f'{pid}:{self.qid}', D('2500.00'),
                                                                     date(2026, 9, 14)))

    def test_payment_moved_to_another_invoice_in_qbo(self):
        inv2 = self.issue(lines=[{'description': 'Freight', 'quantity': '1', 'unit_price': '2000', 'tax_code': 'STANDARD'}],
                          issue=date(2026, 9, 6))
        q2 = self.link('INVOICE', inv2.pk).external_id
        pid = self.qbo.create_unapplied_payment(self.cid, D('500.00'), date(2026, 9, 14))
        self.qbo.apply_payment(pid, self.qid, D('500.00'))
        self.webhook([{'name': 'Payment', 'id': pid, 'operation': 'Update'}])
        self.assertEqual(Payment.objects.get(source='QBO').invoice_id, self.inv.pk)
        # Someone re-applies it to the other invoice.
        c = self.qbo.company()
        p = c.get('Payment', pid)
        c.save_payment({**p, 'Line': [{'Amount': D('500.00'), 'LinkedTxn': [{'TxnId': q2, 'TxnType': 'Invoice'}]}]},
                       existing=p)
        self.webhook([{'name': 'Payment', 'id': pid, 'operation': 'Update', 'lastUpdated': '2026-09-30T10:00:00Z'}])
        self.assertEqual(list(Payment.objects.filter(source='QBO').values_list('invoice_id', 'external_id')),
                         [(inv2.pk, f'{pid}:{q2}')])

    def test_credit_memo_raised_in_qbo_is_imported_when_it_maps_cleanly(self):
        cm = self.qbo.create_credit_memo(self.cid, [FakeQBO.sales_line(D('100.00'), description='Damaged pallet')],
                                         date(2026, 9, 11), 'QCM-1')
        app = self.qbo.apply_credit(cm, self.qid, D('115.00'), date(2026, 9, 11))
        self.webhook([{'name': 'Payment', 'id': app, 'operation': 'Create'}])
        cn = CreditNote.objects.get(invoice=self.inv, source='QBO')
        self.assertEqual((cn.external_id, cn.total_amount, cn.vat_amount), (cm, D('115.00'), D('15.00')))
        self.assertIn('QCM-1', cn.reason)
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.credited_amount, self.inv.paid_amount), (D('115.00'), D('0.00')))
        self.assertFalse(Payment.objects.filter(invoice=self.inv).exists())   # a credit, not money
        self.assertEqual(self.link('CREDIT_NOTE', cn.pk).status, 'SYNCED')
        self.assertEqual(len(self.qbo.credit_memos()), 1)                    # never pushed back

    def test_partly_applied_qbo_credit_memo_is_reported_not_imported(self):
        cm = self.qbo.create_credit_memo(self.cid, [FakeQBO.sales_line(D('100.00'))], date(2026, 9, 11), 'QCM-2')
        app = self.qbo.apply_credit(cm, self.qid, D('50.00'), date(2026, 9, 11))
        self.webhook([{'name': 'Payment', 'id': app, 'operation': 'Create'}])
        self.assertFalse(CreditNote.objects.filter(invoice=self.inv, source='QBO').exists())
        self.assertTrue(self.conn.events.filter(level='ERROR', message__contains='QCM-2').exists())

    def test_webhook_for_an_unknown_company_is_ignored(self):
        body, sig = self.qbo.webhook_payload([{'name': 'Payment', 'id': '9', 'operation': 'Create',
                                               'realmId': '5555'}], key='test-verifier-token')
        with no_commit_delay(self):
            resp = APIClient(HTTP_HOST='localhost').generic('POST', WEBHOOK_URL, body, content_type='application/json',
                                                            HTTP_INTUIT_SIGNATURE=sig)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(AccountingWebhookEvent.objects.get(tenant_id='5555').processed_at)


# ====================================================================== reconciliation + disconnect

class ReconcileAndDisconnectTests(QBOFlowBase):
    def test_reconciliation_is_clean_then_flags_a_change_made_in_qbo(self):
        inv = self.issue()
        inv2 = self.issue(lines=[{'description': 'Freight', 'quantity': '2', 'unit_price': '16750',
                                  'discount_percent': '5', 'tax_code': 'STANDARD'}], issue=date(2026, 9, 9))
        self.qbo.record_payment({self.link('INVOICE', inv2.pk).external_id: D('1000.00')}, date(2026, 9, 20))
        self.poll()
        run = reconciliation.run(self.conn)
        self.assertEqual((run.status, run.difference_count), ('OK', 0), list(run.differences.values()))
        self.qbo.void_invoice(self.link('INVOICE', inv.pk).external_id)
        run = reconciliation.run(self.conn)
        self.assertEqual(run.status, 'DIFFERENCES')
        fields = {(d.scope, d.field) for d in run.differences.all()}
        self.assertIn(('INVOICE', 'status'), fields)
        self.assertIn(('MONTH', 'sales_excl_vat'), fields)
        self.assertIn(('CUSTOMER', 'open_balance'), fields)
        body = self.api().get('/api/v1/integrations/accounting/connection/reconciliation/').json()
        self.assertTrue(any('qbo.intuit.com/app/invoice' in (d['provider_url'] or '') for d in body['differences']))

    def test_disconnect_revokes_and_frees_manual_payments(self):
        inv = self.issue()
        resp = self.api().post('/api/v1/integrations/accounting/connection/disconnect/')
        self.assertEqual(resp.status_code, 200)
        revokes = self.qbo.calls_to('POST', r'/tokens/revoke$')
        self.assertEqual(len(revokes), 1)
        self.assertTrue(revokes[0].json['token'])
        self.conn.refresh_from_db()
        self.assertEqual((self.conn.status, self.conn.access_token, self.conn.refresh_token), ('DISABLED', '', ''))
        self.assertIsNone(self.qbo.current_refresh_token())
        resp = self.api().post('/api/v1/payments/', {'invoice': inv.pk, 'amount': '1.00', 'payment_date': '2026-09-06',
                                                     'payment_method': 'EFT'}, format='json')
        self.assertEqual(resp.status_code, 201)


# ====================================================================== setup API: contacts wizard + backfill

class SetupApiTests(QBOFlowBase):
    map_on_setup = False

    def test_backfill_pushes_historic_receipts_as_payments_and_overpayments(self):
        inv = self.issue(issue=date(2026, 8, 10))
        from core.services.payments import record_payment
        record_payment(self.co, self.admin, {'invoice': inv.pk, 'amount': '1000.00', 'payment_date': '2026-08-12',
                                             'payment_method': 'EFT'})
        small = self.issue(lines=[{'description': 'Freight', 'quantity': '1', 'unit_price': '100', 'tax_code': 'STANDARD'}],
                           issue=date(2026, 8, 11))
        record_payment(self.co, self.admin, {'invoice': small.pk, 'amount': '200.00', 'payment_date': '2026-08-13',
                                             'payment_method': 'EFT'}, allow_overpayment=True)
        map_everything(self.conn)
        url = '/api/v1/integrations/accounting/connection/backfill/'
        with no_commit_delay(self):
            resp = self.api().post(url, {'cutover_date': '2026-08-01'}, format='json')
        self.assertEqual(resp.status_code, 202, resp.content)
        status = self.api().get(url).json()
        self.assertEqual(status['state'], 'DONE', status)
        p = Payment.objects.get(invoice=inv)
        q = self.qinv(inv)
        self.assertEqual((p.source, D(q['Balance'])), ('QBO', inv.total_amount - D('1000.00')))
        self.assertEqual(p.external_id, f'{q["LinkedTxn"][0]["TxnId"]}:{q["Id"]}')
        # 115.00 due, 200.00 received: a payment plus an unapplied payment (customer credit).
        rows = sorted(Payment.objects.filter(invoice=small).values_list('external_id', 'amount'), key=lambda r: r[0])
        self.assertEqual([r[1] for r in rows], [D('115.00'), D('85.00')])
        ovp_id = rows[1][0].split(':', 1)[1]
        self.assertTrue(rows[1][0].startswith('OVPREM:'))
        self.assertEqual(D(self.qbo.payment(ovp_id)['UnappliedAmt']), D('85.00'))
        # Nothing changes on a second poll (the adopted rows match QBO's ids).
        self.later()
        totals = self.poll()
        self.assertEqual((totals['created'], totals['removed']), (0, 0))
        # The credit is used on another invoice in QBO -> the remainder row goes.
        other = self.issue(lines=[{'description': 'Freight', 'quantity': '1', 'unit_price': '500', 'tax_code': 'STANDARD'}],
                           issue=date(2026, 9, 2))
        qo = self.link('INVOICE', other.pk).external_id
        self.later(2)
        self.qbo.apply_payment(ovp_id, qo, D('85.00'))
        self.poll()
        self.assertFalse(Payment.objects.filter(external_id__startswith='OVPREM:').exists())
        self.assertEqual(Payment.objects.get(invoice=other).external_id, f'{ovp_id}:{qo}')
        run = reconciliation.run(self.conn)
        self.assertNotIn('INVOICE', {d.scope for d in run.differences.all()})

    def test_contact_wizard_matches_by_email_and_suggests_by_name(self):
        self.qbo.create_customer('Acme Mining', email='AR@acme.test')
        self.qbo.create_customer('BRAVO BULK LOGISTICS')
        bravo = Customer.objects.create(company=self.co, name='Bravo Bulk Logistics (Pty) Ltd', email='b@b.test',
                                        credit_score=80)
        carry = Customer.objects.create(company=self.co, name='Carry Co', email='c@c.test', credit_score=80)
        sup = Supplier.objects.create(company=self.co, name='Bravo Bulk Logistics', vat_number='4222222222')
        Expense.objects.create(company=self.co, expense_number=f'EXP-{self.co.pk}-9', category='FUEL', supplier=sup,
                               description='Diesel', amount=D('100.00'), expense_date=date(2026, 8, 3))
        map_everything(self.conn)
        for c in (self.cust, bravo, carry):
            self.issue(customer=c, issue=date(2026, 8, 10))
        resp = self.api().post('/api/v1/integrations/accounting/connection/contacts/run-matching/')
        self.assertEqual(resp.status_code, 200, resp.content)
        # Customers and vendors are read separately (no shared list in QBO):
        # the supplier isn't matched against the customer named like it.
        self.assertEqual(resp.json()['summary'], {'MATCHED': 1, 'SUGGESTED': 1, 'UNMATCHED': 0, 'CREATE': 2,
                                                  'SKIPPED': 0})
        self.assertEqual(self.link('CONTACT_CUSTOMER', self.cust.pk).match_method, 'email')
        self.assertEqual(self.link('CONTACT_SUPPLIER', sup.pk).status, 'CREATE')
        self.assertTrue(any('FROM Vendor' in q for q in self.qbo.queries()))
