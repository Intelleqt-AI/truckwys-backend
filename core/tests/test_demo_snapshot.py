"""The demo snapshot (core.services.demo_snapshot): built for the demo company
only, equal to the live API, served to the demo login only, and dropped once
anything in the demo changes."""
import gzip
import json
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from core.models import Company, Customer, Load, Quote, User
from core.services import demo_snapshot as ds
from core.services.demo_seed import DEMO_USER_EMAIL
from core.throttling import UserReadRateThrottle


def _company(name, demo):
    co = Company.objects.create(company_name=name, is_demo=demo, subscription_status='active')
    cust = Customer.objects.create(company=co, name=f'{name} Shipper', email=f'{name[:4].lower()}@s.test',
                                   phone='1', address='x', city='JHB', state='GP', zip_code='2000')
    now = timezone.now()
    for i in range(3):
        Load.objects.create(
            company=co, customer=cust, load_number=f'{name[:3].upper()}-LD-{i}',
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000', pickup_date=now,
            delivery_location='DBN', delivery_city='DBN', delivery_state='KZN', delivery_zip='4000',
            delivery_date=now + timedelta(days=1), cargo_description='x', weight=1, distance=1,
            rate=Decimal('1000'), total_amount=Decimal('1000'), status='PENDING')
    Quote.objects.create(
        company=co, customer=cust, quote_number=f'{name[:3].upper()}-Q-1', pickup_location='JHB',
        delivery_location='DBN', origin='JHB', destination='DBN', cargo_description='x', weight=Decimal('1000'),
        base_rate=Decimal('500'), fuel_surcharge=Decimal('0'), toll_charges=Decimal('0'),
        driver_allowance=Decimal('0'), additional_charges=Decimal('0'), total_amount=Decimal('500'),
        valid_until=timezone.localdate() + timedelta(days=7), status='DRAFT')
    return co


class DemoSnapshotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.demo = _company('Demo Fleet', True)
        cls.demo_user = User.objects.create_user(username=DEMO_USER_EMAIL, email=DEMO_USER_EMAIL, password='x',
                                                 company=cls.demo, role='ADMIN')
        cls.real = _company('Real Fleet', False)
        cls.real_user = User.objects.create_user(username='owner@real.test', email='owner@real.test', password='x',
                                                 company=cls.real, role='ADMIN')

    def setUp(self):
        cache.delete(ds.CACHE_KEY)

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def snapshot(self, user):
        r = self.client_for(user).get('/api/v1/demo/snapshot/')
        if r.status_code != 200:
            return r.status_code, None
        self.assertEqual(r['Content-Encoding'], 'gzip')
        return 200, json.loads(gzip.decompress(r.content))

    def test_built_for_the_demo_only_and_equal_to_the_live_api(self):
        n = ds.build_demo_snapshot()
        self.assertGreater(n, 20)
        status, body = self.snapshot(self.demo_user)
        self.assertEqual(status, 200)
        live = self.client_for(self.demo_user)
        for path, stored in body['responses'].items():
            fresh = live.get('/' + path).json()
            if path == 'api/v1/dashboard/signals/':   # stamps the time it was asked
                for s in fresh.get('signals', []) + stored.get('signals', []):
                    s.pop('created_at', None)
            self.assertEqual(stored, fresh, path)
        numbers = {r['load_number'] for r in body['responses']['api/v1/loads/?page_size=100']['results']}
        self.assertEqual(numbers, {'DEM-LD-0', 'DEM-LD-1', 'DEM-LD-2'})   # demo rows only

    def test_a_real_login_never_gets_it(self):
        ds.build_demo_snapshot()
        self.assertEqual(self.snapshot(self.real_user), (404, None))
        self.assertIsNone(ds.snapshot_for(self.real_user))

    def test_missing_until_built(self):
        self.assertEqual(self.snapshot(self.demo_user), (404, None))

    def test_a_change_in_the_demo_drops_it_until_the_next_build(self):
        ds.build_demo_snapshot()
        q = Quote.objects.get(company=self.demo)
        q.notes = 'a visitor edit'
        q.save()
        self.assertEqual(self.snapshot(self.demo_user), (404, None))
        ds.build_demo_snapshot()
        self.assertEqual(self.snapshot(self.demo_user)[0], 200)

    def test_a_change_in_another_company_does_not(self):
        ds.build_demo_snapshot()
        q = Quote.objects.get(company=self.real)
        q.notes = 'real work'
        q.save()
        self.assertEqual(self.snapshot(self.demo_user)[0], 200)

    def test_the_demo_task_puts_back_a_dropped_snapshot_only(self):
        from core.tasks import reset_demo_company_task
        Company.objects.filter(pk=self.demo.pk).update(demo_last_reset_at=timezone.now())
        reset_demo_company_task()                       # nothing to reset; none stored yet
        self.assertEqual(self.snapshot(self.demo_user)[0], 200)
        stored = cache.get(ds.CACHE_KEY)['built_at']
        reset_demo_company_task()                       # present: left alone
        self.assertEqual(cache.get(ds.CACHE_KEY)['built_at'], stored)

    def test_no_demo_company_builds_nothing(self):
        Company.objects.filter(pk=self.demo.pk).update(is_demo=False)
        self.assertEqual(ds.build_demo_snapshot(), 0)
        self.assertIsNone(cache.get(ds.CACHE_KEY))

    def test_only_the_builders_own_requests_skip_the_read_limit(self):
        factory = APIRequestFactory()
        throttle = UserReadRateThrottle()
        built = factory.get('/api/v1/loads/')
        setattr(built, ds.BUILD_FLAG, True)
        self.assertTrue(throttle.allow_request(built, None))
        # An HTTP header of the same name is not the flag.
        outside = factory.get('/api/v1/loads/', HTTP_DEMO_SNAPSHOT_BUILD='1')
        self.assertFalse(getattr(outside, ds.BUILD_FLAG, False))
