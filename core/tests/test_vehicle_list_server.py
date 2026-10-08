"""The Vehicles list works out each truck's open order, delivered work, the
tiles and the tile filters on the server (core.services.vehicle_list)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Load, Vehicle
from core.services.stale_work import is_stale

User = get_user_model()


class StaleRuleTests(TestCase):
    def test_matches_the_frontend_rule(self):
        now = timezone.now()
        self.assertFalse(is_stale('ASSIGNED', now + timedelta(days=1), now, now))
        self.assertTrue(is_stale('ASSIGNED', now - timedelta(days=2), now, now))          # past delivery
        self.assertTrue(is_stale('PENDING', None, now - timedelta(days=31), now))           # open too long
        self.assertFalse(is_stale('PENDING', None, now - timedelta(days=30), now))
        self.assertFalse(is_stale('DELIVERED', now - timedelta(days=9), now, now))         # not open


class VehicleListServerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Fleet Co')
        cls.user = User.objects.create_user(username='fleet_admin', email='f@fleet.test', password='x')
        cls.user.role = 'ADMIN'; cls.user.company = cls.co; cls.user.save()
        cust = Customer.objects.create(company=cls.co, name='Acme', email='a@a.test', phone='1', address='x',
                                       city='JHB', state='GP', zip_code='2000')

        def truck(plate, status):
            return Vehicle.objects.create(company=cls.co, vin=f'VIN-{plate}', plate=plate, make='MAN', model='TGS',
                                          year=2021, type='Truck', capacity=Decimal('30000'), fuel_type='Diesel', status=status)
        cls.busy = truck('AAA1', 'IN_USE')          # in use, on a current order
        cls.idle = truck('BBB2', 'IN_USE')          # in use, no order: mismatch
        cls.free = truck('CCC3', 'AVAILABLE')       # available, holding an order left open
        cls.shop = truck('DDD4', 'OUT_OF_SERVICE')
        now = timezone.now()
        n = [0]

        def load(vehicle, status, amount='1000', delivery_days=2):
            n[0] += 1
            return Load.objects.create(
                company=cls.co, load_number=f'LD-V-{n[0]}', customer=cust, vehicle=vehicle,
                pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000', pickup_date=now,
                delivery_location='CPT', delivery_city='CPT', delivery_state='WC', delivery_zip='8000',
                delivery_date=now + timedelta(days=delivery_days), cargo_description='Freight', weight=Decimal('1'),
                distance=Decimal('1'), rate=Decimal(amount), total_amount=Decimal(amount), status=status)
        cls.current = load(cls.busy, 'IN_TRANSIT')
        load(cls.busy, 'DELIVERED', '3000')
        load(cls.free, 'ASSIGNED', delivery_days=-3)   # left open
        load(cls.shop, 'INVOICED', '500')
        load(None, 'DELIVERED', '200')                 # no truck recorded

    def setUp(self):
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def get(self, query=''):
        return self.c.get(f'/api/v1/vehicles/?view=fleet&{query}').json()

    def test_rows_summary_and_sort(self):
        body = self.get('sort=revenue')
        self.assertEqual([r['plate'] for r in body['results']][:2], ['AAA1', 'DDD4'])
        rows = {r['plate']: r for r in body['results']}
        self.assertEqual((rows['AAA1']['delivered_revenue'], rows['AAA1']['delivered_loads']), (3000.0, 1))
        self.assertEqual(rows['AAA1']['active_load']['id'], self.current.id)
        self.assertEqual(rows['AAA1']['active_load']['delivery_city'], 'CPT')
        self.assertIsNone(rows['BBB2']['active_load'])
        self.assertTrue(rows['CCC3']['holding_open'])
        self.assertEqual(body['summary'], {
            'total': 4, 'job': 2, 'free': 1, 'shop': 1, 'out_of_service': 1, 'job_no_order': 1,
            'free_on_order': 0, 'shop_on_order': 0, 'free_holding': 1,
            'delivered': {'revenue': 3700.0, 'loads': 3, 'no_vehicle_revenue': 200.0, 'no_vehicle_loads': 1},
        })

    def test_tile_filters(self):
        plates = lambda tile: sorted(r['plate'] for r in self.get(f'tile={tile}')['results'])
        self.assertEqual(plates('job'), ['AAA1', 'BBB2'])
        self.assertEqual(plates('free'), ['CCC3'])
        self.assertEqual(plates('shop'), ['DDD4'])
        self.assertEqual(plates('mismatch'), ['BBB2'])   # the stale order on CCC3 is not current work
        body = self.get('tile=job&search=AAA')
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['summary']['total'], 1)   # tiles follow the search

    def test_plain_list_unchanged(self):
        body = self.c.get('/api/v1/vehicles/').json()
        self.assertNotIn('summary', body)
        self.assertNotIn('active_load', body['results'][0])


class DriverListServerTests(TestCase):
    def test_summary_and_open_order(self):
        from core.models import Driver
        co = Company.objects.create(company_name='Drv Co')
        admin = User.objects.create_user(username='drv_admin', email='d@drv.test', password='x')
        admin.role = 'ADMIN'; admin.company = co; admin.save()
        today = timezone.localdate()

        def driver(username, first, last, expiry_days, status='ACTIVE'):
            u = User.objects.create_user(username=username, email=f'{username}@drv.test', password='x',
                                         first_name=first, last_name=last)
            u.company = co; u.save()
            return Driver.objects.create(user=u, company=co, license_number=f'L-{username}', license_state='GP',
                                         license_expiry=today + timedelta(days=expiry_days), hire_date=today, status=status)
        thabo = driver('thabo', 'Thabo', 'Nkosi', -5)
        driver('anna', 'Anna', 'Smit', 40)
        off = driver('piet', 'Piet', '', 200, status='ON_LEAVE')
        cust = Customer.objects.create(company=co, name='Acme', email='a@d.test', phone='1', address='x',
                                       city='JHB', state='GP', zip_code='2000')
        now = timezone.now()
        Load.objects.create(company=co, load_number='LD-D-1', customer=cust, driver=off,
                            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000', pickup_date=now,
                            delivery_location='CPT', delivery_city='CPT', delivery_state='WC', delivery_zip='8000',
                            delivery_date=now, cargo_description='x', weight=1, distance=1, rate=1, total_amount=1,
                            status='ASSIGNED')
        Load.objects.create(company=co, load_number='LD-D-2', customer=cust, driver=None,
                            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000', pickup_date=now,
                            delivery_location='CPT', delivery_city='CPT', delivery_state='WC', delivery_zip='8000',
                            delivery_date=now, cargo_description='x', weight=1, distance=1, rate=1, total_amount=1,
                            status='DELIVERED')
        c = APIClient(); c.force_authenticate(admin)
        body = c.get('/api/v1/drivers/?view=fleet&status=ON_LEAVE').json()
        self.assertEqual([r['open_load_number'] for r in body['results']], ['LD-D-1'])
        summary = body['summary']
        summary.pop('has_efficiency')   # driver stats fill efficiency on save
        self.assertEqual(summary, {
            'status_counts': {'ALL': 3, 'ACTIVE': 2, 'INACTIVE': 0, 'ON_LEAVE': 1},   # chip not applied
            'expired_count': 1, 'expired_names': ['Thabo Nkosi'],
            'next_renewal': {'name': 'Anna Smit', 'date': (today + timedelta(days=40)).isoformat()},
            'renew_soon': 1, 'delivered_loads': 1, 'no_driver_loads': 1, 'has_revenue': False,
        })
        self.assertEqual(c.get('/api/v1/drivers/?view=fleet&search=nkosi').json()['summary']['status_counts']['ALL'], 1)
        self.assertNotIn('summary', c.get('/api/v1/drivers/').json())
        self.assertEqual(thabo.pk, c.get('/api/v1/drivers/?view=fleet&search=thabo').json()['results'][0]['id'])
