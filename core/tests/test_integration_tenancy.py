"""Fleet / TMS integration endpoints are scoped to the API key's company.

Company A's key must never read or modify company B's loads, vehicles,
drivers or customers, on any inbound integration endpoint; a real
IntegrationAPIKey works (it used to be rejected: only DEBUG demo keys passed
the old `isinstance(request.user, dict)` check); demo keys work only in DEBUG
and only for settings.FLEET_DEMO_COMPANY_ID.
"""
import hashlib
import hmac
import json
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import ActivityEvent, Customer, Driver, Load, Vehicle, VehicleType, WebhookSubscription
from core.tests.trip_fixtures import make_company, make_customer, make_key, make_load, make_user

SYNC = '/api/v1/integrations/fleet/sync/'
BULK = '/api/v1/integrations/fleet/sync/bulk/'
TRIPS = '/api/v1/integrations/trips/sync/'


class _Tenants(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co_a = make_company('Tenancy A')
        cls.co_b = make_company('Tenancy B')
        cls.user_a = make_user('ten_a', cls.co_a)
        cls.user_b = make_user('ten_b', cls.co_b)
        # B's customer is created FIRST so Customer.objects.first() would be B's.
        cls.cust_b = make_customer(cls.co_b, 'b')
        cls.cust_a = make_customer(cls.co_a, 'a')
        cls.key_a = make_key(cls.user_a, 'KEY-A')
        cls.key_b = make_key(cls.user_b, 'KEY-B')
        cls.load_a = make_load(cls.co_a, cls.cust_a, 'TEN-A-1')
        cls.load_b = make_load(cls.co_b, cls.cust_b, 'TEN-B-1')
        vt = VehicleType.objects.create(name='TenTruck', capacity=Decimal('30'), max_distance=Decimal('2000'),
                                        base_rate=Decimal('15'))
        cls.vehicle_b = Vehicle.objects.create(
            company=cls.co_b, vin='TENVINB', plate='BB 11 GP', vehicle_type=vt, make='M', model='A', year=2020,
            type='Truck', capacity=Decimal('30000'), fuel_type='Diesel', status='AVAILABLE')
        cls.vehicle_a = Vehicle.objects.create(
            company=cls.co_a, vin='TENVINA', plate='AA 11 GP', vehicle_type=vt, make='M', model='A', year=2020,
            type='Truck', capacity=Decimal('30000'), fuel_type='Diesel', status='AVAILABLE')
        from django.contrib.auth import get_user_model
        du = get_user_model().objects.create_user(username='ten_drv_b', email='d@b.test', password='x')
        cls.driver_b = Driver.objects.create(
            company=cls.co_b, user=du, license_number='TEN-LIC-B', license_expiry=date.today() + timedelta(days=300),
            license_state='GP', hire_date=date.today() - timedelta(days=300))

    def post(self, url, body, key='KEY-A', **headers):
        return APIClient().post(url, body, format='json', HTTP_X_API_KEY=key, **headers)


class FleetSyncTenancyTests(_Tenants):
    def test_real_integration_key_works_on_own_load(self):
        r = self.post(SYNC, {'load_number': 'TEN-A-1', 'status': 'IN_TRANSIT'})
        self.assertEqual(r.status_code, 200, r.content)
        self.load_a.refresh_from_db()
        self.assertEqual(self.load_a.status, 'IN_TRANSIT')

    def test_key_a_cannot_update_company_b_load(self):
        r = self.post(SYNC, {'load_number': 'TEN-B-1', 'status': 'DELIVERED', 'notes': 'hijack'})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn('TEN-B-1', json.dumps(r.json()).replace('Load TEN-B-1 not found', ''))
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')
        self.assertNotEqual(self.load_b.notes, 'hijack')

    def test_key_a_complete_action_cannot_touch_b(self):
        r = self.post(SYNC, {'action': 'complete', 'load_number': 'TEN-B-1'})
        self.assertEqual(r.status_code, 404)
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')

    def test_create_with_b_number_is_refused_not_updating_b(self):
        r = self.post(SYNC, {'action': 'create', 'load_number': 'TEN-B-1', 'status': 'IN_TRANSIT'})
        self.assertEqual(r.status_code, 409)
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')

    def test_create_uses_own_company_and_own_customer(self):
        r = self.post(SYNC, {'action': 'create', 'load_number': 'TEN-A-NEW', 'pickup_location': 'Durban',
                             'delivery_location': 'Johannesburg', 'total_amount': '12000'})
        self.assertEqual(r.status_code, 200, r.content)
        load = Load.objects.get(load_number='TEN-A-NEW')
        self.assertEqual(load.company_id, self.co_a.id)
        self.assertEqual(load.customer.company_id, self.co_a.id)
        self.assertNotEqual(load.customer_id, self.cust_b.id)

    def test_create_with_b_customer_id_is_refused(self):
        r = self.post(SYNC, {'action': 'create', 'load_number': 'TEN-A-C', 'customer_id': self.cust_b.id})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Load.objects.filter(load_number='TEN-A-C').exists())

    def test_b_vehicle_and_driver_never_assigned_to_a_load(self):
        r = self.post(SYNC, {'load_number': 'TEN-A-1', 'vehicle_plate': 'BB 11 GP', 'driver_id': self.driver_b.id})
        self.assertEqual(r.status_code, 200)
        self.load_a.refresh_from_db()
        self.assertIsNone(self.load_a.vehicle_id)
        self.assertIsNone(self.load_a.driver_id)
        r = self.post(SYNC, {'load_number': 'TEN-A-1', 'vehicle_plate': 'aa11gp'})
        self.load_a.refresh_from_db()
        self.assertEqual(self.load_a.vehicle_id, self.vehicle_a.id)

    def test_bulk_is_scoped_too(self):
        r = self.post(BULK, {'trips': [
            {'load_number': 'TEN-B-1', 'status': 'DELIVERED'},
            {'load_number': 'TEN-A-1', 'status': 'LOADING'},
            {'action': 'create', 'load_number': 'TEN-B-1'},
        ]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body['updated'], body['created'], len(body['errors'])), (1, 0, 2))
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')

    def test_lender_key_cannot_sync(self):
        make_key(self.user_a, 'KEY-LENDER', key_type='LENDER')
        self.assertEqual(self.post(SYNC, {'load_number': 'TEN-A-1'}, key='KEY-LENDER').status_code, 401)

    def test_key_without_company_is_refused(self):
        lone = make_user('ten_lone', None)
        make_key(lone, 'KEY-LONE')
        self.assertEqual(self.post(SYNC, {'load_number': 'TEN-A-1'}, key='KEY-LONE').status_code, 401)
        self.assertEqual(self.post(TRIPS, [{'origin': 'a', 'destination': 'b'}], key='KEY-LONE').status_code, 401)

    def test_inactive_and_unknown_keys_refused(self):
        self.key_a.active = False
        self.key_a.save()
        self.assertEqual(self.post(SYNC, {'load_number': 'TEN-A-1'}).status_code, 401)
        self.assertEqual(self.post(SYNC, {'load_number': 'TEN-A-1'}, key='nope').status_code, 401)

    def test_jwt_user_without_key_cannot_use_sync(self):
        c = APIClient()
        c.force_authenticate(self.user_a)
        self.assertIn(c.post(SYNC, {'load_number': 'TEN-A-1'}, format='json').status_code, (401, 403))

    def test_demo_key_refused_outside_debug(self):
        self.assertEqual(self.post(SYNC, {'load_number': 'TEN-A-1'}, key='fleet_demo_key_123').status_code, 401)

    def test_demo_key_in_debug_is_bound_to_demo_company_only(self):
        with override_settings(DEBUG=True, FLEET_DEMO_COMPANY_ID=None):
            self.assertEqual(self.post(SYNC, {'load_number': 'TEN-A-1'}, key='fleet_demo_key_123').status_code, 401)
        with override_settings(DEBUG=True, FLEET_DEMO_COMPANY_ID=self.co_a.id):
            ok = self.post(SYNC, {'load_number': 'TEN-A-1', 'status': 'LOADING'}, key='fleet_demo_key_123')
            self.assertEqual(ok.status_code, 200, ok.content)
            self.assertEqual(self.post(SYNC, {'load_number': 'TEN-B-1', 'status': 'LOADING'},
                                       key='fleet_demo_key_123').status_code, 404)
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')

    def test_quota_exceeded_is_429_on_trip_sync(self):
        self.key_a.monthly_quota = 1
        self.key_a.save()
        self.post(TRIPS, [])
        self.assertEqual(self.post(TRIPS, []).status_code, 429)


