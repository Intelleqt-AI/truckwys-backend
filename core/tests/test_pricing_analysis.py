"""POST /api/v1/quotes/pricing-analysis/ (core.services.pricing_analysis) and
the additive pieces around it: the company win-model tier and the global
opt-in, the shared margin definition, decision logging at save, loss
reasons, and the honesty fixes on the older endpoints."""
import time
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (Company, Customer, Expense, Quote, QuoteOutcome, QuotePricingDecision, Trip,
                         VehicleType, VerifiedRate)
from core.services import pricing_analysis as pa
from core.services import quote_training
from core.services.quote_ml import WIN_ML_AVAILABLE
from core.tests.test_price_analysis import IsolatedModelStorageMixin, make_quote
from core.tests.test_win_model_tiers import make_outcomes

User = get_user_model()
URL = '/api/v1/quotes/pricing-analysis/'


def make_customer(company, name='Kestrel Mills', email=None):
    return Customer.objects.create(company=company, name=name, email=email or f'{name.split()[0].lower()}@x.test',
                                   phone='', address='', city='', state='', zip_code='')


def base_payload(**over):
    """What QuoteBuilder sends: JHB->DBN, one way, the builder's own lines."""
    p = {
        'origin': 'JHB', 'destination': 'DBN', 'distance_km': 568, 'one_way_distance_km': 568, 'legs': 1,
        'trip_type': 'ONE_WAY', 'duration_minutes': 420, 'vehicle_type': 'Tautliner', 'weight': 28000,
        'fuel_cost': 6500, 'fuel_usage_litres': 216, 'fuel_price_used': 30.09,
        'fuel_consumption_l_per_100km': 38, 'fuel_type': 'Diesel', 'fuel_zone': 'INLAND',
        'toll_cost': 1200, 'route': {'toll_breakdown': [{'plaza': 'Wilge', 'tariff': 600},
                                                         {'plaza': 'Tugela', 'tariff': 600}]},
        'cross_border_cost': 0, 'driver_cost': 0,
    }
    p.update(over)
    return p


def won_quotes(company, customer, n, *, start=24000, step=500, origin='JHB', destination='DBN', prefix='W'):
    out = []
    for i in range(n):
        out.append(make_quote(company, customer, number=f'{prefix}-{company.id}-{i}', total=start + i * step,
                              origin=origin, destination=destination, status='ACCEPTED', outcome='accepted',
                              pickup_location='Johannesburg', delivery_location='Durban'))
    return out


