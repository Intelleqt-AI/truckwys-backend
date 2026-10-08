"""Accounting integrations: units that need no provider (rate limiter,
webhook signatures, retry/backoff policy, the manual-payment guard, mapping
validation, contact matching decisions, token refresh under a lock)."""
import base64
import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock, skipIf

from django.db import connection as db_connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.accounting import sync
from core.accounting.base import (
    AuthError, Contact, PermanentError, RateLimited, TokenSet, TransientError,
)
from core.accounting.ratelimit import Limits, RateLimiter, redis_client
from core.models import (
    AccountingConnection, AccountingWebhookEvent, Company, Customer, ExternalLink, Invoice, Payment,
)
from core.utils.crypto import encrypt_secret

D = Decimal


def make_user(username, company, role='ADMIN'):
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.create_user(username=username, email=f'{username}@acct.test', password='x')
    u.role, u.company = role, company
    u.save()
    return u


def make_connection(company, **kw):
    defaults = dict(provider='XERO', status='ACTIVE', tenant_id=f'tenant-{uuid.uuid4().hex[:8]}',
                    tenant_name='Golden Haulage (Pty) Ltd', base_currency='ZAR',
                    access_token=encrypt_secret('access-1'), refresh_token=encrypt_secret('refresh-1'),
                    access_token_expires_at=timezone.now() + timedelta(minutes=25), connected_at=timezone.now())
    defaults.update(kw)
    return AccountingConnection.objects.create(company=company, **defaults)


def issued_invoice(company, customer, total='115.00', issue=None):
    from core.services.invoice_lines import apply_lines, due_date_for
    from core.services.numbering import provisional_number
    issue = issue or date(2026, 9, 1)
    inv = Invoice(company=company, customer=customer, invoice_number=provisional_number(), issue_date=issue,
                  due_date=due_date_for(issue, 'NET30'), payment_terms='NET30', status='DRAFT',
                  subtotal=0, total_amount=0, balance=0)
    apply_lines(inv, [{'description': 'Freight', 'quantity': '1', 'unit_price': str(D(total) / D('1.15')),
                       'tax_code': 'STANDARD'}])
    inv.mark_as_sent()
    return inv


# ---------------------------------------------------------------- rate limiter

@override_settings(REDIS_URL='redis://127.0.0.1:6379/15')
class RateLimiterTests(SimpleTestCase):
    def setUp(self):
        self.ns = f'test-rl-{uuid.uuid4().hex}'
        self.r = redis_client()

    def tearDown(self):
        for k in self.r.scan_iter(f'{self.ns}:*'):
            self.r.delete(k)

    def limiter(self, **kw):
        return RateLimiter('XERO', Limits(**{'per_minute': 3, 'per_day': 100, 'concurrent': 2, **kw}),
                           namespace=self.ns)

    def test_minute_window_is_a_sliding_limit(self):
        rl = self.limiter()
        for _ in range(3):
            with rl.acquire('t1'):
                pass
        with self.assertRaises(RateLimited) as ctx:
            with rl.acquire('t1'):
                pass
        self.assertEqual(ctx.exception.scope, 'minute')
        self.assertGreater(ctx.exception.retry_after, 50)
        # Another tenant has its own budget.
        with rl.acquire('t2'):
            pass

    def test_concurrency_slots_are_released(self):
        rl = self.limiter(per_minute=50)
        a = rl.acquire('t1')
        b = rl.acquire('t1')
        a.__enter__()
        b.__enter__()
        with self.assertRaises(RateLimited) as ctx:
            with rl.acquire('t1'):
                pass
        self.assertEqual(ctx.exception.scope, 'concurrent')
        a.__exit__(None, None, None)
        with rl.acquire('t1'):
            pass
        b.__exit__(None, None, None)

    def test_day_limit(self):
        rl = self.limiter(per_minute=50, per_day=2)
        for _ in range(2):
            with rl.acquire('t1'):
                pass
        with self.assertRaises(RateLimited) as ctx:
            with rl.acquire('t1'):
                pass
        self.assertEqual(ctx.exception.scope, 'day')

    def test_provider_429_blocks_the_tenant_until_retry_after(self):
        rl = self.limiter(per_minute=50)
        rl.block('t1', 30, 'minute')
        with self.assertRaises(RateLimited) as ctx:
            with rl.acquire('t1'):
                pass
        self.assertGreater(ctx.exception.retry_after, 25)
        self.assertLessEqual(ctx.exception.retry_after, 30)
        with rl.acquire('t2'):
            pass

    def test_app_wide_minute_limit(self):
        rl = self.limiter(per_minute=50, app_per_minute=2)
        with rl.acquire('a'):
            pass
        with rl.acquire('b'):
            pass
        with self.assertRaises(RateLimited) as ctx:
            with rl.acquire('c'):
                pass
        self.assertEqual(ctx.exception.scope, 'app minute')

    def test_redis_down_fails_closed_as_transient(self):
        class Dead:
            def pttl(self, *a):
                raise ConnectionError('down')
        rl = RateLimiter('XERO', Limits(1, 1, 1), client=Dead(), namespace=self.ns)
        with self.assertRaises(TransientError):
            with rl.acquire('t'):
                pass