class TripSyncTenancyTests(_Tenants):
    def test_b_external_id_does_not_dedupe_a_record(self):
        Load.objects.filter(pk=self.load_b.pk).update(notes='ext_id:TMS-1')
        r = self.post(TRIPS, [{'external_id': 'TMS-1', 'origin': 'Durban', 'destination': 'Pretoria',
                               'customer_email': 'b@cust.test', 'rate': 5000}])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['created'], 1)
        load = Load.objects.get(pk=r.json()['load_ids'][0])
        self.assertEqual(load.company_id, self.co_a.id)
        # B's customer email creates A's own customer, never re-uses B's.
        self.assertEqual(load.customer.company_id, self.co_a.id)
        self.assertNotEqual(load.customer_id, self.cust_b.id)
        self.assertEqual(Customer.objects.filter(email='b@cust.test').count(), 2)


def signed_post(url, body, sub, ts=None, secret=None):
    """POST signed with the timestamped scheme (X-Fleet-Timestamp)."""
    import time
    raw = json.dumps(body).encode()
    ts = int(time.time()) if ts is None else ts
    sig = 'sha256=' + hmac.new((secret or sub.secret).encode(), f'{ts}.'.encode() + raw, hashlib.sha256).hexdigest()
    return APIClient().post(url, raw, content_type='application/json', HTTP_X_API_KEY=sub.api_key,
                            HTTP_X_FLEET_SIGNATURE=sig, HTTP_X_FLEET_TIMESTAMP=str(ts))