class _Base(IsolatedModelStorageMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.company = Company.objects.create(company_name='Bluegum Haulage', margin_target_pct=Decimal('10'))
        self.user = User.objects.create_user(username='bluegum', password='x', company=self.company)
        self.customer = make_customer(self.company)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def analyze(self, **over):
        return pa.analyze_pricing(base_payload(**over), company=self.company, user=self.user)

    def post(self, **over):
        resp = self.api.post(URL, base_payload(**over), format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()


class ColdStartTests(_Base):
    def test_no_market_no_model_gives_rules_level_and_estimate_labels(self):
        r = self.post(origin='BFN', destination='PLK', your_price=12000, customer_id=self.customer.id)
        self.assertTrue(r['success'])
        self.assertEqual(r['version'], 'pa-1')
        self.assertEqual(r['market']['tier'], 'none')
        self.assertFalse(r['market']['available'])
        self.assertEqual(r['likelihood']['level'], 'rules')
        self.assertIsNone(r['likelihood']['model'])
        self.assertIn('Not enough closed quotes yet: 0 of 40', r['likelihood']['reason'])
        codes = {w['code'] for w in r['warnings']}
        self.assertIn('no_market', codes)
        self.assertIn('estimate_fixed_cost', codes)
        fixed = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'fixed_cost')
        self.assertEqual(fixed['source']['kind'], 'estimate')
        self.assertEqual(r['cost_floor']['fixed_cost_per_km']['source'], 'vehicle_default')
        # Ladder without market: target, +8pp, +16pp (rounded up, so >=).
        floor = r['cost_floor']['total']
        margins = [c['margin_pct'] for c in r['choices']]
        self.assertGreaterEqual(margins[0], 10)
        self.assertGreaterEqual(margins[1], 18)
        self.assertGreaterEqual(margins[2], 26)
        for c in r['choices']:
            # Never a % at rules level; no thresholds without market or history.
            self.assertEqual(c['likelihood']['level'], 'rules')
            self.assertNotIn('pct', c['likelihood'])
            self.assertIsNone(c['likelihood']['band'])
            self.assertEqual(c['margin'], c['price'] - floor)
        self.assertTrue(all(c['price'] % 50 == 0 for c in r['choices']))
        self.assertEqual(sum(ln['amount'] for ln in r['cost_floor']['lines']), floor)

    def test_estimate_lane_is_labelled_and_not_priced_from(self):
        r = self.analyze(your_price=25000)
        m = r['market']
        self.assertEqual(m['tier'], 'estimate')
        self.assertTrue(m['is_estimate'])
        self.assertNotIn('competitive', m['tier_label'].lower())
        self.assertIn('estimate', m['tier_label'].lower())
        self.assertIn('estimate_market', {w['code'] for w in r['warnings']})
        # Choices come from the target ladder, not the (low) estimate range.
        floor = r['cost_floor']['total']
        self.assertEqual(r['choices'][0]['price'], pa.round_price(floor / 0.9))
        self.assertIsNone(r['likelihood']['rules'])
        self.assertTrue(all('competitive' not in s.lower() for s in r['reasoning']))

    def test_missing_route_is_progressive_not_an_error(self):
        r = self.post(distance_km=0, fuel_cost=None, toll_cost=None, your_price=None, vehicle_type='')
        self.assertTrue(r['success'])
        self.assertEqual(r['missing'][0], 'route')
        self.assertIn('vehicle', r['missing'])
        self.assertIn('customer', r['missing'])
        self.assertIsNone(r['cost_floor'])
        self.assertEqual(r['choices'], [])
        self.assertIn('no_route', {w['code'] for w in r['warnings']})

    def test_unauthenticated_is_refused(self):
        self.assertEqual(APIClient().post(URL, base_payload(), format='json').status_code, 401)


class CostFloorTests(_Base):
    def test_builder_lines_are_consumed_not_recomputed(self):
        r = self.analyze()
        lines = {ln['key']: ln for ln in r['cost_floor']['lines']}
        self.assertEqual(lines['fuel']['amount'], 6500)
        self.assertEqual(lines['tolls']['amount'], 1200)
        self.assertEqual(lines['tolls']['source']['kind'], 'official')
        self.assertIn('SANRAL', lines['tolls']['source']['label'])
        self.assertEqual(lines['fixed_cost']['amount'], round(568 * 4.60))
        self.assertNotIn('return_leg', lines)
        self.assertFalse(r['cost_floor']['include_return'])
        for ln in lines.values():
            self.assertTrue({'key', 'label', 'amount', 'source', 'basis', 'editable', 'details'} <= set(ln))

    def test_fixed_cost_from_company_actuals_with_ten_trips(self):
        from core.models import Driver, Load, Vehicle
        vt = VehicleType.objects.create(name='CostTruck', capacity=Decimal('34'), max_distance=Decimal('2000'),
                                        base_rate=Decimal('15'))
        vehicle = Vehicle.objects.create(company=self.company, vin='PAVIN1', plate='PA001GP', vehicle_type=vt,
                                         make='Merc', model='Actros', year=2020, type='Truck',
                                         capacity=Decimal('34'), fuel_type='Diesel', status='AVAILABLE')
        du = User.objects.create_user(username='pa_driver', email='d@pa.test', password='x')
        driver = Driver.objects.create(company=self.company, user=du, license_number='PA-1',
                                       license_expiry=date.today() + timedelta(days=365), license_state='GP',
                                       hire_date=date.today() - timedelta(days=365))
        for i in range(10):
            load = Load.objects.create(
                load_number=f'L-{i}', company=self.company, customer=self.customer, pickup_location='A',
                pickup_city='A', pickup_state='', pickup_zip='', pickup_date=timezone.now(),
                delivery_location='B', delivery_city='B', delivery_state='', delivery_zip='',
                delivery_date=timezone.now(), cargo_description='x', weight=1, rate=1, total_amount=1)
            trip = Trip.objects.create(load=load, vehicle=vehicle, driver=driver, status='COMPLETED',
                                       distance_km=Decimal('500'),
                                       estimated_distance_km=Decimal('500'), estimated_duration_hours=Decimal('6'),
                                       origin='A', destination='B', start_time=timezone.now())
            Expense.objects.create(company=self.company, expense_number=f'E-{i}', category='MAINTENANCE',
                                   description='m', amount=Decimal('4600'), vat_amount=Decimal('600'),
                                   expense_date=date.today(), trip=trip, status='APPROVED')
            Expense.objects.create(company=self.company, expense_number=f'EF-{i}', category='FUEL',
                                   description='f', amount=Decimal('9000'), expense_date=date.today(), trip=trip)
        cache.clear()
        r = self.analyze()
        fixed = r['cost_floor']['fixed_cost_per_km']
        # (4600 - 600) / 500 km: fuel excluded (its own line), VAT excluded.
        self.assertEqual(fixed['source'], 'company_actuals')
        self.assertEqual(fixed['trips'], 10)
        self.assertAlmostEqual(fixed['value'], 8.0)
        self.assertNotIn('estimate_fixed_cost', {w['code'] for w in r['warnings']})

    def test_driver_allowance_defaults_to_approved_rate_times_nights(self):
        VerifiedRate.objects.create(kind='driver_allowance', key='nbcrfli', label='NBCRFLI', value=Decimal('500'),
                                    unit='per_night', effective_from=date(date.today().year, 3, 1)
                                    if date.today() >= date(date.today().year, 3, 1) else date(date.today().year - 1, 3, 1),
                                    status='approved', source_url='https://example.test/nbcrfli')
        # 20 h one way -> 3 driving days -> 2 nights away.
        r = self.analyze(duration_minutes=1200)
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'driver_allowance')
        self.assertEqual(line['suggested'], 1000)
        self.assertEqual(line['amount'], 1000)
        self.assertEqual(line['source']['kind'], 'official')
        self.assertTrue(line['editable'])
        # The operator's own figure wins, and the approved one stays visible.
        r = self.analyze(duration_minutes=1200, driver_cost=700)
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'driver_allowance')
        self.assertEqual(line['amount'], 700)
        self.assertEqual(line['source']['kind'], 'user')
        self.assertEqual(line['suggested'], 1000)

    def test_no_approved_allowance_warns(self):
        r = self.analyze()
        self.assertIn('no_driver_allowance', {w['code'] for w in r['warnings']})

    def test_empty_return_toggle_and_company_default(self):
        VehicleType.objects.create(company=self.company, name='Tautliner', capacity=Decimal('34'),
                                   max_distance=Decimal('2000'), base_rate=Decimal('20'),
                                   fuel_consumption_l_per_100km=Decimal('38'))
        off = self.analyze()
        on = self.analyze(include_return=True)
        ret = next(ln for ln in on['cost_floor']['lines'] if ln['key'] == 'return_leg')
        self.assertTrue(on['cost_floor']['include_return'])
        self.assertEqual(on['cost_floor']['total'], off['cost_floor']['total'] + ret['amount'])
        # Empty running burns less than the loaded leg; tolls the same plazas.
        self.assertLess(ret['amount'], off['cost_floor']['total'])
        self.assertIn('back empty', ret['basis'])
        self.company.pricing_include_empty_return = True
        self.company.save()
        self.assertTrue(self.analyze()['cost_floor']['include_return'])
        self.assertFalse(self.analyze(include_return=False)['cost_floor']['include_return'])
        # Round trips never add a second return.
        self.assertFalse(self.analyze(legs=2, include_return=True)['cost_floor']['include_return'])


