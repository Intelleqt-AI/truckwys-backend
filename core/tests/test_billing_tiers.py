"""Tests for truck-count-based subscription tier pricing (PayFast)."""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.models import BillingTransaction, Company, Vehicle
from core.services.payfast import get_tier_for_vehicle_count

User = get_user_model()


def make_vehicles(company, count, start=0):
    Vehicle.objects.bulk_create([
        Vehicle(
            company=company,
            vin=f'TIERVIN{start + i:05d}',
            plate=f'TIER{start + i:05d}',
            make='Test',
            model='Truck',
            year=2020,
            type='TRUCK',
            capacity=Decimal('1000.00'),
            fuel_type='Diesel',
        )
        for i in range(count)
    ])


class TierHelperTests(SimpleTestCase):
    """get_tier_for_vehicle_count band boundaries."""

    def test_tier_boundaries(self):
        cases = [
            (0, 'pro_50'),
            (1, 'pro_50'),
            (50, 'pro_50'),
            (51, 'pro_100'),
            (100, 'pro_100'),
            (101, 'pro_150'),
            (150, 'pro_150'),
        ]
        for count, expected_key in cases:
            self.assertEqual(get_tier_for_vehicle_count(count)['key'], expected_key)

    def test_oversize_fleet_pays_top_tier(self):
        self.assertEqual(get_tier_for_vehicle_count(151)['key'], 'pro_150')
        self.assertEqual(get_tier_for_vehicle_count(999)['key'], 'pro_150')


class BillingTierViewTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(
            company_name='Tier Test Logistics',
            registration_number='TIER123',
        )
        self.user = User.objects.create_user(
            username='tieruser',
            email='tier@test.com',
            password='testpass123',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)


class SubscribeViewTierTests(BillingTierViewTestCase):
    def subscribe(self, body=None):
        return self.client.post('/api/v1/billing/subscribe/', body or {}, format='json')

    def test_tier_derived_from_vehicle_count(self):
        make_vehicles(self.company, 60)
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['plan'], 'pro_100')
        self.assertEqual(response.data['amount'], '8999.00')
        self.assertEqual(response.data['vehicle_count'], 60)
        self.assertEqual(response.data['form_data']['custom_str2'], 'pro_100')
        self.assertEqual(response.data['form_data']['amount'], '8999.00')
        txn = BillingTransaction.objects.get(company=self.company)
        self.assertEqual(txn.status, 'pending')
        self.assertEqual(txn.plan, 'pro_100')
        self.assertEqual(txn.amount, Decimal('8999.00'))

    def test_client_sent_plan_is_ignored(self):
        make_vehicles(self.company, 10)
        response = self.subscribe({'plan': 'enterprise'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['plan'], 'pro_50')
        self.assertEqual(response.data['amount'], '4499.00')

    def test_zero_vehicles_falls_into_first_tier(self):
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['plan'], 'pro_50')
        self.assertEqual(response.data['amount'], '4499.00')

    def test_oversize_fleet_charged_top_tier(self):
        make_vehicles(self.company, 151)
        response = self.subscribe()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['plan'], 'pro_150')
        self.assertEqual(response.data['amount'], '13499.00')


@mock.patch('core.views_billing.confirm_payment_with_payfast', return_value=True)
@mock.patch('core.views_billing.validate_itn', return_value=True)
class PayFastITNTierTests(BillingTierViewTestCase):
    def setUp(self):
        super().setUp()
        self.txn = BillingTransaction.objects.create(
            company=self.company,
            amount=Decimal('8999.00'),
            payment_id='m-tier-test-1',
            status='pending',
            plan='pro_100',
        )

    def post_itn(self, **overrides):
        data = {
            'payment_status': 'COMPLETE',
            'm_payment_id': 'm-tier-test-1',
            'pf_payment_id': 'pf-100',
            'token': 'tok-tier-1',
            'custom_str1': str(self.company.id),
            'custom_str2': 'pro_100',
            'amount_gross': '8999.00',
        }
        data.update(overrides)
        return self.client.post('/api/v1/billing/itn/', data)

    def test_tier_key_completes_and_activates_company(self, *_mocks):
        response = self.post_itn()
        self.assertEqual(response.status_code, 200)
        self.txn.refresh_from_db()
        self.company.refresh_from_db()
        self.assertEqual(self.txn.status, 'complete')
        self.assertEqual(self.company.subscription_plan, 'pro_100')
        self.assertEqual(self.company.subscription_status, 'active')
        self.assertEqual(self.company.payfast_token, 'tok-tier-1')

    def test_tampered_amount_is_rejected(self, *_mocks):
        response = self.post_itn(amount_gross='4499.00')
        self.assertEqual(response.status_code, 400)
        self.txn.refresh_from_db()
        self.company.refresh_from_db()
        self.assertEqual(self.txn.status, 'failed')
        self.assertNotEqual(self.company.subscription_status, 'active')


class BillingStatusTierTests(BillingTierViewTestCase):
    def test_status_includes_tier_data(self):
        make_vehicles(self.company, 10)
        response = self.client.get('/api/v1/billing/status/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['vehicle_count'], 10)
        self.assertEqual(response.data['current_tier']['key'], 'pro_50')
        self.assertEqual(response.data['current_tier']['amount'], '4499.00')
        tiers = response.data['tiers']
        self.assertEqual([t['key'] for t in tiers], ['pro_50', 'pro_100', 'pro_150'])
        self.assertEqual([t['amount'] for t in tiers], ['4499.00', '8999.00', '13499.00'])

    def test_legacy_plan_key_still_resolves(self):
        self.company.subscription_plan = 'pro'
        self.company.subscription_status = 'active'
        self.company.save()
        response = self.client.get('/api/v1/billing/status/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['amount'], '4999.00')
        self.assertEqual(response.data['item_name'], 'TruckWys Pro')