@override_settings(CTRLFLEET_WEBHOOK_KEY='')
class FleetWebhookTenancyTests(_Tenants):
    def _signed(self, url, body, sub):
        raw = json.dumps(body).encode()
        sig = 'sha256=' + hmac.new(sub.secret.encode(), raw, hashlib.sha256).hexdigest()
        return APIClient().post(url, raw, content_type='application/json', HTTP_X_API_KEY=sub.api_key,
                                HTTP_X_FLEET_SIGNATURE=sig)

    def test_trip_update_webhook_scoped_to_subscription_company(self):
        sub = WebhookSubscription.objects.create(partner_name='Fleet A', webhook_url='https://a.example',
                                                 company=self.co_a)
        body = {'load_number': 'TEN-B-1', 'event_type': 'delivery_confirmed'}
        self.assertEqual(signed_post('/api/v1/fleet/webhooks/trip-update/', body, sub).status_code, 404)
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')
        body = {'load_id': self.load_b.id, 'event_type': 'delivery_confirmed'}
        self.assertEqual(signed_post('/api/v1/fleet/webhooks/trip-update/', body, sub).status_code, 404)
        ok = signed_post('/api/v1/fleet/webhooks/trip-update/', {'load_number': 'TEN-A-1', 'event_type': 'gps_update'}, sub)
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertTrue(ActivityEvent.objects.filter(entity_id=self.load_a.id, company=self.co_a,
                                                     title__startswith='Fleet update').exists())

    def test_subscription_without_company_is_refused(self):
        sub = WebhookSubscription.objects.create(partner_name='Lender', webhook_url='https://l.example')
        r = signed_post('/api/v1/fleet/webhooks/trip-update/', {'load_number': 'TEN-A-1', 'event_type': 'x'}, sub)
        self.assertEqual(r.status_code, 403)
        r = signed_post('/api/v1/fleet/webhooks/vehicle-event/', {'vehicle_id': self.vehicle_a.id,
                        'status': 'BROKEN'}, sub)
        self.assertEqual(r.status_code, 403)

    def test_vehicle_and_driver_webhooks_scoped(self):
        sub = WebhookSubscription.objects.create(partner_name='Fleet A', webhook_url='https://a.example',
                                                 company=self.co_a)
        r = signed_post('/api/v1/fleet/webhooks/vehicle-event/', {'vehicle_id': self.vehicle_b.id,
                        'status': 'MAINTENANCE', 'event_type': 'breakdown'}, sub)
        self.assertEqual(r.status_code, 404)
        self.vehicle_b.refresh_from_db()
        self.assertEqual(self.vehicle_b.status, 'AVAILABLE')
        r = signed_post('/api/v1/fleet/webhooks/driver-event/', {'driver_id': self.driver_b.id,
                        'violation_count': 9}, sub)
        self.assertEqual(r.status_code, 404)

    def test_outbound_fleet_sync_scoped_to_user_company(self):
        c = APIClient()
        c.force_authenticate(self.user_a)
        r = c.post('/api/v1/fleet/trips/sync/', {'load_ids': [self.load_b.id, self.load_a.id]}, format='json')
        res = {x['load_id']: x['status'] for x in r.json()['results']}
        self.assertEqual(res, {self.load_b.id: 'error', self.load_a.id: 'success'})
        r = c.post('/api/v1/fleet/bookings/sync/', {'booking_ids': [self.load_b.id]}, format='json')
        self.assertEqual(r.json()['results'][0]['status'], 'error')