class RulesWithMarketTests(_Base):
    def setUp(self):
        super().setUp()
        other = Company.objects.create(company_name='Ridgeback Freight')
        other_cust = make_customer(other, 'Saltpan Traders')
        won_quotes(self.company, self.customer, 3, start=24000, prefix='A')
        won_quotes(other, other_cust, 4, start=25500, prefix='B')

    def test_platform_tier_drives_choices_and_bands(self):
        r = self.analyze(your_price=26000, customer_id=self.customer.id)
        m = r['market']
        self.assertEqual(m['tier'], 'platform')
        self.assertFalse(m['is_estimate'])
        self.assertEqual(m['n'], 7)
        safe, balanced, stretch = r['choices']
        self.assertGreaterEqual(safe['price'], m['p25'])
        self.assertGreaterEqual(balanced['price'], m['median'])
        self.assertGreater(stretch['price'], balanced['price'])
        self.assertTrue(balanced['recommended'])
        th = r['likelihood']['rules']['thresholds']
        self.assertEqual(th['likely_max'], m['median'])
        self.assertEqual(th['even_max'], m['p75'])
        self.assertEqual(r['likelihood']['level'], 'rules')
        self.assertEqual(safe['likelihood']['band'], 'likely')
        self.assertEqual(r['your_price']['market_position'], 'within')
        # Customer evidence: their own lane quotes, newest first.
        self.assertEqual(len(r['customer']['recent_lane_quotes']), 3)
        self.assertEqual(r['customer']['acceptance'], {'won': 3, 'decided': 3, 'rate_pct': 100})
        self.assertIn(r['customer']['payment_risk']['band'], ('low', 'medium', 'high', 'unknown'))

    def test_below_floor_warning(self):
        r = self.analyze(your_price=5000)
        self.assertTrue(r['your_price']['below_floor'])
        self.assertIn('below_floor', {w['code'] for w in r['warnings']})
        self.assertLess(r['your_price']['margin'], 0)


