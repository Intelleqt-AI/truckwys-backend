"""Round-trip P&L: a linked return load removes the empty-return estimate on
both legs; actual expenses always win; legacy loads keep a labelled 1,3x."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Expense, Invoice, Load, Trip
from core.services import trip_economics as te
from core.services.reports import load_economics, margin_by_lane
from core.services.return_loads import link_return, unlink_return
from core.tests.quote_rules_fixtures import official_price_now
from core.tests.trip_fixtures import (make_company, make_customer, make_load, make_user, make_vehicle_type,
                                      priced_quote)


def _sum(lines, leg=None):
    return sum(Decimal(str(ln['amount'])) for ln in lines if leg is None or ln['leg'] == leg)


class _Pair(TestCase):
    @classmethod
    def setUpTestData(cls):
        official_price_now()
        cls.co = make_company('Econ Co')
        cls.user = make_user('econ_u', cls.co)
        cls.cust = make_customer(cls.co, 'econ')
        cls.vt = make_vehicle_type(cls.co)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        q_out = priced_quote(self.co, self.cust, 'EC-OUT', vt=self.vt, origin='Johannesburg', destination='Durban',
                             total='30000')
        q_ret = priced_quote(self.co, self.cust, 'EC-RET', vt=self.vt, origin='Durban', destination='Johannesburg',
                             total='26000')
        self.out = Load.objects.get(pk=self.api.post(f'/api/v1/quotes/{q_out.id}/convert_to_load/',
                                                     {'pickup_date': '2026-11-02', 'delivery_date': '2026-11-03'},
                                                     format='json').json()['id'])
        self.ret = Load.objects.get(pk=self.api.post(f'/api/v1/quotes/{q_ret.id}/convert_to_load/',
                                                     {'pickup_date': '2026-11-04', 'delivery_date': '2026-11-05'},
                                                     format='json').json()['id'])
        self.q_out, self.q_ret = q_out, q_ret


class EstimateTests(_Pair):
    def test_standalone_estimate_is_the_quote_floor_with_empty_return(self):
        est = te.estimate(self.out)
        self.assertEqual(est['basis'], 'snapshot')
        self.assertEqual(est['estimated_cost'], self.q_out.cost_floor)
        self.assertGreater(_sum(self.out.costing_snapshot['lines'], 'empty_return'), 0)

    def test_linking_drops_the_empty_leg_on_both_legs(self):
        empty_out = _sum(self.out.costing_snapshot['lines'], 'empty_return')
        empty_ret = _sum(self.ret.costing_snapshot['lines'], 'empty_return')
        link_return(self.out, self.ret)
        for load, empty, q in ((self.out, empty_out, self.q_out), (self.ret, empty_ret, self.q_ret)):
            load.refresh_from_db()
            est = te.estimate(load)
            self.assertEqual(est['basis'], 'snapshot_return_linked')
            self.assertEqual(est['estimated_cost'], (q.cost_floor - empty).quantize(Decimal('0.01')))
            self.assertTrue(all(ln['leg'] == 'loaded' for ln in est['lines']))
            self.assertEqual(est['empty_return_removed'], empty)

    def test_pair_view_combined_and_quoted_vs_actual(self):
        link_return(self.out, self.ret)
        body = self.api.get(f'/api/v1/loads/{self.ret.id}/economics/').json()
        self.assertTrue(body['pair'])
        out_leg, ret_leg = body['legs']
        self.assertEqual((out_leg['role'], ret_leg['role']), ('outbound', 'return'))
        self.assertEqual(out_leg['revenue'], 30000.0)
        self.assertEqual(ret_leg['revenue'], 26000.0)
        self.assertEqual(out_leg['cost_basis'], 'estimate')
        c = body['combined']
        self.assertEqual(c['revenue'], 56000.0)
        self.assertAlmostEqual(c['cost'], out_leg['estimated_cost'] + ret_leg['estimated_cost'], places=2)
        q_floor = float(self.q_out.cost_floor + self.q_ret.cost_floor)
        self.assertAlmostEqual(c['quoted']['cost_floor'], q_floor, places=2)
        self.assertAlmostEqual(c['quoted']['margin_pct'], (56000 - q_floor) / 56000 * 100, places=2)
        # Found a backhaul: the real margin beats what was quoted.
        self.assertGreater(c['margin_vs_quoted_pts'], 0)
        self.assertGreater(c['empty_return_removed'], 0)
        self.assertEqual(body['costing']['source'], 'quote')

    def test_actual_expenses_win_per_leg(self):
        link_return(self.out, self.ret)
        Expense.objects.create(company=self.co, expense_number='EX-ECON-1', category='FUEL', description='Diesel',
                               amount=Decimal('11500'), vat_amount=Decimal('1500'), load=self.out,
                               expense_date=date.today(), status='APPROVED')
        body = te.economics_for_load_id(self.out.id)
        out_leg, ret_leg = body['legs']
        self.assertEqual(out_leg['cost_basis'], 'actual')
        self.assertEqual(out_leg['cost'], 10000.0)
        self.assertIsNotNone(out_leg['estimated_cost'])
        self.assertEqual(ret_leg['cost_basis'], 'estimate')
        self.assertEqual(body['combined']['cost_basis'], 'mixed')

    def test_invoice_revenue_wins(self):
        Invoice.objects.create(company=self.co, customer=self.cust, load=self.out, invoice_number='INV-ECON-1',
                               issue_date=date.today(), due_date=date.today(), subtotal=Decimal('28000'),
                               status='SENT')
        row = load_economics(self.co, [self.out])[self.out.id]
        self.assertEqual(row['revenue_basis'], 'actual')
        self.assertEqual(row['revenue'], Decimal('28000.00'))

    def test_incomplete_snapshot_has_no_partial_estimate(self):
        snap = dict(self.out.costing_snapshot)
        snap['lines'] = [dict(ln, amount=None) if ln['key'] == 'tolls' else ln for ln in snap['lines']]
        Load.objects.filter(pk=self.out.pk).update(costing_snapshot=snap)
        self.out.refresh_from_db()
        est = te.estimate(self.out)
        self.assertEqual((est['basis'], est['estimated_cost']), ('snapshot_incomplete', None))

    def test_reports_use_the_pair_estimate(self):
        link_return(self.out, self.ret)
        Load.objects.filter(pk__in=[self.out.pk, self.ret.pk]).update(status='DELIVERED')
        econ = load_economics(self.co, Load.objects.filter(pk__in=[self.out.pk, self.ret.pk]))
        self.assertEqual({r['estimate_basis'] for r in econ.values()}, {'snapshot_return_linked'})
        lanes = margin_by_lane(self.co, include_loads=True)['lanes']
        rows = [r for lane in lanes for r in lane['load_rows']]
        self.assertTrue(all(r['return_pair'] for r in rows))
        self.out.refresh_from_db()
        self.ret.refresh_from_db()
        self.assertEqual(sum(r['cost_excl_vat'] for r in rows),
                         float(te.estimate(self.out)['estimated_cost'] + te.estimate(self.ret)['estimated_cost']))

    def test_cache_recomputed_on_link_unlink_and_expense(self):
        with self.captureOnCommitCallbacks(execute=True):
            link_return(self.out, self.ret)
        self.out.refresh_from_db()
        self.assertEqual(self.out.estimate_basis, 'snapshot_return_linked')
        self.assertEqual(self.out.estimated_cost, te.estimate(self.out)['estimated_cost'])
        with self.captureOnCommitCallbacks(execute=True):
            unlink_return(self.out)
        self.out.refresh_from_db()
        self.ret.refresh_from_db()
        self.assertEqual((self.out.estimate_basis, self.ret.estimate_basis), ('snapshot', 'snapshot'))
        self.assertEqual(self.out.estimated_cost, self.q_out.cost_floor)
        # Idempotent.
        before = self.out.estimated_cost
        te.recompute([self.out.pk])
        te.recompute([self.out.pk])
        self.out.refresh_from_db()
        self.assertEqual(self.out.estimated_cost, before)

    def test_trip_cost_view_uses_load_estimate(self):
        link_return(self.out, self.ret)
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from core.models import Driver
        from core.tests.quote_rules_fixtures import add_vehicle
        du = get_user_model().objects.create_user(username='econ_drv', email='d@econ.test', password='x')
        driver = Driver.objects.create(company=self.co, user=du, license_number='ECON-1',
                                       license_expiry=date.today() + timedelta(days=300), license_state='GP',
                                       hire_date=date.today())
        trip = Trip.objects.create(load=self.out, vehicle=add_vehicle(self.co, self.vt), driver=driver,
                                   origin='Johannesburg', destination='Durban',
                                   distance_km=Decimal('600'), estimated_distance_km=Decimal('600'),
                                   estimated_duration_hours=Decimal('7'))
        body = self.api.get(f'/api/v1/trips/{trip.id}/costs/').json()
        self.assertEqual(body['estimate_basis'], 'snapshot_return_linked')
        self.assertEqual(Decimal(str(body['estimated_cost'])), te.estimate(self.out)['estimated_cost'])
        self.assertTrue(body['economics']['pair'])


class LegacyEstimateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        official_price_now()
        cls.co = make_company('Legacy Co')
        cls.cust = make_customer(cls.co, 'leg')

    def test_legacy_deadhead_only_without_snapshot_and_labelled(self):
        a = make_load(self.co, self.cust, 'LEG-A', pickup='Johannesburg', delivery='Durban', distance='600')
        b = make_load(self.co, self.cust, 'LEG-B', pickup='Durban', delivery='Johannesburg', distance='600',
                      pickup_in_days=4)
        single = te.estimate(a)
        self.assertEqual(single['basis'], 'legacy_deadhead')
        self.assertIn('1,3', single['label'])
        link_return(a, b)
        a.refresh_from_db()
        paired = te.estimate(a)
        self.assertEqual(paired['basis'], 'legacy_paired')
        self.assertAlmostEqual(float(paired['estimated_cost']) * 1.3, float(single['estimated_cost']), delta=1)

    def test_no_distance_is_unknown(self):
        a = make_load(self.co, self.cust, 'LEG-C', distance='0')
        self.assertEqual(te.estimate(a)['basis'], 'unknown')


class IntelligenceRouteTests(_Pair):
    def test_route_alert_uses_load_economics_and_pairs(self):
        from core.services.intelligence import IntelligenceService
        loads = []
        for i in range(3):
            q = priced_quote(self.co, self.cust, f'EC-LOW-{i}', vt=self.vt, origin='Pretoria', destination='Polokwane',
                             total='9000')
            load = Load.objects.get(pk=self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {},
                                                     format='json').json()['id'])
            Load.objects.filter(pk=load.pk).update(status='DELIVERED', pickup_city='Pretoria',
                                                   delivery_city='Polokwane')
            Invoice.objects.create(company=self.co, customer=self.cust, load=load, invoice_number=f'INV-LOW-{i}',
                                   issue_date=date.today(), due_date=date.today(), subtotal=Decimal('9000'),
                                   status='SENT')
            loads.append(load)
        alerts = [a for a in IntelligenceService(self.co)._check_route_pricing()]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['trip_count'], 3)
        self.assertEqual(alerts[0]['cost_basis'], 'estimate')