class CtrlFleetWebhookTenancyTests(_Tenants):
    URL = '/api/v1/fleet/webhooks/ctrlfleet/'

    def _post(self, body, **headers):
        return APIClient().post(self.URL, body, format='json', **headers)

    @override_settings(CTRLFLEET_WEBHOOK_KEY='shared')
    def test_shared_key_alone_names_no_company(self):
        r = self._post({'event_type': 'started', 'load_number': 'TEN-B-1'}, HTTP_X_CTRLFLEET_KEY='shared')
        self.assertEqual(r.status_code, 401)
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')

    @override_settings(CTRLFLEET_WEBHOOK_KEY='shared')
    def test_company_key_plus_shared_key_scoped(self):
        h = {'HTTP_X_CTRLFLEET_KEY': 'shared', 'HTTP_X_API_KEY': 'KEY-A'}
        self.assertEqual(self._post({'event_type': 'started', 'load_number': 'TEN-B-1'}, **h).status_code, 400)
        self.load_b.refresh_from_db()
        self.assertEqual(self.load_b.status, 'PENDING')
        ok = self._post({'event_type': 'started', 'load_number': 'TEN-A-1'}, **h)
        self.assertEqual(ok.status_code, 200, ok.content)
        self.load_a.refresh_from_db()
        self.assertEqual(self.load_a.status, 'IN_TRANSIT')
        bad = self._post({'event_type': 'started', 'load_number': 'TEN-A-1'},
                         HTTP_X_CTRLFLEET_KEY='wrong', HTTP_X_API_KEY='KEY-A')
        self.assertEqual(bad.status_code, 401)

    @override_settings(CTRLFLEET_WEBHOOK_KEY='')
    def test_vehicle_event_scoped(self):
        r = self._post({'event_category': 'vehicle', 'vehicle_id': self.vehicle_b.id, 'status': 'BREAKDOWN'},
                       HTTP_X_API_KEY='KEY-A')
        self.assertEqual(r.status_code, 400)
        self.vehicle_b.refresh_from_db()
        self.assertEqual(self.vehicle_b.status, 'AVAILABLE')


@override_settings(CTRLFLEET_WEBHOOK_KEY='')
class FleetWebhookSignatureTests(_Tenants):
    URLS = ('/api/v1/fleet/webhooks/vehicle-event/', '/api/v1/fleet/webhooks/driver-event/')

    def setUp(self):
        self.sub = WebhookSubscription.objects.create(partner_name='Fleet A', webhook_url='https://a.example',
                                                      company=self.co_a)

    def test_unsigned_wrong_secret_and_stale_are_refused(self):
        import time
        for url in self.URLS:
            body = {'vehicle_id': self.vehicle_a.id, 'status': 'MAINTENANCE'}
            self.assertEqual(APIClient().post(url, body, format='json', HTTP_X_API_KEY=self.sub.api_key)
                             .status_code, 401)
            self.assertEqual(signed_post(url, body, self.sub, secret='wrong').status_code, 401)
            self.assertEqual(signed_post(url, body, self.sub, ts=int(time.time()) - 3600).status_code, 401)
        self.vehicle_a.refresh_from_db()
        self.assertEqual(self.vehicle_a.status, 'AVAILABLE')

    def test_valid_signature_once_replay_refused(self):
        import time
        ts = int(time.time())
        body = {'vehicle_id': self.vehicle_a.id, 'status': 'MAINTENANCE', 'event_type': 'maintenance'}
        ok = signed_post(self.URLS[0], body, self.sub, ts=ts)
        self.assertEqual(ok.status_code, 200, ok.content)
        self.vehicle_a.refresh_from_db()
        self.assertEqual(self.vehicle_a.status, 'MAINTENANCE')
        replay = signed_post(self.URLS[0], body, self.sub, ts=ts)
        self.assertEqual(replay.status_code, 401)
        self.assertIn('already used', replay.json()['error'])

    def test_trip_update_accepts_timestamped_scheme(self):
        r = signed_post('/api/v1/fleet/webhooks/trip-update/', {'load_number': 'TEN-A-1', 'event_type': 'gps_update'},
                        self.sub)
        self.assertEqual(r.status_code, 200, r.content)