class HttpErrorMappingTests(SimpleTestCase):
    def test_retry_after_parsing(self):
        from core.accounting.http import parse_retry_after
        self.assertEqual(parse_retry_after('45'), 45.0)
        self.assertEqual(parse_retry_after(None, 60), 60)
        self.assertEqual(parse_retry_after('garbage', 7), 7)
        future = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(time.time() + 120))
        self.assertTrue(100 <= parse_retry_after(future) <= 121)

    def test_backoff_grows_and_caps(self):
        waits = [sync.backoff_seconds(n) for n in range(1, 12)]
        self.assertTrue(20 <= waits[0] <= 40)
        self.assertGreater(waits[4], waits[1])
        self.assertLessEqual(max(waits), 6 * 3600 * 1.2)


# ---------------------------------------------------------------- webhooks

@override_settings(XERO_WEBHOOK_KEY='test-webhook-key', ACCOUNTING_SYNC_EAGER=False)
class XeroWebhookTests(TestCase):
    url = '/api/v1/integrations/xero/webhooks/'

    def sign(self, body: bytes, key='test-webhook-key'):
        return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()

    def post(self, body, signature):
        return APIClient(HTTP_HOST='localhost').generic('POST', self.url, body, content_type='application/json',
                                                        HTTP_X_XERO_SIGNATURE=signature)

    def test_intent_to_receive(self):
        """Xero's validation: a valid signature gets 200, an invalid one 401."""
        body = json.dumps({'events': [], 'firstEventSequence': 0, 'lastEventSequence': 0,
                           'entropy': 'ABCDEF'}).encode()
        self.assertEqual(self.post(body, self.sign(body)).status_code, 200)
        self.assertEqual(self.post(body, self.sign(body, 'wrong-key')).status_code, 401)
        self.assertEqual(self.post(body, '').status_code, 401)

    def test_tampered_body_is_refused(self):
        body = b'{"events": []}'
        sig = self.sign(body)
        self.assertEqual(self.post(b'{"events": [ ]}', sig).status_code, 401)

    @override_settings(XERO_WEBHOOK_KEY='')
    def test_no_key_configured_refuses_everything(self):
        body = b'{"events": []}'
        self.assertEqual(self.post(body, self.sign(body, '')).status_code, 401)

    def test_events_are_stored_once_and_answered_fast(self):
        ev = {'resourceUrl': 'https://api.xero.com/api.xro/2.0/Invoices/abc', 'resourceId': 'abc',
              'eventDateUtc': '2026-09-30T10:00:00.000', 'eventType': 'UPDATE', 'eventCategory': 'INVOICE',
              'tenantId': 'tenant-1', 'tenantType': 'ORGANISATION'}
        body = json.dumps({'events': [ev, ev], 'firstEventSequence': 1, 'lastEventSequence': 2,
                           'entropy': 'X'}).encode()
        started = time.monotonic()
        with mock.patch('core.accounting.pull._schedule_processing') as sched:
            resp = self.post(body, self.sign(body))
            self.post(body, self.sign(body))   # a redelivery
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AccountingWebhookEvent.objects.count(), 1)
        self.assertEqual(sched.call_count, 1)
        e = AccountingWebhookEvent.objects.get()
        self.assertEqual((e.tenant_id, e.resource_type, e.resource_id), ('tenant-1', 'INVOICE', 'abc'))


