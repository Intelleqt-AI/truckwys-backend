"""Orders and History are server-side: tab, search, newest first and tiles
(core.services.load_list)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Load, Vehicle

User = get_user_model()


class LoadListServerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Loads Co', vat_registered=True)
        cls.user = User.objects.create_user(username='loads_admin', email='l@loads.test', password='x')
        cls.user.role = 'ADMIN'; cls.user.company = cls.co; cls.user.save()
        acme = Customer.objects.create(company=cls.co, name='Acme', email='a@l.test', phone='1', address='x',
                                       city='JHB', state='GP', zip_code='2000')
        zulu = Customer.objects.create(company=cls.co, name='Zulu', email='z@l.test', phone='1', address='x',
                                       city='JHB', state='GP', zip_code='2000')
        truck = Vehicle.objects.create(company=cls.co, vin='VIN-L1', plate='ND 77 GP', make='MAN', model='TGS', year=2021,
                                       type='Truck', capacity=Decimal('1'), fuel_type='Diesel', status='IN_USE')
        now = timezone.now()

        def load(num, status, cust=acme, vehicle=None, pickup_days=0, delivery_days=2, delivered_days=None, amount='1000'):
            return Load.objects.create(
                company=cls.co, load_number=num, customer=cust, vehicle=vehicle,
                pickup_location='Joburg depot', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
                pickup_date=now + timedelta(days=pickup_days), delivery_location='Cape Town port', delivery_city='CPT',
                delivery_state='WC', delivery_zip='8000', delivery_date=now + timedelta(days=delivery_days),
                actual_delivered_at=now + timedelta(days=delivered_days) if delivered_days is not None else None,
                cargo_description='x', weight=1, distance=1, rate=Decimal(amount), total_amount=Decimal(amount), status=status)
        load('O-1', 'PENDING')                                                  # needs a vehicle
        load('O-2', 'ASSIGNED', delivery_days=-3)                               # needs a vehicle, past delivery
        load('O-3', 'IN_TRANSIT', vehicle=truck, cust=zulu)                     # on schedule
        load('O-4', 'IN_TRANSIT', pickup_days=-40, delivery_days=5)             # moving, no truck, open too long
        load('H-old', 'DELIVERED', delivered_days=-10)
        load('H-new', 'INVOICED', delivered_days=-1, amount='2000')
        load('H-cx', 'CANCELLED', delivery_days=-5)

    def setUp(self):
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def get(self, query):
        return self.c.get(f'/api/v1/loads/?{query}').json()

    def numbers(self, query):
        return [r['load_number'] for r in self.get(query)['results']]

    def test_orders_tab_and_tiles(self):
        self.assertCountEqual(self.numbers('tab=orders'), ['O-1', 'O-2', 'O-3', 'O-4'])
        self.assertEqual(self.numbers('tab=orders&q=nd 77'), ['O-3'])          # truck plate
        self.assertEqual(self.numbers('tab=orders&q=zulu'), ['O-3'])           # customer
        self.assertCountEqual(self.numbers('tab=orders&status=IN_TRANSIT'), ['O-3', 'O-4'])
        self.assertEqual(self.get('tab=orders&q=zulu')['summary'], {
            'open_count': 4, 'need_vehicle': 2, 'need_vehicle_overdue': 1, 'moving_no_vehicle': 1,
            'in_transit': 2, 'in_transit_overdue': 0, 'left_open': 2, 'open_total_incl_vat': 4600.0, 'any_loads': True,
        })

    def test_history_newest_first_and_tiles(self):
        self.assertEqual(self.numbers('tab=history'), ['H-new', 'H-cx', 'H-old'])
        body = self.get('tab=history&page_size=1')
        self.assertEqual(body['count'], 3)
        self.assertEqual(body['summary'], {
            'history_count': 3, 'delivered_not_invoiced': 1, 'invoiced': 1, 'invoiced_total_incl_vat': 2300.0,
            'completed': 2, 'completed_total_incl_vat': 3450.0, 'any_loads': True,
        })

    def test_plain_list_unchanged(self):
        self.assertNotIn('summary', self.get(''))
        self.assertEqual(self.get('')['count'], 7)
