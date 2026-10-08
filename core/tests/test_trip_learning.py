"""Learning: actuals on QuoteOutcome after delivery; lane return-load share."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Expense, Load, QuoteOutcome
from core.services import trip_economics as te
from core.services.return_loads import link_return
from core.services.trip_learning import return_load_share
from core.tests.quote_rules_fixtures import add_vehicle, official_price_now
from core.tests.trip_fixtures import (make_company, make_customer, make_load, make_user, make_vehicle_type,
                                      priced_quote)


class ActualsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        official_price_now()
        cls.co = make_company('Learn Co')
        cls.user = make_user('learn_u', cls.co)
        cls.cust = make_customer(cls.co, 'learn')
        cls.vt = make_vehicle_type(cls.co)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.q = priced_quote(self.co, self.cust, 'LRN-1', vt=self.vt, origin='Johannesburg', destination='Durban')
        self.load = Load.objects.get(pk=self.api.post(f'/api/v1/quotes/{self.q.id}/convert_to_load/', {},
                                                      format='json').json()['id'])

    def test_actuals_written_on_delivery_with_known_costs(self):
        outcome = QuoteOutcome.objects.get(quote=self.q)
        stamp = (outcome.created_at, outcome.updated_at)
        Expense.objects.create(company=self.co, expense_number='EX-LRN-1', category='FUEL', description='Diesel',
                               amount=Decimal('20000'), vat_amount=Decimal('0'), load=self.load,
                               expense_date=date.today(), status='APPROVED')
        with self.captureOnCommitCallbacks(execute=True):
            self.load.status = 'DELIVERED'
            self.load.save()
        outcome.refresh_from_db()
        # Only fuel recorded: not complete -> no actual_*, the estimate so far.
        self.assertIsNone(outcome.actual_cost)
        self.assertIsNone(outcome.actual_margin_pct)
        self.assertEqual(outcome.actual_cost_basis, 'part_actual')
        self.assertIsNotNone(outcome.estimated_cost)
        self.assertIs(outcome.backhaul_found, False)
        # The key costs recorded (tolls; driver when the estimate has one): complete.
        Expense.objects.create(company=self.co, expense_number='EX-LRN-2', category='TOLLS', description='Tolls',
                               amount=Decimal('900'), vat_amount=Decimal('0'), load=self.load,
                               expense_date=date.today(), status='APPROVED')
        Expense.objects.create(company=self.co, expense_number='EX-LRN-3', category='DRIVER_COST',
                               description='Allowance', amount=Decimal('100'), vat_amount=Decimal('0'),
                               load=self.load, expense_date=date.today(), status='APPROVED')
        Expense.objects.create(company=self.co, expense_number='EX-LRN-4', category='MAINTENANCE',
                               description='Tyres', amount=Decimal('1000'), vat_amount=Decimal('0'),
                               load=self.load, expense_date=date.today(), status='APPROVED')
        te.recompute([self.load.pk])
        outcome.refresh_from_db()
        # Complete (key costs recorded) but the running cost is still the
        # estimate: the same 'part_actual' the job card shows.
        self.assertEqual(outcome.actual_cost_basis, 'part_actual')
        leg = te.economics_for_load_id(self.load.pk)['legs'][0]
        self.assertEqual((leg['cost_basis'], leg['cost_complete']), ('part_actual', True))
        self.assertEqual(outcome.actual_revenue, Decimal('30000.00'))
        self.assertIsNotNone(outcome.actual_cost)
        self.assertIsNone(outcome.estimated_cost)
        # Labels only: the outcome's dates (what features filter on) don't move.
        self.assertEqual((outcome.created_at, outcome.updated_at), stamp)

    def test_backhaul_found_when_a_return_is_linked(self):
        ret = make_load(self.co, self.cust, 'LRN-RET', pickup='Durban', delivery='Johannesburg', pickup_in_days=5)
        Load.objects.filter(pk=self.load.pk).update(status='DELIVERED')
        with self.captureOnCommitCallbacks(execute=True):
            link_return(self.load, ret)
        outcome = QuoteOutcome.objects.get(quote=self.q)
        self.assertIs(outcome.backhaul_found, True)
        self.assertEqual(outcome.actual_cost_basis, 'estimate')
        self.load.refresh_from_db()
        self.assertIsNone(outcome.actual_cost)
        self.assertEqual(outcome.estimated_cost, te.estimate(self.load)['estimated_cost'])

    def test_nothing_written_before_delivery(self):
        te.recompute([self.load.pk])
        self.assertIsNone(QuoteOutcome.objects.get(quote=self.q).actuals_recorded_at)


class LaneShareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = make_company('Share Co')
        cls.other = make_company('Share Other')
        cls.cust = make_customer(cls.co, 'share')
        cls.cust_o = make_customer(cls.other, 'share-o')

    def _trip(self, i, with_return, co=None, cust=None, days_ago=10):
        co, cust = co or self.co, cust or self.cust
        out = make_load(co, cust, f'SH-{co.pk}-{i}', pickup='Johannesburg', delivery='Durban', status='DELIVERED',
                        pickup_in_days=-days_ago - 1, days=1)
        if with_return:
            ret = make_load(co, cust, f'SH-{co.pk}-{i}-R', pickup='Durban', delivery='Johannesburg',
                            status='DELIVERED', pickup_in_days=-days_ago + 1, days=1)
            link_return(out, ret)
        return out

    def test_share_text_and_min_sample(self):
        for i in range(3):
            self._trip(i, i < 2)
        s = return_load_share(self.co, 'JHB', 'DBN')
        self.assertEqual((s['trips'], s['found'], s['enough'], s['share_pct']), (3, 2, False, None))
        for i in range(3, 6):
            self._trip(i, i == 3)
        s = return_load_share(self.co, 'JHB', 'DBN')
        self.assertEqual((s['trips'], s['found'], s['share_pct']), (6, 3, 50))
        self.assertEqual(s['text'], 'On this lane 50% of your trips found a return load (3 of 6).')
        # Company scoped; other lanes and old trips don't count.
        for i in range(6):
            self._trip(100 + i, True, co=self.other, cust=self.cust_o)
        self._trip(200, False, days_ago=400)
        self.assertEqual(return_load_share(self.co, 'JHB', 'DBN')['trips'], 6)
        self.assertEqual(return_load_share(self.co, 'DBN', 'JHB')['trips'], 0)

    def test_as_of_never_sees_later_links(self):
        for i in range(5):
            self._trip(i, True)
        past = timezone.now() - timedelta(days=1)
        Load.objects.filter(return_of__isnull=False).update(return_linked_at=timezone.now())
        s = return_load_share(self.co, 'JHB', 'DBN', as_of=past)
        self.assertEqual(s['found'], 0)

    def test_pricing_analysis_exposes_it_without_changing_the_default(self):
        from core.services.pricing_analysis import analyze_pricing
        official_price_now()
        vt = make_vehicle_type(self.co)
        add_vehicle(self.co, vt)
        for i in range(5):
            self._trip(i, i < 3)
        out = analyze_pricing({'origin': 'JHB', 'destination': 'DBN', 'distance_km': 600, 'weight': 10000,
                               'vehicle_type_id': vt.id, 'toll_cost': 800, 'duration_minutes': 420},
                              company=self.co)
        self.assertEqual(out['return_load_history']['text'],
                         'On this lane 60% of your trips found a return load (3 of 5).')
        self.assertTrue(out['cost_floor']['include_return'])
        self.assertEqual(out['alternative_with_return_load']['return_load_history']['found'], 3)


class QuoteDetailActualsTests(ActualsTests):
    def test_quote_detail_exposes_actuals_read_only(self):
        r = self.api.get(f'/api/v1/quotes/{self.q.id}/').json()
        self.assertIsNone(r['actuals'])
        with self.captureOnCommitCallbacks(execute=True):
            self.load.status = 'DELIVERED'
            self.load.save()
        r = self.api.get(f'/api/v1/quotes/{self.q.id}/').json()
        self.assertIs(r['actuals']['backhaul_found'], False)
        self.assertFalse(r['actuals']['complete'])
        self.assertIsNone(r['actuals']['actual_margin_pct'])
        self.assertIsNotNone(r['actuals']['estimated_margin_pct'])
        self.api.patch(f'/api/v1/quotes/{self.q.id}/', {'actuals': {'backhaul_found': True}}, format='json')
        self.assertIs(QuoteOutcome.objects.get(quote=self.q).backhaul_found, False)


class AnalyzeReturnHistoryTests(LaneShareTests):
    def test_quotes_analyze_includes_return_load_history(self):
        from unittest import mock
        official_price_now()
        vt = make_vehicle_type(self.co)
        add_vehicle(self.co, vt)
        for i in range(5):
            self._trip(i, i < 3)
        user = make_user('analyze_u', self.co)
        api = APIClient()
        api.force_authenticate(user)
        with mock.patch('core.services.quote_analysis._llm_narrative', return_value=None):
            r = api.post('/api/v1/quotes/analyze/', {
                'origin': 'JHB', 'destination': 'DBN', 'distance_km': 600, 'weight': 10000, 'quote_total': 30000,
                'vehicle_type_id': vt.id, 'toll_cost': 800, 'duration_minutes': 420, 'skip_narrative': True},
                format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['return_load_history']['text'],
                         'On this lane 60% of your trips found a return load (3 of 5).')