class TenantIsolationTests(_Base):
    def test_company_tier_and_customer_evidence_never_cross_tenants(self):
        other = Company.objects.create(company_name='Ridgeback Freight')
        other_user = User.objects.create_user(username='ridge', password='x', company=other)
        other_cust = make_customer(other, 'Saltpan Traders')
        won_quotes(other, other_cust, 8, prefix='B')   # one operator only: no platform tier
        # Company B sees its own quotes as a company-tier market...
        rb = pa.analyze_pricing(base_payload(customer_id=other_cust.id), company=other, user=other_user)
        self.assertEqual(rb['market']['tier'], 'company')
        self.assertEqual(rb['market']['n'], 8)
        # ...company A sees none of them (k-anonymity: one operator), and
        # can't read B's customer by id.
        ra = self.analyze(customer_id=other_cust.id)
        self.assertEqual(ra['market']['tier'], 'estimate')
        self.assertIsNone(ra['customer'])
        self.assertIn('customer', ra['missing'])


class MarginConsistencyTests(_Base):
    def test_one_margin_definition_everywhere(self):
        r = self.post(your_price=26000)
        floor = r['cost_floor']['total']
        for c in r['choices']:
            self.assertEqual(c['margin'], c['price'] - floor)
            self.assertEqual(c['margin_pct'], round((c['price'] - floor) / c['price'] * 100))
        self.assertEqual(r['your_price']['margin'], 26000 - floor)
        # The guard's additive floor fields agree when given the same inputs.
        direct = 6500 + 1200 + 0 + 0   # fuel + tolls + driver + border
        g = self.api.post('/api/v1/quotes/guard/', {'total_cost': direct, 'quote_price': 26000,
                                                     'distance_km': 568, 'customer_id': self.customer.id},
                          format='json').json()
        self.assertEqual(g['full_cost_floor'], floor)
        self.assertEqual(g['margin_vs_floor'], r['your_price']['margin'])
        self.assertEqual(g['margin_floor_pct'], r['your_price']['margin_pct'])
        # Old fields keep their meaning (direct-cost margin) for existing clients.
        self.assertAlmostEqual(g['margin_pct'], round((26000 - direct) / 26000 * 100, 2))


