"""Assigning an order: vehicle and driver are both optional, so an order can
be marked Assigned with either, both or neither."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Driver, Load, Vehicle

User = get_user_model()


class AssignVehicleOptionalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Assign Co')
        cls.admin = User.objects.create_user(username='asg_admin', email='a@asg.test', password='x')
        cls.admin.role = 'ADMIN'; cls.admin.company = cls.co; cls.admin.save()
        du = User.objects.create_user(username='asg_driver', email='d@asg.test', password='x', first_name='Sipho')
        du.company = cls.co; du.save()
        cls.driver = Driver.objects.create(user=du, company=cls.co, license_number='L-1', license_state='GP',
                                           license_expiry=timezone.localdate() + timedelta(days=300),
                                           hire_date=timezone.localdate())
        cls.vehicle = Vehicle.objects.create(company=cls.co, vin='VIN-ASG', plate='ASG1GP', make='MAN', model='TGS',
                                             year=2021, type='Truck', capacity=Decimal('30000'), fuel_type='Diesel',
                                             status='AVAILABLE')
        cls.cust = Customer.objects.create(company=cls.co, name='Acme', email='c@asg.test', phone='1', address='x',
                                           city='JHB', state='GP', zip_code='2000')

    def setUp(self):
        self.api = APIClient(); self.api.force_authenticate(self.admin)
        now = timezone.now()
        self.load = Load.objects.create(
            company=self.co, load_number=f'ASG-{Load.objects.count()}', customer=self.cust,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000', pickup_date=now,
            delivery_location='DBN', delivery_city='DBN', delivery_state='KZN', delivery_zip='4000',
            delivery_date=now + timedelta(days=1), cargo_description='x', weight=1, distance=1, rate=1,
            total_amount=1, status='PENDING')

    def url(self, action=''):
        return f'/api/v1/loads/{self.load.id}/{action}'

    def test_driver_alone_assigns(self):
        r = self.api.post(self.url('assign_driver/'), {'driver_id': self.driver.id}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.load.refresh_from_db()
        self.assertEqual((self.load.status, self.load.driver_id, self.load.vehicle_id), ('ASSIGNED', self.driver.id, None))

    def test_vehicle_alone_still_assigns(self):
        r = self.api.post(self.url('assign_driver/'), {'vehicle_id': self.vehicle.id}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.load.refresh_from_db()
        self.assertEqual(self.load.status, 'ASSIGNED')

    def test_clearing_both_keeps_assigned(self):
        self.api.post(self.url('assign_driver/'), {'driver_id': self.driver.id}, format='json')
        self.api.post(self.url('assign_driver/'), {}, format='json')
        self.load.refresh_from_db()
        self.assertEqual((self.load.status, self.load.driver_id), ('ASSIGNED', None))

    def test_nobody_can_be_marked_assigned(self):
        r = self.api.patch(self.url('update_status/'), {'status': 'ASSIGNED'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.load.refresh_from_db()
        self.assertEqual(self.load.status, 'ASSIGNED')
        Load.objects.filter(pk=self.load.pk).update(status='PENDING')
        r = self.api.patch(self.url(), {'status': 'ASSIGNED'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.load.refresh_from_db()
        self.assertEqual(self.load.status, 'ASSIGNED')

    def test_status_change_with_a_driver_only(self):
        Load.objects.filter(pk=self.load.pk).update(driver=self.driver)
        r = self.api.patch(self.url('update_status/'), {'status': 'ASSIGNED'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        r = self.api.patch(self.url(), {'status': 'ASSIGNED'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