# ---------------------------------------------------------------- push failure policy

class StubAdapter:
    def __init__(self, exc=None):
        self.exc = exc


class RunLinkPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Policy Haulage')
        cls.conn = make_connection(cls.co, settings={'cutover_date': '2026-01-01'})

    def make_link(self):
        return ExternalLink.objects.create(company=self.co, connection=self.conn, provider='XERO',
                                           object_type='TEST', local_id=1, status='PENDING')

    def run_with(self, exc):
        link = self.make_link()

        def handler(connection, adapter, link):
            raise exc
        with mock.patch.dict(sync.HANDLERS, {'TEST': handler}), \
                mock.patch('core.accounting.sync.get_adapter', return_value=StubAdapter()):
            sync.run_link(link.pk)
        link.refresh_from_db()
        return link

    def test_transient_errors_back_off_then_go_dead(self):
        link = self.run_with(TransientError('Xero error 503'))
        self.assertEqual((link.status, link.attempts), ('ERROR', 1))
        self.assertGreater(link.next_attempt_at, timezone.now())
        link.attempts = sync.MAX_ATTEMPTS - 1
        link.status = 'ERROR'
        link.save()

        def handler(connection, adapter, link):
            raise TransientError('still down')
        with mock.patch.dict(sync.HANDLERS, {'TEST': handler}), \
                mock.patch('core.accounting.sync.get_adapter', return_value=StubAdapter()):
            sync.run_link(link.pk)
        link.refresh_from_db()
        self.assertEqual(link.status, 'DEAD')
        self.assertIn('Gave up', link.last_error)

    def test_rate_limit_waits_retry_after_without_counting_an_attempt(self):
        link = self.run_with(RateLimited('429', retry_after=90))
        self.assertEqual((link.status, link.attempts), ('ERROR', 0))
        wait = (link.next_attempt_at - timezone.now()).total_seconds()
        self.assertTrue(85 <= wait <= 91, wait)

    def test_permanent_error_is_dead_and_logged(self):
        link = self.run_with(PermanentError('Account 999 is not valid'))
        self.assertEqual(link.status, 'DEAD')
        self.assertTrue(self.conn.events.filter(level='ERROR', message__contains='Account 999').exists())

    def test_auth_error_flags_reconnect_and_keeps_the_document_queued(self):
        link = self.run_with(AuthError('refused'))
        self.assertEqual(link.status, 'PENDING')
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.status, 'NEEDS_REAUTH')

    def test_a_link_is_claimed_once(self):
        link = self.make_link()
        ExternalLink.objects.filter(pk=link.pk).update(status='RUNNING', updated_at=timezone.now())
        self.assertIsNone(sync.run_link(link.pk))
        # A worker that died long ago doesn't hold it forever.
        ExternalLink.objects.filter(pk=link.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        with mock.patch.dict(sync.HANDLERS, {'TEST': lambda c, a, l: sync._finish(l, 'SYNCED')}), \
                mock.patch('core.accounting.sync.get_adapter', return_value=StubAdapter()):
            self.assertEqual(sync.run_link(link.pk), 'SYNCED')


# ---------------------------------------------------------------- manual payments refused while connected

class PaymentGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Guard Haulage')
        cls.admin = make_user('guard_admin', cls.co)
        cls.cust = Customer.objects.create(company=cls.co, name='Acme', email='a@guard.test', credit_score=80)

    def setUp(self):
        self.inv = issued_invoice(self.co, self.cust)
        self.api = APIClient(HTTP_HOST='localhost')
        self.api.force_authenticate(self.admin)

    def test_manual_payments_work_until_the_cutover_is_chosen(self):
        make_connection(self.co)   # connected, setup unfinished
        resp = self.api.post('/api/v1/payments/', {'invoice': self.inv.pk, 'amount': '10.00',
                                                   'payment_date': '2026-09-02', 'payment_method': 'EFT'},
                             format='json')
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_manual_payment_api_refused_with_provider_details(self):
        make_connection(self.co, settings={'cutover_date': '2026-08-01'})
        resp = self.api.post('/api/v1/payments/', {'invoice': self.inv.pk, 'amount': '10.00',
                                                   'payment_date': '2026-09-02', 'payment_method': 'EFT'},
                             format='json')
        self.assertEqual(resp.status_code, 409, resp.content)
        body = resp.json()
        self.assertEqual(body['code'], 'payments_managed_by_accounting')
        self.assertEqual((body['provider'], body['provider_name']), ('XERO', 'Xero'))
        self.assertIn('Record this payment in Xero', body['error'])
        mark = self.api.post(f'/api/v1/invoices/{self.inv.pk}/mark_paid/', {}, format='json')
        self.assertEqual(mark.status_code, 409)
        self.assertEqual(Payment.objects.filter(invoice=self.inv).count(), 0)

    def test_editing_or_deleting_old_manual_payments_is_refused_too(self):
        from core.services.payments import record_payment
        p = record_payment(self.co, self.admin, {'invoice': self.inv.pk, 'amount': '5.00',
                                                 'payment_date': '2026-09-02', 'payment_method': 'EFT'}).instance
        make_connection(self.co, status='NEEDS_REAUTH', settings={'cutover_date': '2026-08-01'})
        self.assertEqual(self.api.patch(f'/api/v1/payments/{p.pk}/', {'amount': '6.00'}, format='json').status_code,
                         409)
        self.assertEqual(self.api.delete(f'/api/v1/payments/{p.pk}/').status_code, 409)

    def test_synced_payments_still_record(self):
        from core.services.payments import record_payment
        make_connection(self.co, settings={'cutover_date': '2026-08-01'})
        s = record_payment(self.co, None, {'invoice': self.inv.pk, 'amount': '115.00', 'payment_date': '2026-09-03',
                                           'payment_method': 'EFT', 'source': 'XERO', 'external_id': 'pay-1'},
                           allow_overpayment=True)
        self.assertEqual(s.instance.source, 'XERO')
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, 'PAID')

    def test_disconnected_means_manual_again(self):
        make_connection(self.co, status='DISABLED', settings={'cutover_date': '2026-08-01'})
        resp = self.api.post('/api/v1/payments/', {'invoice': self.inv.pk, 'amount': '10.00',
                                                   'payment_date': '2026-09-02', 'payment_method': 'EFT'},
                             format='json')
        self.assertEqual(resp.status_code, 201)

    def test_connection_api_tells_the_ui(self):
        make_connection(self.co, settings={'cutover_date': '2026-08-01'})
        body = self.api.get('/api/v1/integrations/accounting/connection/').json()
        self.assertTrue(body['payments_managed_externally'])
        self.assertNotIn('access_token', json.dumps(body))
        self.assertNotIn('refresh', json.dumps(body))