class _ModelMixin:
    def train_company_model(self, company, user, customer, n=40):
        make_outcomes(company, customer, user, n, prefix=f'M{company.id}')
        result = quote_training.retrain_win_model_for_scope('company', company_id=company.id)
        self.assertTrue(result.get('trained'), result)
        from core.services.quote_ml import _MODEL_CACHE
        _MODEL_CACHE.clear()

    def model_payload(self, **over):
        # JHB->CPT: the lane make_outcomes prices (16k-29k against the 38,900
        # SA estimate the training market rate resolves to).
        p = dict(origin='JHB', destination='CPT', fuel_cost=9000, toll_cost=1500, distance_km=1400,
                 one_way_distance_km=1400, duration_minutes=480, vehicle_type='', route={})
        p.update(over)
        return p


class ModelLevelTests(_ModelMixin, _Base):
    def setUp(self):
        super().setUp()
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn not installed')
        self.train_company_model(self.company, self.user, self.customer)

    def test_company_model_gives_percentages_and_a_curve(self):
        # Floor ~ 9000 + 1500 + 1400*4.6 = 16,940 -> choices ~ 18.8k-22.7k.
        r = self.analyze(**self.model_payload(your_price=21000, customer_id=self.customer.id))
        lk = r['likelihood']
        self.assertEqual(lk['level'], 'model', lk.get('reason'))
        self.assertEqual(lk['model']['scope'], 'company')
        self.assertEqual(lk['model']['n_closed'], 40)
        self.assertTrue(lk['model']['version'].startswith('company:'))
        self.assertIn('your company', lk['model']['basis_label'])
        curve = lk['model']['curve']
        self.assertTrue(3 <= len(curve) <= 25)
        self.assertGreaterEqual(curve[0]['pct'], curve[-1]['pct'])
        floor = r['cost_floor']['total']
        for pt in curve:
            # expected_profit = P(win) x margin (from the unrounded probability).
            self.assertAlmostEqual(pt['expected_profit'], pt['pct'] / 100 * (pt['price'] - floor), delta=0.01 * pt['price'])
        model_choices = [c for c in r['choices'] if c['likelihood']['level'] == 'model']
        self.assertTrue(model_choices)
        for c in model_choices:
            self.assertTrue(0 <= c['likelihood']['pct'] <= 100)
        self.assertEqual(sum(1 for c in r['choices'] if c['recommended']), 1)
        self.assertIn(r['your_price']['likelihood']['level'], ('model', 'rules'))

    def test_out_of_range_falls_back_to_rules(self):
        # A floor far above anything the model has seen on this lane.
        r = self.analyze(**self.model_payload(fuel_cost=90000, your_price=120000))
        self.assertEqual(r['likelihood']['level'], 'rules')
        self.assertIsNone(r['likelihood']['model'])
        for c in r['choices']:
            self.assertNotIn('pct', c['likelihood'])
        self.assertNotIn('pct', r['your_price']['likelihood'])

    def test_other_company_never_gets_this_model(self):
        other = Company.objects.create(company_name='Ridgeback Freight', pool_pricing_data=True)
        other_user = User.objects.create_user(username='ridge2', password='x', company=other)
        r = pa.analyze_pricing(base_payload(**self.model_payload()), company=other, user=other_user)
        self.assertEqual(r['likelihood']['level'], 'rules')

    def test_nightly_sweep_skips_unchanged_companies(self):
        summary = quote_training.retrain_company_win_models()
        self.assertEqual(summary['considered'], 1)
        self.assertEqual(summary['skipped'], 1)
        self.assertEqual(summary['trained'], 0)


class GlobalOptInTests(_ModelMixin, _Base):
    def setUp(self):
        super().setUp()
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn not installed')
        donor = Company.objects.create(company_name='Donor Logistics', pool_pricing_data=True)
        donor_user = User.objects.create_user(username='donor', password='x', company=donor)
        make_outcomes(donor, make_customer(donor, 'Marula Co-op'), donor_user, 40, prefix='G')
        result = quote_training.retrain_win_model_for_scope('global')
        self.assertTrue(result.get('trained'), result)

    def test_global_model_only_for_opted_in_companies(self):
        r = self.analyze(**self.model_payload())
        self.assertEqual(r['likelihood']['level'], 'rules')
        self.company.pool_pricing_data = True
        self.company.save()
        from core.services.quote_ml import _MODEL_CACHE
        _MODEL_CACHE.clear()
        r = self.analyze(**self.model_payload())
        self.assertEqual(r['likelihood']['level'], 'model', r['likelihood'].get('reason'))
        self.assertEqual(r['likelihood']['model']['scope'], 'global')

    def test_global_training_pools_only_opted_in_companies(self):
        hidden = Company.objects.create(company_name='Private Co')
        hidden_user = User.objects.create_user(username='private', password='x', company=hidden)
        make_outcomes(hidden, make_customer(hidden, 'Quiet Ltd'), hidden_user, 10, prefix='H')
        _X, _y, n, _names = quote_training.build_win_training_matrix_for_scope('global')
        self.assertEqual(n, 40)