class BindWebhookSubscriptionsCommandTests(_Tenants):
    def _run(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('bind_webhook_subscriptions', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_then_apply_binds_only_unambiguous(self):
        by_key = WebhookSubscription.objects.create(partner_name='key KEY-A', webhook_url='https://k.example')
        by_name = WebhookSubscription.objects.create(partner_name='tenancy b', webhook_url='https://b.example')
        lost = WebhookSubscription.objects.create(partner_name='Nobody', webhook_url='https://n.example')
        out = self._run()
        self.assertIn('WOULD BIND', out)
        self.assertIn('UNRESOLVED #%d' % lost.pk, out)
        self.assertFalse(WebhookSubscription.objects.filter(company__isnull=False).exists())
        self._run('--apply')
        by_key.refresh_from_db(); by_name.refresh_from_db(); lost.refresh_from_db()
        self.assertEqual((by_key.company_id, by_name.company_id, lost.company_id), (self.co_a.id, self.co_b.id, None))
        self._run('--bind', f'{lost.pk}={self.co_a.pk}', '--apply')
        lost.refresh_from_db()
        self.assertEqual(lost.company_id, self.co_a.id)


@override_settings(CTRLFLEET_WEBHOOK_KEY='')
class LegacySignatureTests(_Tenants):
    URL = '/api/v1/fleet/webhooks/trip-update/'

    def _legacy(self, sub, body):
        raw = json.dumps(body).encode()
        sig = 'sha256=' + hmac.new(sub.secret.encode(), raw, hashlib.sha256).hexdigest()
        return APIClient().post(self.URL, raw, content_type='application/json', HTTP_X_API_KEY=sub.api_key,
                                HTTP_X_FLEET_SIGNATURE=sig)

    def test_body_only_signature_refused_unless_opted_in_and_never_replayed(self):
        sub = WebhookSubscription.objects.create(partner_name='Fleet A', webhook_url='https://a.example',
                                                 company=self.co_a)
        body = {'load_number': 'TEN-A-1', 'event_type': 'gps_update'}
        self.assertEqual(self._legacy(sub, body).status_code, 401)
        sub.allow_legacy_signature = True
        sub.save()
        self.assertEqual(self._legacy(sub, body).status_code, 200)
        replay = self._legacy(sub, body)
        self.assertEqual(replay.status_code, 401)
        self.assertIn('already used', replay.json()['error'])

    def test_bad_ids_and_dates_are_400_not_500(self):
        sub = WebhookSubscription.objects.create(partner_name='Fleet A', webhook_url='https://a.example',
                                                 company=self.co_a)
        self.assertEqual(signed_post(self.URL, {'load_id': 'abc', 'event_type': 'gps_update'}, sub).status_code, 400)
        r = signed_post('/api/v1/fleet/webhooks/vehicle-event/', {'vehicle_id': self.vehicle_a.id,
                        'maintenance_due': 'soon'}, sub)
        self.assertEqual(r.status_code, 400)
        r = signed_post('/api/v1/fleet/webhooks/driver-event/', {'driver_id': 'x1'}, sub)
        self.assertEqual(r.status_code, 400)
        with override_settings(CTRLFLEET_WEBHOOK_KEY=''):
            r = APIClient().post('/api/v1/fleet/webhooks/ctrlfleet/', {'event_category': 'vehicle', 'vehicle_id': 'abc'},
                                 format='json', HTTP_X_API_KEY='KEY-A')
            self.assertEqual(r.status_code, 400)


@override_settings(CTRLFLEET_WEBHOOK_KEY='')
class RoundThreeWebhookTests(_Tenants):
    URL = '/api/v1/fleet/webhooks/trip-update/'

    def setUp(self):
        self.sub = WebhookSubscription.objects.create(partner_name='Fleet A', webhook_url='https://a.example',
                                                      company=self.co_a)

    def test_delivery_confirmed_never_revives_a_cancelled_job(self):
        from core.models import Invoice
        Load.objects.filter(pk=self.load_a.pk).update(status='CANCELLED')
        r = signed_post(self.URL, {'load_id': self.load_a.id, 'event_type': 'delivery_confirmed'}, self.sub)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['status_refused']['code'], 'cancelled_in_truckwys')
        self.load_a.refresh_from_db()
        self.assertEqual(self.load_a.status, 'CANCELLED')
        self.assertFalse(Invoice.objects.filter(load=self.load_a).exists())

    def test_delivery_confirmed_on_invoiced_is_a_no_op(self):
        Load.objects.filter(pk=self.load_a.pk).update(status='INVOICED')
        r = signed_post(self.URL, {'load_id': self.load_a.id, 'event_type': 'delivery_confirmed'}, self.sub)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('status_refused', r.json())
        self.load_a.refresh_from_db()
        self.assertEqual(self.load_a.status, 'INVOICED')

    def test_string_pod_data_is_400(self):
        r = signed_post(self.URL, {'load_id': self.load_a.id, 'event_type': 'delivery_confirmed',
                                   'pod_data': 'signed'}, self.sub)
        self.assertEqual(r.status_code, 400)

    def test_vehicle_mileage_validated(self):
        r = signed_post('/api/v1/fleet/webhooks/vehicle-event/', {'vehicle_id': self.vehicle_a.id,
                        'mileage': 'lots'}, self.sub)
        self.assertEqual(r.status_code, 400)

    def test_replay_store_survives_cache_culling_and_503_when_down(self):
        from django.core.cache import cache
        from unittest import mock as _m
        body = {'load_number': 'TEN-A-1', 'event_type': 'gps_update'}
        import time as _t
        ts = int(_t.time())
        self.assertEqual(signed_post(self.URL, body, self.sub, ts=ts).status_code, 200)
        cache.clear()                                   # the cache culled / churned
        self.assertEqual(signed_post(self.URL, body, self.sub, ts=ts).status_code, 401)
        from django.db import DatabaseError
        with _m.patch('core.models.UsedWebhookSignature.objects.create', side_effect=DatabaseError('down')):
            self.assertEqual(signed_post(self.URL, body, self.sub).status_code, 503)

    def test_ctrlfleet_completed_never_revives_cancelled(self):
        Load.objects.filter(pk=self.load_a.pk).update(status='CANCELLED')
        r = APIClient().post('/api/v1/fleet/webhooks/ctrlfleet/', {'event_type': 'completed',
                             'load_number': 'TEN-A-1'}, format='json', HTTP_X_API_KEY='KEY-A')
        self.assertEqual(r.status_code, 200, r.content)
        self.load_a.refresh_from_db()
        self.assertEqual(self.load_a.status, 'CANCELLED')


@override_settings(CTRLFLEET_WEBHOOK_KEY='')
class FinalCheckWebhookTests(_Tenants):
    def test_mileage_bounded(self):
        sub = WebhookSubscription.objects.create(partner_name='Fleet A', webhook_url='https://a.example',
                                                 company=self.co_a)
        url = '/api/v1/fleet/webhooks/vehicle-event/'
        for bad in ('1e12', -5, 100_000_000):
            self.assertEqual(signed_post(url, {'vehicle_id': self.vehicle_a.id, 'mileage': bad}, sub).status_code,
                             400, bad)
        ok = signed_post(url, {'vehicle_id': self.vehicle_a.id, 'mileage': 125000}, sub)
        self.assertEqual(ok.status_code, 200, ok.content)
        self.vehicle_a.refresh_from_db()
        self.assertEqual(self.vehicle_a.mileage, Decimal('125000'))
        r = APIClient().post('/api/v1/fleet/webhooks/ctrlfleet/', {'event_category': 'vehicle',
                             'vehicle_id': self.vehicle_a.id, 'mileage': '1e12'}, format='json', HTTP_X_API_KEY='KEY-A')
        self.assertEqual(r.status_code, 400)