# ---------------------------------------------------------------- mapping

OPTIONS = {
    'accounts': [{'code': '200', 'name': 'Sales', 'type': 'REVENUE', 'class': 'REVENUE', 'is_bank': False},
                 {'code': '201', 'name': 'Fuel surcharge', 'type': 'REVENUE', 'class': 'REVENUE', 'is_bank': False},
                 {'code': '449', 'name': 'Motor vehicle', 'type': 'EXPENSE', 'class': 'EXPENSE', 'is_bank': False},
                 {'code': '090', 'name': 'Bank', 'type': 'BANK', 'class': 'ASSET', 'is_bank': True}],
    'tax_rates': [{'code': 'OUTPUT2', 'name': 'Standard Rate Sales', 'rate': '15.00', 'revenue': True, 'expenses': False},
                  {'code': 'OUTPUT3', 'name': 'Old Standard Rate', 'rate': '14.00', 'revenue': True, 'expenses': False},
                  {'code': 'INPUT2', 'name': 'Standard Rate Purchases', 'rate': '15.00', 'revenue': False, 'expenses': True},
                  {'code': 'ZERORATEDOUTPUT', 'name': 'Zero Rated', 'rate': '0.00', 'revenue': True, 'expenses': False},
                  {'code': 'ZERORATEDINPUT', 'name': 'Zero Rated Purchases', 'rate': '0.00', 'revenue': False, 'expenses': True},
                  {'code': 'EXEMPTOUTPUT', 'name': 'Exempt Sales', 'rate': '0.00', 'revenue': True, 'expenses': False},
                  {'code': 'EXEMPTINPUT', 'name': 'Exempt Purchases', 'rate': '0.00', 'revenue': False, 'expenses': True},
                  {'code': 'NONE', 'name': 'No VAT', 'rate': '0.00', 'revenue': True, 'expenses': True}],
    'tracking_categories': [{'id': 'cat-veh', 'name': 'Vehicle', 'options': []},
                            {'id': 'cat-reg', 'name': 'Region', 'options': [{'id': 'o1', 'name': 'Gauteng'}]}],
    'fetched_at': '2026-10-01T00:00:00+00:00',
}


class MappingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Map Haulage')
        cls.admin = make_user('map_admin', cls.co)
        cls.viewer = make_user('map_dispatch', cls.co, role='DISPATCHER')

    def setUp(self):
        self.conn = make_connection(self.co, settings={'options': OPTIONS})
        self.api = APIClient(HTTP_HOST='localhost')
        self.api.force_authenticate(self.admin)
        self.url = '/api/v1/integrations/accounting/connection/mapping/'

    def test_suggestions_are_offered_never_applied(self):
        body = self.api.get(self.url).json()
        self.assertFalse(body['complete'])
        self.assertEqual(body['suggestions']['tax_sales']['STANDARD'], 'OUTPUT2')
        self.assertEqual(body['suggestions']['tax_purchases']['ZERO_RATED'], 'ZERORATEDINPUT')
        self.assertTrue(all(r['tax_code'] is None for r in body['tax_sales']))
        self.assertIn('revenue:FREIGHT', body['missing'])

    def test_a_wrong_rate_or_account_is_refused(self):
        resp = self.api.put(self.url, {'tax_sales': {'STANDARD': 'OUTPUT3', 'ZERO_RATED': 'ZERORATEDINPUT'},
                                       'revenue_types': {'FREIGHT': '090', 'TOLLS': '999'},
                                       'receipts_account': '200'}, format='json')
        self.assertEqual(resp.status_code, 400)
        errors = resp.json()['errors']
        self.assertIn('15', errors['tax_sales.STANDARD'])
        self.assertIn("can't be used on sales", errors['tax_sales.ZERO_RATED'])
        self.assertIn('bank account', errors['revenue_types.FREIGHT'])
        self.assertIn("doesn't exist", errors['revenue_types.TOLLS'])
        self.assertIn('receipts_account', errors)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.settings.get('tax_sales', {}), {})

    def test_complete_mapping(self):
        from core.revenue_types import REVENUE_TYPES
        from core.accounting.mapping import EXPENSE_CATEGORIES
        payload = {
            'revenue_types': {k: '200' for k in REVENUE_TYPES} | {'FUEL_SURCHARGE': '201'},
            'expense_categories': {k: '449' for k in EXPENSE_CATEGORIES},
            'tax_sales': {'STANDARD': 'OUTPUT2', 'ZERO_RATED': 'ZERORATEDOUTPUT', 'EXEMPT': 'EXEMPTOUTPUT',
                          'NO_VAT': 'NONE'},
            'tax_purchases': {'STANDARD': 'INPUT2', 'ZERO_RATED': 'ZERORATEDINPUT', 'EXEMPT': 'EXEMPTINPUT',
                              'NO_VAT': 'NONE'},
            'receipts_account': '090',
            'tracking': {'vehicle_category_id': 'cat-veh', 'branch_category_id': 'cat-reg', 'branch_option': 'Gauteng'},
        }
        resp = self.api.put(self.url, payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['complete'])
        self.assertEqual(resp.json()['missing'], [])
        # A tracking option that doesn't exist is refused.
        bad = self.api.put(self.url, {'tracking': {'branch_option': 'Mars'}}, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('tracking.branch_option', bad.json()['errors'])

    def test_non_admins_can_read_not_write(self):
        api = APIClient(HTTP_HOST='localhost')
        api.force_authenticate(self.viewer)
        self.assertEqual(api.get(self.url).status_code, 200)
        self.assertEqual(api.put(self.url, {}, format='json').status_code, 403)


# ---------------------------------------------------------------- contact matching decisions

class ContactDecisionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Match Haulage')
        cls.conn = make_connection(cls.co)

    def cust(self, **kw):
        base = dict(company=self.co, name='Acme Mining (Pty) Ltd', email=f'{uuid.uuid4().hex[:6]}@acme.test',
                    credit_score=80)
        base.update(kw)
        return Customer.objects.create(**base)

    def decide(self, obj, contacts):
        from core.accounting.contacts import decide
        return decide(self.conn, 'CONTACT_CUSTOMER', obj, contacts)

    def test_order_of_evidence(self):
        c = self.cust(vat_number='4123456789', registration_number='2015/123456/07', email='ar@acme.test')
        by_vat = Contact(name='Totally different name', external_id='x1', vat_number='4123 456 789')
        by_reg = Contact(name='Other', external_id='x2', registration_number='201512345607')
        by_email = Contact(name='Other 2', external_id='x3', email='AR@ACME.TEST')
        by_name = Contact(name='ACME MINING', external_id='x4')
        self.assertEqual(self.decide(c, [by_name, by_email, by_reg, by_vat])[:3][:2], ('SYNCED', 'vat'))
        self.assertEqual(self.decide(c, [by_name, by_email, by_reg])[:2], ('SYNCED', 'registration'))
        self.assertEqual(self.decide(c, [by_name, by_email])[:2], ('SYNCED', 'email'))
        status, method, contact, cands = self.decide(c, [by_name])
        self.assertEqual((status, method, contact), ('SUGGESTED', 'name', None))
        self.assertEqual(cands[0]['external_id'], 'x4')
        self.assertEqual(self.decide(c, [Contact(name='Unrelated', external_id='x9')])[0], 'CREATE')

    def test_two_contacts_with_one_vat_number_need_a_person(self):
        c = self.cust(vat_number='4123456789')
        status, method, contact, cands = self.decide(c, [Contact(name='A', external_id='a', vat_number='4123456789'),
                                                         Contact(name='B', external_id='b', vat_number='4123456789')])
        self.assertEqual((status, method, len(cands)), ('SUGGESTED', 'vat', 2))

    def test_a_contact_already_linked_elsewhere_is_only_suggested(self):
        other = self.cust(name='Acme Two', email='two@acme.test')
        ExternalLink.objects.create(company=self.co, connection=self.conn, provider='XERO',
                                    object_type='CONTACT_CUSTOMER', local_id=other.pk, external_id='x1',
                                    status='SYNCED')
        c = self.cust(email='shared@acme.test')
        status, method, contact, cands = self.decide(c, [Contact(name='Acme', external_id='x1',
                                                                 email='shared@acme.test')])
        self.assertEqual(status, 'SUGGESTED')


# ---------------------------------------------------------------- tokens

class FakeRefreshAdapter:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.lock = threading.Lock()

    def refresh(self, refresh_token):
        with self.lock:
            self.calls += 1
            n = self.calls
        time.sleep(0.2)   # widen the race window
        if self.fail:
            raise AuthError('invalid_grant')
        return TokenSet(access_token=f'access-{n + 1}', refresh_token=f'refresh-{n + 1}', expires_in=1800)


class TokenRefreshTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Token Haulage')

    def test_expiring_token_is_refreshed_and_rotation_is_stored(self):
        from core.accounting.tokens import token_getter
        from core.utils.crypto import decrypt_secret
        conn = make_connection(self.co, access_token_expires_at=timezone.now() + timedelta(seconds=30))
        adapter = FakeRefreshAdapter()
        self.assertEqual(token_getter(conn, adapter)(False), 'access-2')
        conn.refresh_from_db()
        self.assertEqual(decrypt_secret(conn.refresh_token), 'refresh-2')
        self.assertTrue(conn.refresh_token.startswith('enc:'))
        self.assertEqual(adapter.calls, 1)

    def test_refused_refresh_needs_reauth(self):
        from core.accounting.tokens import token_getter
        conn = make_connection(self.co, access_token_expires_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(AuthError):
            token_getter(conn, FakeRefreshAdapter(fail=True))(False)
        conn.refresh_from_db()
        self.assertEqual(conn.status, 'NEEDS_REAUTH')


PG_ONLY = skipIf(db_connection.vendor != 'postgresql', 'needs real row locks (Postgres)')


@PG_ONLY
class TokenRefreshConcurrencyTests(TransactionTestCase):
    """Two workers with an expired token: only one may spend the refresh
    token (Xero rotates it); the other uses the result."""

    def test_one_refresh_for_concurrent_workers(self):
        from django.db import connection as conn_db
        from core.accounting.tokens import token_getter
        co = Company.objects.create(company_name='Race Token')
        conn = make_connection(co, access_token_expires_at=timezone.now() - timedelta(minutes=1))
        adapter = FakeRefreshAdapter()
        barrier = threading.Barrier(3)
        results, errors = [], []

        def worker():
            try:
                c = AccountingConnection.objects.get(pk=conn.pk)
                barrier.wait()
                results.append(token_getter(c, adapter)(False))
            except Exception as e:
                errors.append(e)
            finally:
                conn_db.close()
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(set(results), {'access-2'})


# ---------------------------------------------------------------- revenue types

class RevenueTypeTests(TestCase):
    def test_generated_descriptions_are_typed(self):
        from core.revenue_types import type_for_generated_description as t
        self.assertEqual(t('Fuel Surcharge (812 km)'), 'FUEL_SURCHARGE')
        self.assertEqual(t('Toll Charges'), 'TOLLS')
        self.assertEqual(t('Extra Distance (40 km)'), 'EXTRA_KM')
        self.assertEqual(t('Pallet handling'), 'FREIGHT')

    def test_lines_and_credit_notes_carry_the_type(self):
        from core.services.credit_notes import create_credit_note
        co = Company.objects.create(company_name='Typed Haulage')
        user = make_user('typed_admin', co)
        cust = Customer.objects.create(company=co, name='T', email='t@typed.test', credit_score=80)
        from core.services.invoice_lines import apply_lines, LineError
        from core.services.numbering import provisional_number
        inv = Invoice(company=co, customer=cust, invoice_number=provisional_number(), issue_date=date(2026, 9, 1),
                      due_date=date(2026, 10, 1), status='DRAFT', subtotal=0, total_amount=0, balance=0)
        apply_lines(inv, [{'description': 'Freight', 'unit_price': '100', 'tax_code': 'STANDARD'},
                          {'description': 'Fuel', 'unit_price': '20', 'tax_code': 'STANDARD',
                           'revenue_type': 'FUEL_SURCHARGE'}])
        self.assertEqual([l.revenue_type for l in inv.lines.order_by('position')], ['FREIGHT', 'FUEL_SURCHARGE'])
        with self.assertRaises(LineError):
            apply_lines(inv, [{'description': 'x', 'unit_price': '1', 'revenue_type': 'BOGUS'}])
        inv.mark_as_sent()
        cn = create_credit_note(inv, user=user, reason='test', full=True, issue_date=date(2026, 9, 2))
        self.assertEqual(sorted(l.revenue_type for l in cn.lines.all()), ['FREIGHT', 'FUEL_SURCHARGE'])