class _DecisionHelpers:
    def quote_payload(self, **over):
        p = {'customer': self.customer.id, 'pickup_location': 'Johannesburg', 'delivery_location': 'Durban',
             'cargo_description': 'Maize', 'weight': '28000', 'base_rate': '15000', 'fuel_surcharge': '6500',
             'toll_charges': '1200', 'total_amount': '22700',
             'valid_until': str(date.today() + timedelta(days=7))}
        p.update(over)
        return p

    def decision(self, **over):
        d = {'version': 'pa-1', 'shown_choices': [{'key': 'balanced', 'price': 22700}],
             'picked_choice': 'balanced', 'final_price': 22700, 'floor': 10313,
             'market': {'p25': 21000, 'median': 22700, 'p75': 24000, 'tier': 'platform', 'n': 9},
             'model_version': None, 'likelihood_level': 'rules', 'likelihood_at_final_pct': None,
             'band_at_final': 'likely'}
        d.update(over)
        return d


class DecisionLoggingTests(_DecisionHelpers, _Base):
    def test_decision_is_stored_and_returned_and_heuristic_never_saved(self):
        resp = self.api.post('/api/v1/quotes/', self.quote_payload(win_probability='80',
                                                                  pricing_decision=self.decision()), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        quote = Quote.objects.get(id=resp.json()['id'])
        self.assertIsNone(quote.win_probability)
        d = QuotePricingDecision.objects.get(quote=quote)
        self.assertEqual(d.picked_choice, 'balanced')
        self.assertEqual(d.market_tier, 'platform')
        self.assertEqual(d.likelihood_level, 'rules')
        detail = self.api.get(f'/api/v1/quotes/{quote.id}/').json()
        self.assertEqual(detail['pricing_decision']['picked_choice'], 'balanced')
        self.assertEqual(detail['pricing_decision']['band_at_final'], 'likely')
        self.assertIsNone(detail['win_probability'])
        # List pages don't carry it.
        listing = self.api.get('/api/v1/quotes/').json()
        rows = listing.get('results', listing)
        self.assertNotIn('pricing_decision', rows[0])

    def test_model_claim_without_a_model_stores_null(self):
        resp = self.api.post('/api/v1/quotes/', self.quote_payload(
            pricing_decision=self.decision(likelihood_level='model', likelihood_at_final_pct=72)), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(Quote.objects.get(id=resp.json()['id']).win_probability)

    def test_old_clients_without_decision_still_save(self):
        resp = self.api.post('/api/v1/quotes/', self.quote_payload(), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(self.api.get(f"/api/v1/quotes/{resp.json()['id']}/").json()['pricing_decision'])

    def test_invalid_decision_is_rejected(self):
        resp = self.api.post('/api/v1/quotes/', self.quote_payload(
            pricing_decision=self.decision(picked_choice='cheapest')), format='json')
        self.assertEqual(resp.status_code, 400)


class ModelDecisionTests(_ModelMixin, _DecisionHelpers, _Base):
    def test_model_level_sets_win_probability_on_update(self):
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn not installed')
        self.train_company_model(self.company, self.user, self.customer)
        resp = self.api.post('/api/v1/quotes/', self.quote_payload(), format='json')
        qid = resp.json()['id']
        resp = self.api.patch(f'/api/v1/quotes/{qid}/', {'pricing_decision': self.decision(
            likelihood_level='model', likelihood_at_final_pct=64, model_version='company:1:x')}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Quote.objects.get(id=qid).win_probability, Decimal('64'))
        # Back to rules level -> cleared, never left at an old figure.
        self.api.patch(f'/api/v1/quotes/{qid}/', {'pricing_decision': self.decision()}, format='json')
        self.assertIsNone(Quote.objects.get(id=qid).win_probability)


class LossReasonAndOutcomeTests(_Base):
    def test_outcome_endpoint_records_loss_reason(self):
        q = make_quote(self.company, self.customer, number='LR-1', created_by=self.user)
        resp = self.api.patch(f'/api/v1/quotes/{q.id}/outcome/', {'outcome': 'rejected', 'loss_reason': 'price',
                                                                   'loss_reason_note': 'R2k cheaper'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        o = QuoteOutcome.objects.get(quote=q)
        self.assertEqual((o.loss_reason, o.loss_reason_note), ('price', 'R2k cheaper'))
        self.assertEqual(self.api.get(f'/api/v1/quotes/{q.id}/').json()['loss_reason'],
                         {'reason': 'price', 'note': 'R2k cheaper'})

    def test_unknown_loss_reason_is_dropped(self):
        q = make_quote(self.company, self.customer, number='LR-2', created_by=self.user)
        self.api.patch(f'/api/v1/quotes/{q.id}/outcome/', {'outcome': 'rejected', 'loss_reason': 'weather'},
                       format='json')
        self.assertEqual(QuoteOutcome.objects.get(quote=q).loss_reason, '')

    def test_plain_status_patch_records_the_outcome(self):
        q = make_quote(self.company, self.customer, number='LR-3', created_by=self.user)
        self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'ACCEPTED'}, format='json')
        q.refresh_from_db()
        self.assertEqual(q.outcome, 'accepted')
        q2 = make_quote(self.company, self.customer, number='LR-4', created_by=self.user)
        self.api.patch(f'/api/v1/quotes/{q2.id}/', {'status': 'DECLINED', 'loss_reason': 'timing'}, format='json')
        self.assertEqual(QuoteOutcome.objects.get(quote=q2).loss_reason, 'timing')

    def test_public_decline_with_reason_and_draft_is_blocked(self):
        q = make_quote(self.company, self.customer, number='LR-5')
        resp = APIClient().post(f'/api/v1/quotes/public/{q.id}/{q.token}/respond/',
                                {'action': 'decline', 'loss_reason': 'capacity'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(QuoteOutcome.objects.get(quote=q).loss_reason, 'capacity')
        draft = make_quote(self.company, self.customer, number='LR-6', status='DRAFT')
        resp = APIClient().post(f'/api/v1/quotes/public/{draft.id}/{draft.token}/respond/',
                                {'action': 'accept'}, format='json')
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()['not_sent'])
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'DRAFT')
        view = APIClient().get(f'/api/v1/quotes/public/{draft.id}/{draft.token}/').json()
        self.assertFalse(view['can_respond'])


class ConvertToLoadTests(_Base):
    def test_tolls_driver_and_province_carry_over_total_unchanged(self):
        q = make_quote(self.company, self.customer, number='CV-1', created_by=self.user, status='ACCEPTED',
                       origin='CPT', destination='JHB', pickup_location='Cape Town', delivery_location='Johannesburg',
                       total=25000)
        self.api.patch(f'/api/v1/quotes/{q.id}/outcome/', {'outcome': 'accepted', 'final_price': '24000'},
                       format='json')
        resp = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        load = resp.json()
        self.assertEqual(Decimal(load['toll_charges']), Decimal('1500'))
        self.assertEqual(Decimal(load['driver_allowance']), Decimal('1000'))
        # The booking keeps the quote total exactly as on main (billing the
        # agreed price is a deferred owner decision).
        self.assertEqual(Decimal(load['total_amount']), Decimal('25000'))
        self.assertEqual(Decimal(load['rate']), Decimal('5000'))
        self.assertEqual((load['pickup_city'], load['pickup_state']), ('Cape Town', 'WC'))
        self.assertEqual((load['delivery_city'], load['delivery_state']), ('Johannesburg', 'GP'))


class HonestyFixTests(_Base):
    def test_benchmark_without_your_rate_or_from_estimate_is_never_competitive(self):
        r = self.api.get('/api/v1/quotes/benchmark/?origin=JHB&destination=DBN&vehicle_type=truck').json()
        self.assertEqual(r['source'], 'estimate')
        self.assertTrue(r['is_estimate'])
        self.assertIsNone(r['your_rate'])
        self.assertIsNone(r['your_vs_market_pct'])
        self.assertNotIn('competitive', r['recommendation'].lower())
        r = self.api.get('/api/v1/quotes/benchmark/?origin=JHB&destination=DBN&vehicle_type=truck'
                         '&your_rate=15000').json()
        self.assertNotIn('competitive', r['recommendation'].lower())
        self.assertEqual(r['your_vs_market_pct'], 0.0)

    def test_win_probability_endpoint_says_heuristic(self):
        r = self.api.post('/api/v1/quotes/win-probability/', {'price': 20000, 'client_id': self.customer.id,
                                                               'origin': 'JHB', 'destination': 'DBN'},
                          format='json').json()
        self.assertFalse(r['available'])
        self.assertEqual(r['level'], 'heuristic')
        self.assertIn('win_probability', r)

    def test_optimizer_flags_pure_heuristic(self):
        from core.services.margin_optimizer import optimize_price
        r = optimize_price(total_cost=10000, market_rate=13000)
        self.assertTrue(r['used_heuristic_fallback'])
        self.assertEqual(r['win_probability_source'], 'heuristic')

    def test_new_lane_codes(self):
        from core.services.lane_benchmark import derive_lane_code
        self.assertEqual(derive_lane_code('', 'Gaborone West, Botswana'), 'GBE')
        self.assertEqual(derive_lane_code('POL', 'Polokwane, Limpopo'), 'PLK')
        q = make_quote(self.company, self.customer, number='LC-1', origin='', destination='',
                       pickup_location='Pretoria', delivery_location='Maputo, Mozambique')
        self.assertEqual((q.origin, q.destination), ('PTA', 'MPM'))


class PerformanceTests(_Base):
    def test_typical_call_is_fast_on_seeded_data(self):
        other = Company.objects.create(company_name='Ridgeback Freight')
        other_cust = make_customer(other, 'Saltpan Traders')
        won_quotes(self.company, self.customer, 60, prefix='P')
        won_quotes(other, other_cust, 60, prefix='Q')
        for i in range(60):
            make_quote(self.company, self.customer, number=f'PO-{i}', origin='JHB', destination='DBN',
                       pickup_location='Johannesburg', delivery_location='Durban')
        payload = base_payload(customer_id=self.customer.id, your_price=26000)
        self.api.post(URL, payload, format='json')   # warm imports / caches
        timings = []
        for _ in range(5):
            t = time.monotonic()
            resp = self.api.post(URL, payload, format='json')
            timings.append((time.monotonic() - t) * 1000)
            self.assertEqual(resp.status_code, 200)
        timings.sort()
        # Loose: the target is < 150 ms typical; > 300 ms is a defect.
        self.assertLess(timings[len(timings) // 2], 300, timings)
        self.assertLess(resp.json()['computed_ms'], 300)


class RoundTripDocumentTests(_Base):
    def test_pdf_and_share_email_say_return_trip(self):
        from unittest import mock
        from core.services.quote_pdf import generate_quote_pdf_bytes
        q = make_quote(self.company, self.customer, number='RT-1', trip_type='ROUND_TRIP', distance=Decimal('568'))
        self.assertTrue(generate_quote_pdf_bytes(q).startswith(b'%PDF'))
        from core.services import email_service
        with mock.patch.object(email_service, '_quote_summary_box', wraps=email_service._quote_summary_box) as box, \
                mock.patch.object(email_service, 'send_email', return_value=True, create=True):
            try:
                email_service.send_quote_share_email(q, 'https://example.test/q')
            except Exception:
                pass
        rows = box.call_args[0][0]
        self.assertIn(('Trip', 'Return trip (there and back)'), rows)
