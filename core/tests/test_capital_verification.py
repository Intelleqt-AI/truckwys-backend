"""POD verification tiers and duplicate / fraud checks (core.capital.verification)."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from core.capital import verification as v
from core.capital.policy import default_policy
from core.models import Load, Vehicle

from .test_capital_scoring import (
    days_ago, make_company, make_customer, make_debtor, make_invoice, make_load,
)

D = Decimal
SA = ZoneInfo('Africa/Johannesburg')
DBN = (D('-29.858680'), D('31.021840'))


def pod(load, *, source='CAMERA', captured=None, lat=DBN[0], lng=DBN[1], sha='a' * 64, file='pod/x.jpg',
        signature=''):
    Load.objects.filter(pk=load.pk).update(
        pod_document=file, pod_signature=signature, pod_source=source,
        pod_captured_at=captured if captured is not None else timezone.now() - timedelta(hours=30),
        pod_latitude=lat, pod_longitude=lng, pod_file_sha256=sha,
    )
    load.refresh_from_db()
    return load


def codes(res):
    return [r['code'] for r in res['reasons']]


def flag_codes(res):
    return [r['code'] for r in res['flags']]


class VerificationTierTests(TestCase):
    def setUp(self):
        self.policy = default_policy()
        self.co = make_company()
        self.cust = make_customer(self.co)
        self.load = make_load(self.co, self.cust, delivery_lat=DBN[0], delivery_lng=DBN[1])

    def test_v0_no_load_or_no_pod(self):
        self.assertEqual(v.verification_tier(None, self.policy)['tier'], 'V0')
        res = v.verification_tier(self.load, self.policy)
        self.assertEqual(res['tier'], 'V0')
        self.assertEqual(codes(res), ['E-POD-V0'])

    def test_v1_upload_or_signature_or_missing_metadata(self):
        res = v.verification_tier(pod(self.load, source='UPLOAD'), self.policy)
        self.assertEqual(res['tier'], 'V1')
        self.assertEqual(codes(res), ['E-POD-V1'])
        self.assertIn('camera capture', res['details']['missing'])
        res = v.verification_tier(pod(self.load, file='', signature='J Smith', source=''), self.policy)
        self.assertEqual(res['tier'], 'V1')
        res = v.verification_tier(pod(self.load, sha=''), self.policy)
        self.assertEqual(res['tier'], 'V1')
        self.assertIn('file hash', res['details']['missing'])
        res = v.verification_tier(pod(self.load, lat=None), self.policy)
        self.assertEqual(res['tier'], 'V1')

    def test_v2_inside_geofence(self):
        # ~0.3 km from the delivery point
        res = v.verification_tier(pod(self.load, lat=D('-29.856000')), self.policy)
        self.assertEqual(res['tier'], 'V2')
        self.assertEqual(codes(res), ['V2'])
        self.assertEqual(res['details']['geofence'], 'inside')
        self.assertLess(D(res['details']['distance_km']), D('1'))
        self.assertEqual(res['details']['telematics'], 'no_data')

    def test_far_from_delivery_is_v1_with_v2_far(self):
        # ~5.6 km north
        res = v.verification_tier(pod(self.load, lat=D('-29.808680')), self.policy)
        self.assertEqual(res['tier'], 'V1')
        self.assertEqual(codes(res), ['V2-FAR'])
        self.assertEqual(res['reasons'][0]['params']['km'], '5.6')
        self.assertEqual(res['details']['geofence'], 'outside')

    def test_geofence_not_checked_without_delivery_coordinates(self):
        load = make_load(self.co, self.cust)
        res = v.verification_tier(pod(load), self.policy)
        self.assertEqual(res['tier'], 'V2')
        self.assertEqual(res['details']['geofence'], 'not_checked')

    def test_v3_unreachable_without_telematics(self):
        self.assertIsNone(v._telematics_stop_match(self.load))

    def test_haversine(self):
        # Johannesburg -> Durban is about 500 km great-circle
        km = v.haversine_km(D('-26.2041'), D('28.0473'), D('-29.8587'), D('31.0218'))
        self.assertTrue(D('490') < km < D('510'))


class FraudCheckTests(TestCase):
    def setUp(self):
        self.policy = default_policy()
        self.debtor = make_debtor()
        self.co_a = make_company('Alpha')
        self.co_b = make_company('Bravo')
        self.cust_a = make_customer(self.co_a, self.debtor)
        self.cust_b = make_customer(self.co_b, self.debtor)

    def test_clean_invoice(self):
        load = make_load(self.co_a, self.cust_a)
        inv = make_invoice(self.co_a, self.cust_a, days_ago(1), load=load)
        res = v.fraud_checks(inv, self.policy)
        self.assertEqual(res['score'], D('0.000'))
        self.assertFalse(res['duplicate'])
        self.assertEqual(res['flags'], [])

    def test_pod_reuse_across_tenants(self):
        la = pod(make_load(self.co_a, self.cust_a, number='L-A-1'), sha='b' * 64)
        pod(make_load(self.co_b, self.cust_b, number='L-B-1'), sha='b' * 64)
        inv = make_invoice(self.co_a, self.cust_a, days_ago(1), load=la)
        res = v.fraud_checks(inv, self.policy)
        self.assertTrue(res['duplicate'])
        self.assertIn('F-POD-REUSE', flag_codes(res))
        self.assertIn('L-B-1', res['duplicate_detail'])
        self.assertGreaterEqual(res['score'], D('0.9'))

    def test_network_duplicate_across_two_tenants(self):
        when = timezone.now() - timedelta(days=2)
        la = make_load(self.co_a, self.cust_a, delivered=when)
        lb = make_load(self.co_b, self.cust_b, delivered=when)
        mine = make_invoice(self.co_a, self.cust_a, days_ago(1), load=la, subtotal='10000.00')
        other = make_invoice(self.co_b, self.cust_b, days_ago(1), load=lb, subtotal='10050.00', number='INV-B-77')
        res = v.fraud_checks(mine, self.policy)
        self.assertTrue(res['duplicate'])
        self.assertIn('INV-B-77', res['duplicate_detail'])
        self.assertIn('E-DUPLICATE', flag_codes(res))
        self.assertNotIn('F-SAME-AMOUNT', flag_codes(res))
        self.assertGreaterEqual(res['score'], D('0.9'))
        # a different delivery date is not a duplicate, only a same-amount signal
        lb.delivery_date = when - timedelta(days=3)
        lb.save()
        res = v.fraud_checks(mine, self.policy)
        self.assertFalse(res['duplicate'])
        self.assertIn('F-SAME-AMOUNT', flag_codes(res))
        self.assertEqual(res['score'], D('0.150'))
        self.assertIsNotNone(other.pk)

    def test_second_invoice_on_same_load_is_duplicate(self):
        la = make_load(self.co_a, self.cust_a)
        make_invoice(self.co_a, self.cust_a, days_ago(1), load=la, number='INV-A-1')
        second = make_invoice(self.co_a, self.cust_a, days_ago(1), load=la, subtotal='3000.00')
        res = v.fraud_checks(second, self.policy)
        self.assertTrue(res['duplicate'])
        self.assertIn('INV-A-1', res['duplicate_detail'])

    def test_void_and_draft_invoices_do_not_count(self):
        la = make_load(self.co_a, self.cust_a)
        make_invoice(self.co_a, self.cust_a, days_ago(1), load=la, status='CANCELLED')
        make_invoice(self.co_a, self.cust_a, days_ago(1), load=la, status='DRAFT')
        inv = make_invoice(self.co_a, self.cust_a, days_ago(1), load=la)
        self.assertFalse(v.fraud_checks(inv, self.policy)['duplicate'])

    def test_same_amount_within_7_days_without_identity_uses_customer(self):
        cust = make_customer(self.co_a)  # no debtor identity
        make_invoice(self.co_a, cust, days_ago(10), subtotal='7000.00')
        make_invoice(self.co_a, cust, days_ago(30), subtotal='7000.00')   # outside 7 days
        inv = make_invoice(self.co_a, cust, days_ago(5), subtotal='7000.00')
        res = v.fraud_checks(inv, self.policy)
        flag = [f for f in res['flags'] if f['code'] == 'F-SAME-AMOUNT'][0]
        self.assertEqual(flag['params']['n'], 1)
        self.assertEqual(res['score'], D('0.150'))

    def test_round_amount(self):
        inv = make_invoice(self.co_a, self.cust_a, days_ago(1), subtotal='20000.00', vat='0')
        res = v.fraud_checks(inv, self.policy)
        self.assertIn('F-ROUND', flag_codes(res))
        self.assertEqual(res['score'], D('0.050'))
        small = make_invoice(self.co_b, self.cust_b, days_ago(40), subtotal='5000.00', vat='0')
        self.assertNotIn('F-ROUND', flag_codes(v.fraud_checks(small, self.policy)))

    def test_night_pod(self):
        night = datetime(2026, 9, 20, 23, 30, tzinfo=SA)
        day = datetime(2026, 9, 20, 14, 0, tzinfo=SA)
        early = datetime(2026, 9, 20, 4, 59, tzinfo=SA)
        for captured, flagged in ((night, True), (day, False), (early, True)):
            with self.subTest(captured=captured):
                load = pod(make_load(self.co_a, self.cust_a), captured=captured, sha='')
                inv = make_invoice(self.co_a, self.cust_a, days_ago(1), load=load)
                res = v.fraud_checks(inv, self.policy)
                self.assertEqual('F-NIGHT-POD' in flag_codes(res), flagged)
        self.assertEqual([f for f in res['flags'] if f['code'] == 'F-NIGHT-POD'][0]['params']['hour'], '04')

    def test_capacity(self):
        Vehicle.objects.create(company=self.co_a, make='Volvo', model='FH', plate='ND 1-234', type='TRUCK',
                               capacity=D('30000'), fuel_type='Diesel')
        when = timezone.now() - timedelta(days=5)
        base = dict(company=self.co_a, customer=self.cust_a, pickup_location='JHB', pickup_city='JHB',
                    pickup_state='GP', pickup_zip='2000', pickup_date=when, delivery_location='DBN',
                    delivery_city='DBN', delivery_state='KZN', delivery_zip='4000', delivery_date=when,
                    cargo_description='x', weight=D('1'), rate=D('1'), total_amount=D('1'), status='DELIVERED')
        Load.objects.bulk_create([Load(load_number=f'L-CAP-{i}', **base) for i in range(90)])
        inv = make_invoice(self.co_a, self.cust_a, days_ago(1))
        self.assertNotIn('F-CAPACITY', flag_codes(v.fraud_checks(inv, self.policy)))   # 90 = 3 x 1 x 30
        Load.objects.create(load_number='L-CAP-90', **base)
        res = v.fraud_checks(inv, self.policy)
        self.assertIn('F-CAPACITY', flag_codes(res))
        self.assertEqual(res['score'], D('0.100'))
        # no vehicles -> capacity check skipped
        inv_b = make_invoice(self.co_b, self.cust_b, days_ago(20))
        self.assertNotIn('F-CAPACITY', flag_codes(v.fraud_checks(inv_b, self.policy)))

    def test_scores_add_and_cap(self):
        load = pod(make_load(self.co_a, self.cust_a), captured=datetime(2026, 9, 20, 23, 0, tzinfo=SA), sha='')
        make_invoice(self.co_a, self.cust_a, days_ago(3), subtotal='20000.00', vat='0')
        inv = make_invoice(self.co_a, self.cust_a, days_ago(1), subtotal='20000.00', vat='0', load=load)
        res = v.fraud_checks(inv, self.policy)
        self.assertEqual(sorted(flag_codes(res)), ['F-NIGHT-POD', 'F-ROUND', 'F-SAME-AMOUNT'])
        self.assertEqual(res['score'], D('0.300'))
        self.assertIsNone(v._route_mismatch(load))
