"""Return-load linking: rules, API and candidate suggestions."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import ActivityEvent, Load
from core.services.return_loads import LinkError, check_link, link_return, return_candidates, unlink_return
from core.tests.trip_fixtures import make_company, make_customer, make_load, make_user

JHB = dict(lat=Decimal('-26.2041'), lng=Decimal('28.0473'))
CPT = dict(lat=Decimal('-33.9249'), lng=Decimal('18.4241'))
PAARL = dict(lat=Decimal('-33.7342'), lng=Decimal('18.9621'))   # ~55 km from CPT
DBN = dict(lat=Decimal('-29.8587'), lng=Decimal('31.0218'))


def lane(a, b):
    return dict(pickup_lat=a['lat'], pickup_lng=a['lng'], delivery_lat=b['lat'], delivery_lng=b['lng'])


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = make_company('Return Co')
        cls.other = make_company('Other Co')
        cls.user = make_user('ret_u', cls.co)
        cls.cust = make_customer(cls.co, 'ret')
        cls.cust_o = make_customer(cls.other, 'reto')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.out = make_load(self.co, self.cust, 'OUT-1', pickup='Johannesburg', delivery='Cape Town',
                             pickup_in_days=1, days=2, **lane(JHB, CPT))
        # Collects in Paarl the day after the drop, back to Johannesburg.
        self.ret = make_load(self.co, self.cust, 'RET-1', pickup='Paarl', delivery='Johannesburg',
                             pickup_in_days=4, days=2, **lane(PAARL, JHB))


class LinkRulesTests(_Base):
    def test_clean_reverse_lane_links_without_warnings(self):
        self.assertEqual(check_link(self.out, self.ret), [])
        link_return(self.out, self.ret, user=self.user)
        self.ret.refresh_from_db()
        self.assertEqual(self.ret.return_of_id, self.out.id)
        self.assertEqual(self.ret.return_link_source, 'manual')
        self.assertEqual(Load.objects.get(pk=self.out.pk).return_load.pk, self.ret.pk)
        self.assertTrue(ActivityEvent.objects.filter(entity_id=self.out.id, title__startswith='Return load').exists())
        # Idempotent.
        link_return(self.out, self.ret, user=self.user)

    def test_other_company_is_not_found(self):
        foreign = make_load(self.other, self.cust_o, 'FOREIGN-1', **lane(CPT, JHB))
        with self.assertRaises(LinkError) as e:
            check_link(self.out, foreign)
        self.assertEqual(e.exception.code, 'not_found')

    def test_pairs_only(self):
        link_return(self.out, self.ret)
        third = make_load(self.co, self.cust, 'THIRD', pickup='Johannesburg', delivery='Cape Town', **lane(JHB, CPT))
        self.out.refresh_from_db()
        self.ret.refresh_from_db()
        for out, ret, code in ((self.out, third, 'outbound_has_return'),
                               (third, self.ret, 'return_already_linked'),
                               (self.ret, third, 'outbound_is_return'),
                               (third, self.out, 'return_has_return')):
            with self.assertRaises(LinkError) as e:
                check_link(out, ret)
            self.assertEqual(e.exception.code, code)

    def test_round_trip_and_cancelled_and_self_blocked(self):
        with self.assertRaises(LinkError):
            check_link(self.out, self.out)
        Load.objects.filter(pk=self.ret.pk).update(trip_type='ROUND_TRIP')
        self.ret.refresh_from_db()
        with self.assertRaises(LinkError) as e:
            check_link(self.out, self.ret)
        self.assertEqual(e.exception.code, 'round_trip')
        Load.objects.filter(pk=self.ret.pk).update(trip_type='ONE_WAY', status='CANCELLED')
        self.ret.refresh_from_db()
        with self.assertRaises(LinkError):
            check_link(self.out, self.ret)

    def test_lane_and_timing_warn_not_block(self):
        far = make_load(self.co, self.cust, 'FAR', pickup='Durban', delivery='Cape Town',
                        pickup_in_days=0, days=1, **lane(DBN, CPT))
        codes = {w['code'] for w in check_link(self.out, far)}
        self.assertEqual(codes, {'return_starts_elsewhere', 'return_ends_elsewhere', 'return_before_delivery'})
        link_return(self.out, far)     # warnings never block
        far.refresh_from_db()
        self.assertEqual(far.return_of_id, self.out.id)

    def test_names_used_when_no_coordinates(self):
        a = make_load(self.co, self.cust, 'N-OUT', pickup='Johannesburg', delivery='Durban')
        b = make_load(self.co, self.cust, 'N-RET', pickup='Durban', delivery='Johannesburg', pickup_in_days=3)
        self.assertEqual(check_link(a, b), [])
        c = make_load(self.co, self.cust, 'N-RET2', pickup='Polokwane', delivery='Johannesburg', pickup_in_days=3)
        self.assertIn('return_starts_elsewhere', {w['code'] for w in check_link(a, c)})

    def test_unlink_either_side(self):
        link_return(self.out, self.ret)
        self.ret.refresh_from_db()
        self.assertEqual(unlink_return(self.ret), (self.out.id, self.ret.id))
        self.ret.refresh_from_db()
        self.assertIsNone(self.ret.return_of_id)
        self.assertIsNone(unlink_return(self.out))


class CandidateTests(_Base):
    def test_suggests_loads_collected_near_the_drop_within_days(self):
        make_load(self.co, self.cust, 'TOO-LATE', pickup='Cape Town', delivery='Johannesburg',
                  pickup_in_days=20, **lane(CPT, JHB))
        make_load(self.co, self.cust, 'WRONG-PLACE', pickup='Durban', delivery='Johannesburg',
                  pickup_in_days=4, **lane(DBN, JHB))
        make_load(self.other, self.cust_o, 'OTHER-CO', pickup='Cape Town', delivery='Johannesburg',
                  pickup_in_days=4, **lane(CPT, JHB))
        side = make_load(self.co, self.cust, 'CPT-DBN', pickup='Cape Town', delivery='Durban',
                         pickup_in_days=3, **lane(CPT, DBN))
        rows = return_candidates(self.out, days=7)
        self.assertEqual([r['load_number'] for r in rows], ['RET-1', 'CPT-DBN'])
        self.assertTrue(rows[0]['reverses_lane'])
        self.assertFalse(rows[1]['reverses_lane'])
        self.assertIn('return_ends_elsewhere', {w['code'] for w in rows[1]['warnings']})
        link_return(self.out, side)
        self.out.refresh_from_db()
        self.assertEqual(return_candidates(self.out), [])

    def test_api_link_unlink_candidates(self):
        r = self.api.get(f'/api/v1/loads/{self.out.id}/return-candidates/?days=5')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['candidates'][0]['load_id'], self.ret.id)
        r = self.api.get(f'/api/v1/loads/{self.ret.id}/return-candidates/?direction=outbound')
        self.assertEqual(r.json()['candidates'][0]['load_id'], self.out.id)
        r = self.api.post(f'/api/v1/loads/{self.out.id}/link-return/', {'return_load_id': self.ret.id}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()['outbound_id'], r.json()['return_id'], r.json()['warnings']),
                         (self.out.id, self.ret.id, []))
        self.ret.refresh_from_db()
        self.assertEqual(self.ret.return_linked_by_id, self.user.id)
        # PATCH can't relink.
        self.api.patch(f'/api/v1/loads/{self.ret.id}/', {'return_of': None}, format='json')
        self.ret.refresh_from_db()
        self.assertEqual(self.ret.return_of_id, self.out.id)
        r = self.api.post(f'/api/v1/loads/{self.ret.id}/unlink-return/', {}, format='json')
        self.assertTrue(r.json()['unlinked'])

    def test_api_refuses_other_company_load(self):
        foreign = make_load(self.other, self.cust_o, 'FOREIGN-2', **lane(CPT, JHB))
        r = self.api.post(f'/api/v1/loads/{self.out.id}/link-return/', {'return_load_id': foreign.id}, format='json')
        self.assertEqual(r.status_code, 404)
        r = self.api.post(f'/api/v1/loads/{foreign.id}/link-return/', {'return_load_id': self.ret.id}, format='json')
        self.assertEqual(r.status_code, 404)
        foreign.refresh_from_db()
        self.assertIsNone(Load.objects.get(pk=self.ret.pk).return_of_id)

    def test_api_blocked_pair_is_400_with_code(self):
        Load.objects.filter(pk=self.ret.pk).update(trip_type='ROUND_TRIP')
        r = self.api.post(f'/api/v1/loads/{self.out.id}/link-return/', {'return_load_id': self.ret.id}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['code'], 'round_trip')
