"""Outbound webhooks are tenant-scoped, delivered after commit, never in the request.

Regression for the cross-tenant leak (Oct 2026): every active
WebhookSubscription used to receive every company's loads, invoices and quote
acceptances, synchronously, inside the saving request.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from io import StringIO
from unittest import mock

from celery.exceptions import Retry
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Load, User, Webhook, WebhookSubscription
from core.services import webhook_delivery as wd

ALL_EVENTS = ['load.created', 'load.status_changed', 'load.delivered', 'invoice.created',
              'invoice.paid', 'quote.accepted', 'webhook.test']

_n = 0


def _load(company, **kw):
    global _n
    _n += 1
    cust = Customer.objects.create(
        company=company, name=f'WH Cust {_n}', email=f'whc{_n}@wh.test', phone='', address='',
        city='JHB', state='', zip_code='', credit_score=85, credit_score_source='MANUAL',
    )
    now = timezone.now()
    fields = dict(
        company=company, load_number=f'LOAD-WH-{_n}', customer=cust,
        pickup_location='a', pickup_city='JHB', pickup_state='GP', pickup_zip='1',
        pickup_date=now, delivery_location='b', delivery_city='DBN', delivery_state='KZN',
        delivery_zip='2', delivery_date=now, cargo_description='x', weight=Decimal('1000'),
        rate=Decimal('1000'), total_amount=Decimal('1000'), status='PENDING',
    )
    fields.update(kw)
    return Load.objects.create(**fields)


def _http_forbidden(*a, **k):
    raise AssertionError('synchronous webhook HTTP call')


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Company.objects.create(company_name='WH Alpha')
        cls.b = Company.objects.create(company_name='WH Beta')
        cls.sub_a = WebhookSubscription.objects.create(
            partner_name='Alpha Partner', webhook_url='https://a.example.com/hook',
            events=ALL_EVENTS, company=cls.a)
        cls.sub_b = WebhookSubscription.objects.create(
            partner_name='Beta Partner', webhook_url='https://b.example.com/hook',
            events=ALL_EVENTS, company=cls.b)
        cls.sub_none = WebhookSubscription.objects.create(
            partner_name='Unknown Partner', webhook_url='https://evil.example.com/hook',
            events=ALL_EVENTS, company=None)

    def setUp(self):
        super().setUp()
        # No real DNS in tests: every host resolves to a public address
        # unless a test says otherwise.
        p = mock.patch('core.services.webhook_url.resolve_ips', return_value=['93.184.216.34'])
        self.resolve = p.start()
        self.addCleanup(p.stop)

    def queued(self, enqueue):
        """(kind, target_id, event_type, company_id) for every apply_async call."""
        out = []
        for c in enqueue.call_args_list:
            kind, target_id, event_type, _body, company_id = c.kwargs['args']
            out.append((kind, target_id, event_type, company_id))
        return out


@mock.patch('core.services.webhook_delivery._post', side_effect=_http_forbidden)
@mock.patch('core.tasks.deliver_webhook.apply_async')
class TenantScopeTests(_Base):

    def test_company_a_load_reaches_only_company_a(self, enqueue, _post):
        with self.captureOnCommitCallbacks(execute=True):
            _load(self.a)
        self.assertEqual(self.queued(enqueue), [(wd.SUBSCRIPTION, self.sub_a.id, 'load.created', self.a.id)])

    def test_company_b_never_receives_company_a_events(self, enqueue, _post):
        with self.captureOnCommitCallbacks(execute=True):
            ld = _load(self.a)
            ld.status = 'DELIVERED'
            ld.save()
        targets = {q[1] for q in self.queued(enqueue)}
        self.assertEqual(targets, {self.sub_a.id})
        self.assertNotIn(self.sub_b.id, targets)
        self.assertNotIn(self.sub_none.id, targets)

    def test_companyless_subscription_gets_nothing(self, enqueue, _post):
        self.assertEqual(wd.resolve_targets('load.created', self.a.id), [(wd.SUBSCRIPTION, self.sub_a.id)])
        for company in (self.a, self.b):
            self.assertNotIn((wd.SUBSCRIPTION, self.sub_none.id), wd.resolve_targets('load.created', company.id))

    def test_event_without_company_goes_nowhere(self, enqueue, _post):
        with self.captureOnCommitCallbacks(execute=True):
            _load(None)
        enqueue.assert_not_called()
        self.assertEqual(wd.resolve_targets('load.created', None), [])

    def test_legacy_webhook_scoped_by_operator_company(self, enqueue, _post):
        ub = User.objects.create_user(username='wh_b', email='wh_b@wh.test', password='x')
        ub.company = self.b
        ub.save()
        nobody = User.objects.create_user(username='wh_none', email='wh_none@wh.test', password='x')
        hook_b = Webhook.objects.create(operator=ub, url='https://legacy-b.example.com', events=ALL_EVENTS)
        Webhook.objects.create(operator=nobody, url='https://legacy-none.example.com', events=ALL_EVENTS)
        with self.captureOnCommitCallbacks(execute=True):
            _load(self.a)
        self.assertNotIn(wd.LEGACY, {q[0] for q in self.queued(enqueue)})
        enqueue.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            _load(self.b)
        self.assertCountEqual(self.queued(enqueue), [
            (wd.SUBSCRIPTION, self.sub_b.id, 'load.created', self.b.id),
            (wd.LEGACY, hook_b.id, 'load.created', self.b.id),
        ])

    def test_status_changed_only_on_real_change(self, enqueue, _post):
        with self.captureOnCommitCallbacks(execute=True):
            ld = _load(self.a)
        enqueue.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            ld.cargo_description = 'edited'
            ld.save()
            ld.save()
        enqueue.assert_not_called()
        with self.captureOnCommitCallbacks(execute=True):
            ld.status = 'ASSIGNED'
            ld.save()
        self.assertEqual([q[2] for q in self.queued(enqueue)], ['load.status_changed'])

    def test_load_delivered_once_with_auto_invoice(self, enqueue, _post):
        # Auto-invoice moves the row to INVOICED; re-saving the same object
        # must not write DELIVERED back or re-fire load.delivered.
        with self.captureOnCommitCallbacks(execute=True):
            ld = _load(self.a)
            ld.status = 'DELIVERED'
            ld.save()
            ld.cargo_description = 'edited after delivery'
            ld.save()
        events = [q[2] for q in self.queued(enqueue)]
        self.assertEqual(events.count('load.delivered'), 1)
        self.assertEqual(Load.objects.get(pk=ld.pk).status, 'INVOICED')
        self.assertEqual(ld.status, 'INVOICED')

    @override_settings(AUTO_INVOICE_ON_DELIVERY=False)  # it moves the row to INVOICED behind the instance
    def test_load_delivered_once(self, enqueue, _post):
        with self.captureOnCommitCallbacks(execute=True):
            ld = _load(self.a)
            ld.status = 'DELIVERED'
            ld.save()
            ld.save()
        events = [q[2] for q in self.queued(enqueue)]
        self.assertEqual(events.count('load.delivered'), 1)


@mock.patch('core.services.webhook_delivery._post', side_effect=_http_forbidden)
@mock.patch('core.tasks.deliver_webhook.apply_async')
class AfterCommitTests(_Base):

    def test_nothing_queued_or_sent_before_commit(self, enqueue, _post):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            _load(self.a)
        enqueue.assert_not_called()      # still inside the transaction
        _post.assert_not_called()        # and no HTTP in the request path
        self.assertTrue(callbacks)
        for cb in callbacks:
            cb()
        self.assertEqual(len(enqueue.call_args_list), 1)
        _post.assert_not_called()        # the worker sends, not the request

    def test_rolled_back_save_sends_nothing(self, enqueue, _post):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    _load(self.a)
                    raise RuntimeError('rollback')
            except RuntimeError:
                pass
        enqueue.assert_not_called()

    def test_broker_down_never_breaks_the_save(self, enqueue, _post):
        enqueue.side_effect = ConnectionError('broker down')
        with self.captureOnCommitCallbacks(execute=True):
            ld = _load(self.a)
        self.assertTrue(Load.objects.filter(pk=ld.pk).exists())
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs.get('retry'), False)


class DeliveryTests(_Base):

    def _ok(self, code=200):
        return mock.Mock(status_code=code)

    @override_settings(WEBHOOK_DELIVERY_EAGER=True)
    def test_eager_mode_posts_once_after_commit_signed(self):
        with mock.patch('core.services.webhook_delivery._post', return_value=self._ok()) as post:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                _load(self.a)
            post.assert_not_called()
            for cb in callbacks:
                cb()
        post.assert_called_once()
        url, body, headers = post.call_args.args
        self.assertEqual(url, self.sub_a.webhook_url)
        self.assertEqual(json.loads(body)['event'], 'load.created')
        expected = 'sha256=' + hmac.new(self.sub_a.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Webhook-Signature'], expected)

    def test_http_call_has_timeout_and_no_redirects(self):
        with mock.patch('requests.Session.post', return_value=self._ok()) as rp:
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'load.created', '{}', self.a.id), wd.OK)
        self.assertEqual(rp.call_args.kwargs['timeout'], wd.HTTP_TIMEOUT)
        self.assertIs(rp.call_args.kwargs['allow_redirects'], False)

    def test_post_connects_to_the_checked_address_not_a_new_lookup(self):
        # DNS rebinding: the check sees a public IP; a second lookup at send
        # time would get a private one. The POST must go to the checked IP.
        self.resolve.side_effect = [['93.184.216.34'], ['169.254.169.254']]
        with mock.patch('requests.Session.post', return_value=self._ok()) as rp:
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'load.created', '{}', self.a.id), wd.OK)
        sent_to = rp.call_args.args[0]
        self.assertTrue(sent_to.startswith('https://93.184.216.34'), sent_to)
        from urllib.parse import urlparse
        self.assertEqual(rp.call_args.kwargs['headers']['Host'], urlparse(self.sub_a.webhook_url).hostname)
        self.assertEqual(self.resolve.call_count, 1)   # one lookup, no re-resolution

    def test_pinned_adapter_keeps_the_hostname_for_tls(self):
        adapter = wd._pinned_adapter('hooks.example.com')
        kw = adapter.poolmanager.connection_pool_kw
        self.assertEqual((kw['server_hostname'], kw['assert_hostname']), ('hooks.example.com', 'hooks.example.com'))

    def test_ipv6_tunnels_to_private_addresses_are_blocked(self):
        from core.services.webhook_url import BLOCKED, check_webhook_url
        for ip in ('64:ff9b::a00:1', '2002:a00:1::1'):
            self.resolve.return_value = [ip]
            self.assertEqual(check_webhook_url('https://tunnel.example.com/h'), BLOCKED, ip)

    def test_ownership_rechecked_at_send_time(self):
        WebhookSubscription.objects.filter(pk=self.sub_a.pk).update(company=self.b)
        with mock.patch('core.services.webhook_delivery._post') as post:
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'load.created', '{}', self.a.id), wd.SKIPPED)
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_none.id, 'load.created', '{}', None), wd.SKIPPED)
        post.assert_not_called()

    def test_bounded_retries_without_sleeping(self):
        from core.tasks import deliver_webhook
        with mock.patch('core.services.webhook_delivery._post', return_value=self._ok(503)), \
                mock.patch('time.sleep', side_effect=AssertionError('sleep')):
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'e', '{}', self.a.id, final=False), wd.RETRY)
            with self.assertRaises(Retry):
                deliver_webhook(wd.SUBSCRIPTION, self.sub_a.id, 'e', '{}', self.a.id)
            self.sub_a.refresh_from_db()
            self.assertEqual(self.sub_a.failure_count, 0)
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'e', '{}', self.a.id, final=True), wd.FAILED)
        self.sub_a.refresh_from_db()
        self.assertEqual(self.sub_a.failure_count, 1)
        self.assertEqual(deliver_webhook.max_retries, 3)

    def test_client_error_is_final(self):
        with mock.patch('core.services.webhook_delivery._post', return_value=self._ok(404)):
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'e', '{}', self.a.id, final=False), wd.FAILED)


@mock.patch('core.views_partner_api._is_safe_webhook_url', return_value=True)
class PartnerApiScopeTests(_Base):
    url = '/api/v1/partners/webhooks/'

    def client_for(self, sub):
        c = APIClient()
        c.credentials(HTTP_X_API_KEY=sub.api_key)
        return c

    def test_create_inherits_callers_company(self, _safe):
        r = self.client_for(self.sub_a).post(self.url, {
            'webhook_url': 'https://new.example.com/h', 'events': ['load.created'],
            'company': self.b.id, 'company_id': self.b.id,
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(WebhookSubscription.objects.get(pk=r.data['subscription_id']).company_id, self.a.id)

    def test_unbound_key_cannot_create(self, _safe):
        r = self.client_for(self.sub_none).post(self.url, {
            'webhook_url': 'https://new.example.com/h', 'events': ['load.created']}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_list_and_delete_are_company_scoped(self, _safe):
        r = self.client_for(self.sub_a).get(self.url)
        self.assertEqual([s['subscription_id'] for s in r.data['subscriptions']], [self.sub_a.id])
        r = self.client_for(self.sub_a).delete(f'{self.url}{self.sub_b.id}/')
        self.assertEqual(r.status_code, 404)
        self.assertTrue(WebhookSubscription.objects.filter(pk=self.sub_b.pk).exists())
        r = self.client_for(self.sub_none).get(self.url)
        self.assertEqual([s['subscription_id'] for s in r.data['subscriptions']], [self.sub_none.id])

    def test_other_partners_on_the_same_company_are_not_listed_or_deletable(self, _safe):
        # A lender and the fleet system both bound to company A: neither may
        # see or delete the other's subscription.
        fleet = WebhookSubscription.objects.create(partner_name='Fleet System', webhook_url='https://fleet.example.com/h',
                                                   events=ALL_EVENTS, company=self.a, fleet_write_enabled=True)
        r = self.client_for(self.sub_a).get(self.url)
        self.assertEqual([s['subscription_id'] for s in r.data['subscriptions']], [self.sub_a.id])
        r = self.client_for(self.sub_a).delete(f'{self.url}{fleet.id}/')
        self.assertEqual(r.status_code, 404)
        self.assertTrue(WebhookSubscription.objects.filter(pk=fleet.pk).exists())
        # Its own extra subscription is still its to see and delete.
        r = self.client_for(self.sub_a).post(self.url, {'webhook_url': 'https://second.example.com/h',
                                                        'events': ['load.created']}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        new_id = r.data['subscription_id']
        self.assertFalse(WebhookSubscription.objects.get(pk=new_id).fleet_write_enabled)   # never inherited
        listed = [s['subscription_id'] for s in self.client_for(self.sub_a).get(self.url).data['subscriptions']]
        self.assertCountEqual(listed, [self.sub_a.id, new_id])
        self.assertEqual(self.client_for(self.sub_a).delete(f'{self.url}{new_id}/').status_code, 204)

    def test_only_superusers_change_fleet_access_in_admin(self, _safe):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        ma = site._registry[WebhookSubscription]
        staff = User.objects.create_user(username='wh_staff', email='st@wh.test', password='x', is_staff=True)
        staff.company = self.a
        staff.save()
        req = RequestFactory().get('/')
        req.user = staff
        self.assertIn('fleet_write_enabled', ma.get_readonly_fields(req, self.sub_a))
        boss = User.objects.create_superuser(username='wh_boss', email='b@wh.test', password='x')
        req.user = boss
        self.assertNotIn('fleet_write_enabled', ma.get_readonly_fields(req, self.sub_a))


@mock.patch('core.tasks.deliver_webhook.apply_async')
class LegacyTestPingTests(_Base):

    def test_test_ping_targets_only_that_webhook(self, enqueue):
        admin_a = User.objects.create_user(username='wh_admin_a', email='wa@wh.test', password='x')
        admin_a.company, admin_a.role = self.a, 'ADMIN'
        admin_a.save()
        hook = Webhook.objects.create(operator=admin_a, url='https://legacy-a.example.com', events=ALL_EVENTS)
        c = APIClient()
        c.force_authenticate(admin_a)
        with self.captureOnCommitCallbacks(execute=True):
            r = c.post(f'/api/v1/webhooks/{hook.id}/test/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.queued(enqueue), [(wd.LEGACY, hook.id, 'webhook.test', self.a.id)])


class AuditCommandTests(_Base):

    def test_lists_and_flags_without_writing(self):
        before = list(WebhookSubscription.objects.values_list('id', 'company_id', 'is_active', 'updated_at'))
        out = StringIO()
        call_command('audit_webhook_subscriptions', stdout=out)
        text = out.getvalue()
        self.assertIn('https://evil.example.com/hook', text)
        self.assertIn('NO_COMPANY', text)
        self.assertIn('UNKNOWN_PARTNER', text)
        self.assertIn('WH Alpha', text)
        out = StringIO()
        call_command('audit_webhook_subscriptions', '--json', stdout=out)
        rows = {r['id']: r for r in json.loads(out.getvalue()) if r['kind'] == 'subscription'}
        self.assertEqual(rows[self.sub_none.id]['company_id'], None)
        self.assertEqual(rows[self.sub_a.id]['company_id'], self.a.id)
        after = list(WebhookSubscription.objects.values_list('id', 'company_id', 'is_active', 'updated_at'))
        self.assertEqual(before, after)


class SsrfTests(_Base):
    """Every place a webhook URL is accepted, and send time, use one rule."""

    BAD_IPS = ['10.0.0.5', '127.0.0.1', '169.254.169.254', '192.168.1.1', '172.16.0.1',
               '100.64.0.1', '0.0.0.0', '::1', 'fe80::1', '::ffff:127.0.0.1', 'fd00::1']

    def test_check_rejects_scheme_and_private_ips(self):
        from core.services.webhook_url import BLOCKED, UNRESOLVED, check_webhook_url
        self.assertIsNone(check_webhook_url('https://ok.example.com/h'))
        self.assertEqual(check_webhook_url('http://ok.example.com/h'), BLOCKED)
        self.assertEqual(check_webhook_url('https://user:pw@ok.example.com/h'), BLOCKED)
        self.assertEqual(check_webhook_url('ftp://ok.example.com'), BLOCKED)
        for ip in self.BAD_IPS:
            self.resolve.return_value = [ip]
            self.assertEqual(check_webhook_url('https://rebind.example.com/h'), BLOCKED, ip)
        self.resolve.return_value = ['93.184.216.34', '10.0.0.1']   # any private answer blocks
        self.assertEqual(check_webhook_url('https://mixed.example.com/h'), BLOCKED)
        import socket
        self.resolve.side_effect = socket.gaierror('nx')
        self.assertEqual(check_webhook_url('https://nx.example.com/h'), UNRESOLVED)

    def test_send_time_dns_rebinding_is_blocked(self):
        self.resolve.return_value = ['169.254.169.254']
        with mock.patch('core.services.webhook_delivery._post') as post:
            outcome = wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'load.created', '{}', self.a.id, final=False)
        self.assertEqual(outcome, wd.FAILED)
        post.assert_not_called()
        self.sub_a.refresh_from_db()
        self.assertEqual(self.sub_a.failure_count, 1)

    def test_send_time_dns_failure_retries_without_posting(self):
        import socket
        self.resolve.side_effect = socket.gaierror('nx')
        with mock.patch('core.services.webhook_delivery._post') as post:
            self.assertEqual(wd.attempt(wd.SUBSCRIPTION, self.sub_a.id, 'e', '{}', self.a.id, final=False), wd.RETRY)
        post.assert_not_called()

    def _admin(self):
        u = User.objects.create_user(username='wh_ssrf', email='ssrf@wh.test', password='x')
        u.company, u.role = self.a, 'ADMIN'
        u.save()
        c = APIClient()
        c.force_authenticate(u)
        return c

    def test_legacy_webhook_api_rejects_unsafe_urls(self):
        c = self._admin()
        r = c.post('/api/v1/webhooks/', {'url': 'http://ok.example.com/h', 'events': ['load.created']}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.resolve.return_value = ['127.0.0.1']
        r = c.post('/api/v1/webhooks/', {'url': 'https://internal.example.com/h', 'events': []}, format='json')
        self.assertEqual(r.status_code, 400, r.content)
        self.resolve.return_value = ['93.184.216.34']
        r = c.post('/api/v1/webhooks/', {'url': 'https://ok.example.com/h', 'events': []}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        hook_id = r.data['id']
        self.resolve.return_value = ['10.1.2.3']
        r = c.patch(f'/api/v1/webhooks/{hook_id}/', {'url': 'https://moved.example.com/h'}, format='json')
        self.assertEqual(r.status_code, 400, r.content)

    def test_integration_key_serializer_rejects_unsafe_webhook_url(self):
        from core.serializers import IntegrationAPIKeySerializer
        self.resolve.return_value = ['169.254.169.254']
        s = IntegrationAPIKeySerializer(data={'name': 'k', 'webhook_url': 'https://meta.example.com/h'})
        self.assertFalse(s.is_valid())
        self.assertIn('webhook_url', s.errors)
        self.resolve.return_value = ['93.184.216.34']
        s = IntegrationAPIKeySerializer(data={'name': 'k', 'webhook_url': 'https://ok.example.com/h'})
        s.is_valid()
        self.assertNotIn('webhook_url', s.errors)

    def test_partner_api_uses_same_rule(self):
        c = APIClient()
        c.credentials(HTTP_X_API_KEY=self.sub_a.api_key)
        self.resolve.return_value = ['192.168.0.10']
        r = c.post('/api/v1/partners/webhooks/', {'webhook_url': 'https://lan.example.com/h',
                                                  'events': ['load.created']}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_admin_form_rejects_unsafe_url(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        admin_obj = site._registry[WebhookSubscription]
        su = User.objects.create_superuser(username='wh_su', email='su@wh.test', password='x')
        req = RequestFactory().get('/')
        req.user = su
        Form = admin_obj.get_form(req)
        self.resolve.return_value = ['127.0.0.1']
        f = Form(data={'partner_name': 'X', 'webhook_url': 'https://lo.example.com/h', 'events': '[]',
                       'is_active': True, 'company': self.a.id})
        self.assertFalse(f.is_valid())
        self.assertIn('webhook_url', f.errors)
