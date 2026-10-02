"""End-to-end: TruckWys <-> the fake Xero ledger (core/tests/accounting/fake_xero.py).

Covers connect / org picker / currency guard, mapping gate, contact matching,
invoice / credit note / bill pushes (draft -> verify -> authorise), duplicate
detection, retries (429 Retry-After, 5xx backoff), token refresh with
rotation, re-auth, webhooks, payment mirror (new, deleted, overpayment and
prepayment allocations, credit notes raised in Xero), reconciliation and
disconnect.
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
    AccountingConnection, CreditNote, Customer, Expense, ExternalLink, Invoice, Payment, Supplier,
)
from core.tests.accounting.fake_xero import FakeXero
from core.tests.accounting.xero_helpers import (
    connect, map_everything, no_commit_delay, xero_settings,
)

D = Decimal
CUTOVER = date(2026, 9, 1)


def make_user(username, company, role='ADMIN'):
    u = get_user_model().objects.create_user(username=username, email=f'{username}@xs.test', password='x')
    u.role, u.company = role, company
    u.save()
    return u


class XeroFlowBase(TestCase):
    """A connected, mapped company with a cut-over, on a fresh fake Xero."""
    connect_on_setup = True
    map_on_setup = True
    cutover = CUTOVER

    def setUp(self):
        from core.models import Company
        self._settings = xero_settings()
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.xero = FakeXero()
        self._transport = use_transport(self.xero)
        self._transport.__enter__()
        self.addCleanup(self._transport.__exit__, None, None, None)
        self.co = Company.objects.create(company_name='Flow Haulage (Pty) Ltd', vat_number='4987654321')
        self.admin = make_user(f'flow_admin_{self.co.pk}', self.co)
        self.cust = Customer.objects.create(company=self.co, name='Acme Mining (Pty) Ltd', email='ar@acme.test',
                                            vat_number='4123456789', credit_score=80)
        self.conn = None
        if self.connect_on_setup:
            self.conn, outcome = connect(self.co, self.admin, self.xero)
            self.assertEqual(outcome, 'connected')
            if self.map_on_setup:
                map_everything(self.conn)
                s = dict(self.conn.settings)
                s['cutover_date'] = self.cutover.isoformat()
                self.conn.settings = s
                self.conn.save()

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

    def xinv(self, inv):
        return self.xero.find_invoice(inv.invoice_number)

    def webhook(self, invoice_id):
        body, sig = self.xero.webhook_payload([{
            'resourceUrl': f'https://api.xero.com/api.xro/2.0/Invoices/{invoice_id}', 'resourceId': invoice_id,
            'eventDateUtc': timezone.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3], 'eventType': 'UPDATE',
            'eventCategory': 'INVOICE', 'tenantId': self.conn.tenant_id, 'tenantType': 'ORGANISATION'}],
            key='test-webhook-key')
        with no_commit_delay(self):
            resp = APIClient(HTTP_HOST='localhost').generic(
                'POST', '/api/v1/integrations/xero/webhooks/', body, content_type='application/json',
                HTTP_X_XERO_SIGNATURE=sig)
        self.assertEqual(resp.status_code, 200)
        return resp

    def poll(self):
        self.conn.refresh_from_db()
        return pull.poll_payments(self.conn)


# ====================================================================== connect

class ConnectTests(XeroFlowBase):
    connect_on_setup = False

    def test_single_zar_org_connects_and_reads_its_settings(self):
        conn, outcome = connect(self.co, self.admin, self.xero)
        self.assertEqual(outcome, 'connected')
        self.assertEqual(conn.status, 'ACTIVE')
        self.assertEqual(conn.base_currency, 'ZAR')
        self.assertTrue(conn.access_token.startswith('enc:'))
        self.assertTrue(conn.refresh_token.startswith('enc:'))
        opts = conn.settings['options']
        self.assertTrue(any(a['code'] == '200' for a in opts['accounts']))
        self.assertTrue(any(t['code'] == 'OUTPUT2' for t in opts['tax_rates']))
        body = self.api().get('/api/v1/integrations/accounting/connection/').json()
        self.assertEqual(body['status'], 'ACTIVE')
        self.assertFalse(body['readiness']['sync_enabled'])
        self.assertFalse(body['payments_managed_externally'])

    def test_several_orgs_need_a_choice_and_the_others_are_released(self):
        self.xero = FakeXero(orgs=[
            {'tenant_id': 'org-za', 'name': 'Flow Haulage', 'currency': 'ZAR', 'short_code': '!za', 'country': 'ZA'},
            {'tenant_id': 'org-us', 'name': 'Flow USA', 'currency': 'USD', 'short_code': '!us', 'country': 'US'},
        ])
        with use_transport(self.xero):
            conn, outcome = connect(self.co, self.admin, self.xero)
            self.assertEqual(outcome, 'choose_org')
            self.assertEqual(conn.status, 'PENDING_ORG')
            resp = self.api().post('/api/v1/integrations/accounting/connection/select-org/',
                                   {'tenant_id': 'org-us'}, format='json')
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()['code'], 'currency_not_supported')
            resp = self.api().post('/api/v1/integrations/accounting/connection/select-org/',
                                   {'tenant_id': 'org-za'}, format='json')
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertEqual(resp.json()['status'], 'ACTIVE')
            self.assertTrue(self.xero.calls_to('DELETE', r'/connections/'))

    def test_a_single_non_zar_org_is_refused_and_released(self):
        self.xero = FakeXero(orgs=[{'tenant_id': 'org-gb', 'name': 'Flow UK', 'currency': 'GBP',
                                    'short_code': '!gb', 'country': 'GB'}])
        with use_transport(self.xero):
            with self.assertRaises(conn_svc.ConnectError) as ctx:
                connect(self.co, self.admin, self.xero)
        self.assertEqual(ctx.exception.code, 'currency_not_supported')
        self.assertFalse(AccountingConnection.objects.filter(company=self.co, status='ACTIVE').exists())
        self.assertTrue(self.xero.calls_to('POST', r'connect/revocation'))

    def test_callback_redirects_with_reason_and_rejects_bad_state(self):
        resp = APIClient(HTTP_HOST='localhost').get('/api/v1/integrations/xero/callback/',
                                                    {'code': 'x', 'state': 'forged'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('result=error&reason=state_invalid', resp['Location'])
        resp = APIClient(HTTP_HOST='localhost').get('/api/v1/integrations/xero/callback/', {'error': 'access_denied'})
        self.assertIn('reason=denied', resp['Location'])

    def test_full_callback_through_the_view(self):
        from urllib.parse import parse_qs, urlparse
        start = self.api().post('/api/v1/integrations/accounting/xero/connect/').json()
        # Our own start page first (sets the browser nonce), then Xero.
        self.assertTrue(start['auth_url'].startswith(
            'https://api.truckwys.test/api/v1/integrations/accounting/xero/start/?ticket='), start['auth_url'])
        browser = APIClient(HTTP_HOST='localhost')
        hop = browser.get(urlparse(start['auth_url']).path + '?' + urlparse(start['auth_url']).query)
        self.assertEqual(hop.status_code, 302)
        self.assertTrue(hop['Location'].startswith('https://login.xero.com/identity/connect/authorize?'))
        self.assertIn('scope=openid%20profile', hop['Location'])
        self.assertIn('tw_acct_oauth', hop.cookies)
        # The ticket is single use.
        again = APIClient(HTTP_HOST='localhost').get(urlparse(start['auth_url']).path + '?' +
                                                     urlparse(start['auth_url']).query)
        self.assertIn('reason=state_invalid', again['Location'])
        state = parse_qs(urlparse(hop['Location']).query)['state'][0]
        code = self.xero.authorize(redirect_uri='https://api.truckwys.test/api/v1/integrations/xero/callback/')
        # Someone else's browser (no cookie) can't complete it...
        stranger = APIClient(HTTP_HOST='localhost').get('/api/v1/integrations/xero/callback/',
                                                        {'code': code, 'state': state})
        self.assertIn('reason=browser_mismatch', stranger['Location'])
        self.assertFalse(AccountingConnection.objects.filter(company=self.co).exists())
        # ... the admin's browser can.
        code = self.xero.authorize(redirect_uri='https://api.truckwys.test/api/v1/integrations/xero/callback/')
        resp = browser.get('/api/v1/integrations/xero/callback/', {'code': code, 'state': state})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('https://app.truckwys.test/settings/integrations/accounting?'))
        self.assertIn('result=connected', resp['Location'])

    def test_only_one_accounting_system_at_a_time(self):
        connect(self.co, self.admin, self.xero)
        resp = self.api().post('/api/v1/integrations/accounting/xero/connect/')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['code'], 'already_connected')

    def test_non_admin_cannot_connect(self):
        disp = make_user('flow_disp', self.co, role='DISPATCHER')
        self.assertEqual(self.api(disp).post('/api/v1/integrations/accounting/xero/connect/').status_code, 403)


# ====================================================================== pushing

class PushTests(XeroFlowBase):
    def test_issued_invoice_is_pushed_verified_and_authorised(self):
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        x = self.xinv(inv)
        self.assertEqual(x['Status'], 'AUTHORISED')
        self.assertEqual(x['InvoiceNumber'], inv.invoice_number)
        self.assertEqual((D(x['SubTotal']), D(x['TotalTax']), D(x['Total'])),
                         (inv.subtotal, inv.vat_amount, inv.total_amount))
        # 3 x 33.335 less 10% = 90.0045 -> 90.00 needs unitdp=4.
        self.assertEqual(D(x['LineItems'][1]['LineAmount']), D('90.00'))
        self.assertEqual([l['AccountCode'] for l in x['LineItems']], ['200', '260', '201', '200'])
        self.assertEqual([l['TaxType'] for l in x['LineItems']], ['OUTPUT2', 'OUTPUT2', 'OUTPUT2', 'ZERORATEDOUTPUT'])
        # Created as DRAFT, then authorised.
        puts = self.xero.calls_to('PUT', r'/Invoices$')
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][4]['Invoices'][0]['Status'], 'DRAFT')
        self.assertIn('Idempotency-Key', puts[0][3])
        # The contact was created (no match) with our identifiers.
        contact = self.xero.org()['contacts'][x['Contact']['ContactID']]
        self.assertEqual(contact['TaxNumber'], '4123456789')
        # Visible on the invoice API.
        body = self.api().get(f'/api/v1/invoices/{inv.pk}/').json()
        self.assertEqual(body['accounting_sync']['status'], 'SYNCED')
        self.assertIn('go.xero.com', body['accounting_sync']['url'])

    def test_drafts_and_pre_cutover_documents_never_leave(self):
        from core.services.invoice_lines import apply_lines
        from core.services.numbering import provisional_number
        draft = Invoice(company=self.co, customer=self.cust, invoice_number=provisional_number(),
                        issue_date=date(2026, 9, 5), due_date=date(2026, 10, 5), status='DRAFT',
                        subtotal=0, total_amount=0, balance=0)
        with no_commit_delay(self):
            apply_lines(draft, [{'description': 'x', 'unit_price': '100', 'tax_code': 'STANDARD'}])
        old = self.issue(issue=date(2026, 8, 20))
        self.assertIsNone(self.link('INVOICE', draft.pk))
        self.assertIsNone(self.link('INVOICE', old.pk))
        self.assertEqual(self.xero.calls_to('PUT', r'/Invoices$'), [])

    def test_unmapped_revenue_type_blocks_until_mapped(self):
        s = dict(self.conn.settings)
        s['revenue_types'] = {k: v for k, v in s['revenue_types'].items() if k != 'OTHER'}
        self.conn.settings = s
        self.conn.save()
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'BLOCKED')
        self.assertIn('revenue type OTHER', link.last_error)
        self.assertIsNone(self.xinv(inv))
        with no_commit_delay(self):
            map_everything(self.conn)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')

    def test_name_only_contact_match_waits_for_a_person(self):
        existing = self.xero.add_contact(name='ACME MINING')
        inv = self.issue(customer=Customer.objects.create(company=self.co, name='Acme Mining (Pty) Ltd.',
                                                          email='other@acme.test', credit_score=80))
        link = self.link('INVOICE', inv.pk)
        self.assertEqual(link.status, 'BLOCKED')
        clink = self.link('CONTACT_CUSTOMER', inv.customer_id)
        self.assertEqual((clink.status, clink.match_method), ('SUGGESTED', 'name'))
        with no_commit_delay(self):
            resp = self.api().post(f'/api/v1/integrations/accounting/connection/contacts/{clink.pk}/confirm/',
                                   {'external_id': existing}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['status'], 'MATCHED')
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(self.xinv(inv)['Contact']['ContactID'], existing)
        self.assertEqual(len(self.xero.org()['contacts']), 1)

    def test_vat_number_match_links_without_asking(self):
        existing = self.xero.add_contact(name='Acme (old name)', TaxNumber='4123456789')
        inv = self.issue()
        self.assertEqual(self.xinv(inv)['Contact']['ContactID'], existing)
        self.assertEqual(self.link('CONTACT_CUSTOMER', self.cust.pk).match_method, 'vat')

    def test_invoice_already_typed_into_xero_is_linked_not_duplicated(self):
        inv = self.issue(issue=date(2026, 8, 30))   # before cut-over: not pushed automatically
        cid = self.xero.add_contact(name='Acme Mining (Pty) Ltd', TaxNumber='4123456789')
        self.xero.add_invoice(contact_id=cid, number=inv.invoice_number, date=inv.issue_date,
                              lines=[{'Description': 'x', 'Quantity': '1', 'UnitAmount': str(inv.subtotal),
                                      'AccountCode': '200', 'TaxType': 'OUTPUT2',
                                      'TaxAmount': str(inv.vat_amount)}], status='AUTHORISED')
        s = dict(self.conn.settings)
        s['cutover_date'] = '2026-08-01'
        self.conn.settings = s
        self.conn.save()
        link = sync.get_or_create_link(self.conn, 'INVOICE', inv.pk)
        sync.run_link(link.pk)
        link.refresh_from_db()
        self.assertEqual(link.status, 'SYNCED')
        self.assertTrue(link.meta.get('matched_existing'))
        self.assertEqual(len([i for i in self.xero.org()['invoices'].values()
                              if i['InvoiceNumber'] == inv.invoice_number]), 1)

    def test_same_number_with_different_totals_is_dead_with_both_figures(self):
        inv = self.issue(issue=date(2026, 8, 30))
        cid = self.xero.add_contact(name='Acme Mining (Pty) Ltd', TaxNumber='4123456789')
        self.xero.add_invoice(contact_id=cid, number=inv.invoice_number, date=inv.issue_date,
                              lines=[{'Description': 'x', 'Quantity': '1', 'UnitAmount': '10.00',
                                      'AccountCode': '200', 'TaxType': 'OUTPUT2'}], status='AUTHORISED')
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

    def test_xero_calculating_differently_is_never_posted(self):
        """If Xero's totals disagree (simulated: the fake ignores TaxAmount),
        the draft is deleted and nothing reaches the ledger."""
        self.xero.honour_tax_amount = False
        inv = self.issue(lines=[{'description': 'Odd', 'quantity': '1', 'unit_price': '10.10', 'tax_code': 'STANDARD'}])
        cn_lines = [{'description': 'slice', 'quantity': '1', 'unit_price': '3.33', 'tax_code': 'STANDARD'}]
        from core.services.credit_notes import create_credit_note
        # 10.10 VAT 1.52; credit 3.33 (VAT 0.50) then the remaining 6.77, which must take
        # the remaining VAT 1.02 (round(6.77 x 15%) would be 1.02 too) -- use a mismatch instead:
        self.xero.force_tax_delta = D('0.01')
        with no_commit_delay(self):
            cn = create_credit_note(inv, user=self.admin, reason='slice', lines=cn_lines, issue_date=date(2026, 9, 6))
        link = self.link('CREDIT_NOTE', cn.pk)
        self.assertEqual(link.status, 'DEAD')
        self.assertIn('not posted', link.last_error)
        cns = [c for c in self.xero.org()['credit_notes'].values() if c['Status'] not in ('DELETED',)]
        self.assertEqual(cns, [])

    def test_credit_note_is_pushed_and_allocated_then_voided(self):
        from core.services.credit_notes import create_credit_note, void_credit_note
        inv = self.issue()
        line = inv.lines.get(position=2)   # fuel surcharge
        with no_commit_delay(self):
            cn = create_credit_note(inv, user=self.admin, reason='Surcharge waived', issue_date=date(2026, 9, 10),
                                    lines=[{'invoice_line': line.pk, 'description': 'Surcharge waived',
                                            'quantity': '1', 'unit_price': '1275.50', 'tax_code': 'STANDARD'}])
        link = self.link('CREDIT_NOTE', cn.pk)
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        xcn = self.xero.org()['credit_notes'][link.external_id]
        self.assertEqual(D(xcn['Total']), cn.total_amount)
        self.assertEqual(xcn['LineItems'][0]['AccountCode'], '201')   # same income account as the line
        x = self.xinv(inv)
        self.assertEqual(D(x['AmountCredited']), cn.total_amount)
        self.assertEqual(D(x['AmountDue']), inv.total_amount - cn.total_amount)
        with no_commit_delay(self):
            void_credit_note(cn, user=self.admin, reason='issued in error')
        self.assertEqual(self.xero.org()['credit_notes'][link.external_id]['Status'], 'VOIDED')
        self.assertEqual(D(self.xinv(inv)['AmountDue']), inv.total_amount)

    def test_voided_invoice_is_voided_in_xero(self):
        from core.services.credit_notes import void_invoice
        inv = self.issue()
        with no_commit_delay(self):
            void_invoice(inv, user=self.admin, reason='raised twice')
        self.assertEqual(self.xinv(inv)['Status'], 'VOIDED')
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'VOIDED')

    def test_supplier_expense_becomes_a_bill_and_follows_edits(self):
        from core.serializers import ExpenseSerializer
        from types import SimpleNamespace
        sup = Supplier.objects.create(company=self.co, name='Midrand Fuel Depot', vat_number='4111111111')
        ser = ExpenseSerializer(data={'category': 'TOLLS', 'description': 'N4 tolls', 'amount': '1150.00',
                                      'expense_date': '2026-09-03', 'tax_code': 'STANDARD', 'supplier': sup.pk,
                                      'expense_number': f'EXP-{self.co.pk}-1', 'receipt_number': 'TOLL-778'},
                                context={'request': SimpleNamespace(user=self.admin), 'company': self.co})
        ser.is_valid(raise_exception=True)
        with no_commit_delay(self):
            exp = ser.save(company=self.co, created_by=self.admin)
        link = self.link('BILL', exp.pk)
        self.assertEqual(link.status, 'SYNCED', link.last_error)
        bill = self.xero.org()['invoices'][link.external_id]
        self.assertEqual((bill['Type'], bill['Status'], bill['LineAmountTypes']), ('ACCPAY', 'AUTHORISED', 'Inclusive'))
        self.assertEqual((D(bill['Total']), D(bill['TotalTax'])), (D('1150.00'), D('150.00')))
        self.assertEqual(bill['LineItems'][0]['AccountCode'], '450')
        self.assertEqual(bill['InvoiceNumber'], 'TOLL-778')
        old_id = link.external_id
        with no_commit_delay(self):
            exp.amount = D('1265.00')
            exp.vat_amount = D('165.00')
            exp.save()
        # A posted bill isn't edited in place: a verified replacement is
        # posted, then the old one is voided.
        link.refresh_from_db()
        self.assertNotEqual(link.external_id, old_id)
        bill = self.xero.org()['invoices'][link.external_id]
        self.assertEqual((bill['Status'], D(bill['Total']), D(bill['TotalTax'])),
                         ('AUTHORISED', D('1265.00'), D('165.00')))
        self.assertEqual(self.xero.org()['invoices'][old_id]['Status'], 'VOIDED')
        with no_commit_delay(self):
            exp.status = 'PENDING'
            exp.save()
            exp.reject(self.admin)
        self.assertEqual(self.xero.org()['invoices'][link.external_id]['Status'], 'VOIDED')

    def test_expense_without_supplier_stays_in_truckwys(self):
        with no_commit_delay(self):
            exp = Expense.objects.create(company=self.co, expense_number=f'EXP-{self.co.pk}-2', category='OVERHEAD',
                                         description='Stationery', amount=D('99.99'), expense_date=date(2026, 9, 3))
        self.assertIsNone(self.link('BILL', exp.pk))


# ====================================================================== failures, retries, tokens

class RetryTests(XeroFlowBase):
    def test_429_waits_for_retry_after_then_succeeds(self):
        self.xero.fail_next('PUT', r'/Invoices$', status=429,
                            headers={'Retry-After': '42', 'X-Rate-Limit-Problem': 'minute'})
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual((link.status, link.attempts), ('ERROR', 0))
        wait = (link.next_attempt_at - timezone.now()).total_seconds()
        self.assertTrue(35 <= wait <= 43, wait)
        # The tenant is parked in the limiter too.
        from core.accounting.ratelimit import limiter_for
        self.assertGreater(limiter_for('XERO').blocked_for(self.conn.tenant_id), 30)
        limiter_for('XERO').block(self.conn.tenant_id, 1)   # let the test continue
        limiter_for('XERO').r.delete(f'{limiter_for("XERO").ns}:{self.conn.tenant_id}:blocked')
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(sync.retry_due(), 1)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.assertEqual(len([i for i in self.xero.org()['invoices'].values()
                              if i['InvoiceNumber'] == inv.invoice_number]), 1)

    def test_outage_backs_off_and_timeout_does_not_duplicate(self):
        self.xero.fail_next('PUT', r'/Invoices$', status=503)
        inv = self.issue()
        link = self.link('INVOICE', inv.pk)
        self.assertEqual((link.status, link.attempts), ('ERROR', 1))
        # Xero created the draft but the answer never arrived: the retry
        # must reuse it (Idempotency-Key / number lookup), not make a second.
        self.xero.timeout_next('PUT', r'/Invoices$', after_processing=True)
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        sync.retry_due()
        ExternalLink.objects.filter(pk=link.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        sync.retry_due()
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        live = [i for i in self.xero.org()['invoices'].values()
                if i['InvoiceNumber'] == inv.invoice_number and i['Status'] != 'DELETED']
        self.assertEqual(len(live), 1)

    def test_expired_access_token_is_refreshed_with_rotation(self):
        from core.utils.crypto import decrypt_secret
        old_refresh = decrypt_secret(self.conn.refresh_token)
        self.xero.expire_access_tokens()
        AccountingConnection.objects.filter(pk=self.conn.pk).update(
            access_token_expires_at=timezone.now() + timedelta(minutes=20))   # we think it's still valid
        inv = self.issue()
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        self.conn.refresh_from_db()
        self.assertNotEqual(decrypt_secret(self.conn.refresh_token), old_refresh)

    def test_revoked_refresh_token_needs_reauth_and_resumes_after_reconnect(self):
        self.xero.revoke_all_tokens()
        AccountingConnection.objects.filter(pk=self.conn.pk).update(
            access_token_expires_at=timezone.now() - timedelta(minutes=1))
        inv = self.issue()
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, 'NEEDS_REAUTH')
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'PENDING')
        # Manual payments stay refused while re-auth is pending.
        resp = self.api().post('/api/v1/payments/', {'invoice': inv.pk, 'amount': '1.00', 'payment_date': '2026-09-06',
                                                     'payment_method': 'EFT'}, format='json')
        self.assertEqual(resp.status_code, 409)
        with no_commit_delay(self):
            conn, outcome = connect(self.co, self.admin, self.xero)
        self.assertEqual((conn.pk, outcome, conn.status), (self.conn.pk, 'connected', 'ACTIVE'))
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')


# ====================================================================== payments back

class PaymentsBackTests(XeroFlowBase):
    def setUp(self):
        super().setUp()
        self.inv = self.issue()
        self.xid = self.link('INVOICE', self.inv.pk).external_id

    def test_webhook_payment_arrives_and_deletion_reverses_it(self):
        pid = self.xero.record_payment(self.xid, D('5000.00'), date(2026, 9, 12))
        self.webhook(self.xid)
        p = Payment.objects.get(invoice=self.inv, source='XERO')
        self.assertEqual((p.external_id, p.amount, p.payment_date), (pid, D('5000.00'), date(2026, 9, 12)))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, 'PARTIALLY_PAID')
        # Same webhook again: idempotent.
        self.webhook(self.xid)
        self.assertEqual(Payment.objects.filter(invoice=self.inv).count(), 1)
        # Deleted in Xero: the hourly poll removes it.
        self.xero.now = datetime.now(dt_timezone.utc) + timedelta(minutes=1)
        self.xero.delete_payment(pid)
        self.poll()
        self.assertFalse(Payment.objects.filter(invoice=self.inv).exists())
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.paid_amount, self.inv.balance), (D('0.00'), self.inv.total_amount))

    def test_full_payment_settles_the_invoice(self):
        self.xero.record_payment(self.xid, self.inv.total_amount, date(2026, 9, 20))
        self.poll()
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.status, self.inv.balance), ('PAID', D('0.00')))

    def test_overpayment_and_prepayment_allocations(self):
        cid = self.xinv(self.inv)['Contact']['ContactID']
        ovp = self.xero.create_overpayment(cid, D('1000.00'), date(2026, 9, 8))
        pre = self.xero.create_prepayment(cid, D('500.00'), date(2026, 9, 9))
        a1 = self.xero.allocate('OVERPAYMENT', ovp, self.xid, D('600.00'), date(2026, 9, 14))
        self.xero.allocate('PREPAYMENT', pre, self.xid, D('500.00'), date(2026, 9, 15))
        self.poll()
        rows = {p.external_id.split(':')[0]: p for p in Payment.objects.filter(invoice=self.inv, source='XERO')}
        self.assertEqual((rows['OVP'].amount, rows['OVP'].payment_date), (D('600.00'), date(2026, 9, 14)))
        self.assertEqual(rows['PRE'].amount, D('500.00'))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.paid_amount, D('1100.00'))
        # Allocation removed in Xero -> gone in TruckWys.
        self.xero.now = datetime.now(dt_timezone.utc) + timedelta(minutes=2)
        self.xero.remove_allocation('OVERPAYMENT', ovp, a1)
        self.poll()
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.paid_amount, D('500.00'))

    def test_credit_note_raised_in_xero_is_imported_when_it_maps_cleanly(self):
        cid = self.xinv(self.inv)['Contact']['ContactID']
        cn_id = self.xero.create_credit_note(cid, [{'Description': 'Damaged pallet', 'Quantity': '1',
                                                     'UnitAmount': '100.00', 'AccountCode': '200',
                                                     'TaxType': 'OUTPUT2'}], date(2026, 9, 11), 'XCN-1')
        self.xero.allocate('CREDIT_NOTE', cn_id, self.xid, D('115.00'), date(2026, 9, 11))
        self.webhook(self.xid)
        cn = CreditNote.objects.get(invoice=self.inv, source='XERO')
        self.assertEqual((cn.external_id, cn.total_amount, cn.vat_amount), (cn_id, D('115.00'), D('15.00')))
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.credited_amount, D('115.00'))
        # It is never pushed back.
        self.assertEqual(self.link('CREDIT_NOTE', cn.pk).status, 'SYNCED')
        self.assertEqual(len(self.xero.org()['credit_notes']), 1)

    def test_partly_allocated_xero_credit_note_is_reported_not_imported(self):
        cid = self.xinv(self.inv)['Contact']['ContactID']
        cn_id = self.xero.create_credit_note(cid, [{'Description': 'Goodwill', 'Quantity': '1',
                                                     'UnitAmount': '100.00', 'AccountCode': '200',
                                                     'TaxType': 'OUTPUT2'}], date(2026, 9, 11), 'XCN-2')
        self.xero.allocate('CREDIT_NOTE', cn_id, self.xid, D('50.00'), date(2026, 9, 11))
        self.webhook(self.xid)
        self.assertFalse(CreditNote.objects.filter(invoice=self.inv, source='XERO').exists())
        self.assertTrue(self.conn.events.filter(level='ERROR', message__contains='XCN-2').exists())

    def test_webhook_for_an_unknown_org_is_ignored(self):
        body, sig = self.xero.webhook_payload([{'resourceId': 'nope', 'eventCategory': 'INVOICE',
                                                'eventType': 'UPDATE', 'tenantId': 'someone-else',
                                                'eventDateUtc': '2026-09-30T10:00:00.000'}], key='test-webhook-key')
        with no_commit_delay(self):
            resp = APIClient(HTTP_HOST='localhost').generic('POST', '/api/v1/integrations/xero/webhooks/', body,
                                                            content_type='application/json', HTTP_X_XERO_SIGNATURE=sig)
        self.assertEqual(resp.status_code, 200)
        from core.models import AccountingWebhookEvent
        ev = AccountingWebhookEvent.objects.get(tenant_id='someone-else')
        self.assertIsNotNone(ev.processed_at)


# ====================================================================== reconciliation + disconnect

class ReconcileAndDisconnectTests(XeroFlowBase):
    def test_reconciliation_is_clean_then_flags_a_change_made_in_xero(self):
        inv = self.issue()
        self.issue(lines=[{'description': 'Freight', 'quantity': '2', 'unit_price': '16750', 'discount_percent': '5',
                           'tax_code': 'STANDARD'}], issue=date(2026, 9, 9))
        run = reconciliation.run(self.conn)
        self.assertEqual((run.status, run.difference_count), ('OK', 0), list(run.differences.values()))
        # Someone voids an invoice directly in Xero.
        self.xero.void_invoice(self.link('INVOICE', inv.pk).external_id)
        run = reconciliation.run(self.conn)
        self.assertEqual(run.status, 'DIFFERENCES')
        fields = {(d.scope, d.field) for d in run.differences.all()}
        self.assertIn(('INVOICE', 'status'), fields)
        self.assertIn(('MONTH', 'sales_excl_vat'), fields)
        body = self.api().get('/api/v1/integrations/accounting/connection/reconciliation/').json()
        self.assertEqual(body['run']['difference_count'], run.difference_count)
        self.assertTrue(any(d['provider_url'] for d in body['differences']))

    def test_disconnect_revokes_and_frees_manual_payments(self):
        inv = self.issue()
        resp = self.api().post('/api/v1/integrations/accounting/connection/disconnect/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.xero.calls_to('DELETE', r'/connections/'))
        self.assertTrue(self.xero.calls_to('POST', r'connect/revocation'))
        self.conn.refresh_from_db()
        self.assertEqual((self.conn.status, self.conn.access_token, self.conn.refresh_token), ('DISABLED', '', ''))
        resp = self.api().post('/api/v1/payments/', {'invoice': inv.pk, 'amount': '1.00', 'payment_date': '2026-09-06',
                                                     'payment_method': 'EFT'}, format='json')
        self.assertEqual(resp.status_code, 201)


# ====================================================================== setup API: contacts wizard + backfill

class SetupApiTests(XeroFlowBase):
    map_on_setup = False

    def test_backfill_refuses_until_ready_then_runs(self):
        inv = self.issue(issue=date(2026, 8, 10))   # nothing syncs yet: no cut-over chosen
        self.assertIsNone(self.link('INVOICE', inv.pk))
        url = '/api/v1/integrations/accounting/connection/backfill/'
        resp = self.api().post(url, {'cutover_date': '2026-08-01'}, format='json')
        self.assertEqual((resp.status_code, resp.json()['code']), (400, 'mapping_incomplete'))
        map_everything(self.conn, receipts_account=None)
        # A receipt recorded in TruckWys needs a receipts account first.
        from core.services.payments import record_payment
        record_payment(self.co, self.admin, {'invoice': inv.pk, 'amount': '1000.00', 'payment_date': '2026-08-12',
                                             'payment_method': 'EFT'})
        resp = self.api().post(url, {'cutover_date': '2026-08-01'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('bank account', resp.json()['error'])
        map_everything(self.conn)
        self.assertEqual(self.api().post(url, {'cutover_date': '2099-01-01'}, format='json').json()['code'],
                         'invalid_cutover')
        preview = self.api().get(url, {'cutover_date': '2026-08-01'}).json()['preview']
        self.assertEqual((preview['invoices'], preview['historic_receipts']), (1, 1))
        with no_commit_delay(self):
            resp = self.api().post(url, {'cutover_date': '2026-08-01'}, format='json')
        self.assertEqual(resp.status_code, 202, resp.content)
        status = self.api().get(url).json()
        self.assertEqual(status['state'], 'DONE', status)
        self.assertEqual(self.link('INVOICE', inv.pk).status, 'SYNCED')
        p = Payment.objects.get(invoice=inv)
        self.assertEqual(p.source, 'XERO')
        x = self.xinv(inv)
        self.assertEqual(D(x['AmountPaid']), D('1000.00'))
        self.assertEqual(x['Payments'][0]['PaymentID'], p.external_id)
        # The cut-over can only move earlier.
        resp = self.api().post(url, {'cutover_date': '2026-08-15'}, format='json')
        self.assertEqual(resp.json()['code'], 'invalid_cutover')

    def test_contact_wizard_lists_matches_and_suggestions(self):
        self.xero.add_contact(name='Acme Mining', TaxNumber='4123456789')
        self.xero.add_contact(name='BRAVO BULK LOGISTICS')
        bravo = Customer.objects.create(company=self.co, name='Bravo Bulk Logistics (Pty) Ltd', email='b@b.test',
                                        credit_score=80)
        carry = Customer.objects.create(company=self.co, name='Carry Co', email='c@c.test', credit_score=80)
        map_everything(self.conn)
        for c in (self.cust, bravo, carry):
            self.issue(customer=c, issue=date(2026, 8, 10))
        resp = self.api().post('/api/v1/integrations/accounting/connection/contacts/run-matching/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['summary'], {'MATCHED': 1, 'SUGGESTED': 1, 'UNMATCHED': 0, 'CREATE': 1,
                                                  'SKIPPED': 0})
        rows = self.api().get('/api/v1/integrations/accounting/connection/contacts/').json()['results']
        self.assertEqual(rows[0]['status'], 'SUGGESTED')   # suggestions first
        self.assertEqual(rows[0]['local_name'], 'Bravo Bulk Logistics (Pty) Ltd')
        self.assertEqual(rows[0]['candidates'][0]['name'], 'BRAVO BULK LOGISTICS')
        found = self.api().get('/api/v1/integrations/accounting/connection/contacts/search/', {'q': 'bravo'}).json()
        self.assertEqual([r['name'] for r in found['results']], ['BRAVO BULK LOGISTICS'])
        # Backfill refuses while a suggestion is open.
        resp = self.api().post('/api/v1/integrations/accounting/connection/backfill/', {'cutover_date': '2026-08-01'},
                               format='json')
        self.assertEqual(resp.json()['code'], 'contacts_unconfirmed')
        resp = self.api().post(f'/api/v1/integrations/accounting/connection/contacts/{rows[0]["id"]}/confirm/',
                               {'action': 'create'}, format='json')
        self.assertEqual(resp.json()['status'], 'CREATE')
        body = self.api().get('/api/v1/integrations/accounting/connection/').json()
        self.assertEqual(body['readiness']['contacts_to_confirm'], 0)
