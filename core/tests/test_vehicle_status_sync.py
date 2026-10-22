"""A truck's status follows its loads (core.services.vehicle_status)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import Company, Customer, Load, Vehicle


class VehicleStatusSyncTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(company_name='Sync Co')
        self.cust = Customer.objects.create(company=self.co, name='Shipper', email='s@example.com',
                                            phone='', address='', city='', state='', zip_code='')
        self.truck = self._truck('SYNC-1')

    def _truck(self, plate, status='AVAILABLE'):
        return Vehicle.objects.create(company=self.co, make='Scania', model='R450', plate=plate,
                                      type='Truck', capacity=28, fuel_type='Diesel', status=status)

    def _load(self, status='PENDING', vehicle=None, delivery_days=1):
        now = timezone.now()
        return Load.objects.create(
            company=self.co, load_number=f'SYNC-{Load.objects.count()}', customer=self.cust,
            vehicle=vehicle or self.truck,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000', pickup_date=now,
            delivery_location='DBN', delivery_city='DBN', delivery_state='KZN', delivery_zip='4000',
            delivery_date=now + timedelta(days=delivery_days), cargo_description='x', weight=1, distance=1, rate=1,
            total_amount=1, status=status)

    def status(self, truck=None):
        return Vehicle.objects.get(pk=(truck or self.truck).pk).status

    def test_pending_load_leaves_the_truck_available(self):
        self._load('PENDING')
        self.assertEqual(self.status(), 'AVAILABLE')

    def test_assigned_and_in_transit_mark_in_use_and_delivery_frees_it(self):
        load = self._load('ASSIGNED')
        self.assertEqual(self.status(), 'IN_USE')
        load.status = 'IN_TRANSIT'
        load.save()
        self.assertEqual(self.status(), 'IN_USE')
        load.status = 'DELIVERED'
        load.save()
        self.assertEqual(self.status(), 'AVAILABLE')

    def test_truck_stays_in_use_while_another_load_is_active(self):
        first = self._load('IN_TRANSIT')
        self._load('ASSIGNED')
        first.status = 'CANCELLED'
        first.save()
        self.assertEqual(self.status(), 'IN_USE')

    def test_moving_the_load_frees_the_old_truck(self):
        load = self._load('ASSIGNED')
        other = self._truck('SYNC-2')
        load.vehicle = other
        load.save()
        self.assertEqual(self.status(), 'AVAILABLE')
        self.assertEqual(self.status(other), 'IN_USE')

    def test_deleting_an_active_load_frees_the_truck(self):
        self._load('IN_TRANSIT').delete()
        self.assertEqual(self.status(), 'AVAILABLE')

    def test_a_load_left_open_does_not_mark_the_truck_in_use(self):
        # Past its delivery date: left open, not current work (stale_work).
        self._load('IN_TRANSIT', delivery_days=-3)
        self.assertEqual(self.status(), 'AVAILABLE')

    def test_maintenance_is_never_overridden(self):
        self.truck.status = 'MAINTENANCE'
        self.truck.save()
        load = self._load('ASSIGNED')
        self.assertEqual(self.status(), 'MAINTENANCE')
        load.status = 'DELIVERED'
        load.save()
        self.assertEqual(self.status(), 'MAINTENANCE')
