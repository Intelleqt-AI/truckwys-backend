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


def won_quotes(company, customer, n, *, start=24000, step=500, origin='JHB', destination='DBN', prefix='W', **extra):
    extra.setdefault('was_sent', True)       # market / history evidence = quotes known to have been sent
    out = []
    for i in range(n):
        out.append(make_quote(company, customer, number=f'{prefix}-{company.id}-{i}', total=start + i * step,
                              origin=origin, destination=destination, status='ACCEPTED', outcome='accepted',
                              pickup_location='Johannesburg', delivery_location='Durban', **extra))
    return out


OTHER_OPERATORS = ('Ridgeback Freight', 'Saltpan Haulage', 'Quiver Tree Logistics')


def platform_market(company, customer, n_own=3, n_other=4, start=24000, **kw):
    """A platform market on the lane under the privacy rules: n_own quotes of
    the requesting company (never part of its platform range) and n_other
    won quotes from EACH of three other operators (>= 10 from >= 3 others)."""
    won_quotes(company, customer, n_own, start=start, prefix='A', **kw)
    others = []
    for i, name in enumerate(OTHER_OPERATORS):
        other = Company.objects.filter(company_name=name).first() or Company.objects.create(company_name=name)
        won_quotes(other, make_customer(other, f'{name} Customer'), n_other, start=start + 1500 + i * 250,
                   prefix=f'B{i}', **kw)
        others.append(other)
    return others


class _Base(IsolatedModelStorageMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        pa._MARKET_MEMO.clear()
        self.addCleanup(pa._MARKET_MEMO.clear)
        self.company = Company.objects.create(company_name='Bluegum Haulage', margin_target_pct=Decimal('10'),
                                              driver_allowance_per_night=Decimal('450'))
        self.user = User.objects.create_user(username='bluegum', password='x', company=self.company)
        self.customer = make_customer(self.company)
        # QUOTE-RULES.md: the floor is priced on an official diesel price in
        # force and the company's own vehicle type (never the client's lines).
        from core.tests.quote_rules_fixtures import official_price_now
        official_price_now()
        self.vt = VehicleType.objects.create(company=self.company, name='Tautliner', capacity=34, max_distance=3000,
                                             base_rate=20, fuel_consumption_l_per_100km=38)
        from core.tests.quote_rules_fixtures import add_vehicle
        add_vehicle(self.company, self.vt)
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
        self.assertIn('you have 0 won and 0 lost so far', r['likelihood']['reason'])
        # The level reason is not repeated in the reasoning sentences.
        self.assertFalse(any('0 so far' in t for t in r['reasoning']))
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
            self.assertAlmostEqual(c['margin'], c['price'] - floor, places=2)
        self.assertTrue(all(c['price'] % 50 == 0 for c in r['choices']))
        self.assertAlmostEqual(sum(ln['amount'] for ln in r['cost_floor']['lines']), floor, places=2)

    def test_estimate_lane_is_labelled_and_not_priced_from(self):
        r = self.analyze(your_price=25000)
        m = r['market']
        # No invented stats: the hard-coded SA lane table is not market data.
        self.assertEqual(m['tier'], 'none')
        self.assertFalse(m['available'])
        self.assertIn('no_market', {w['code'] for w in r['warnings']})
        # Choices come from the target ladder, not the (low) estimate range.
        floor = r['cost_floor']['total']
        self.assertEqual(r['choices'][0]['price'], pa.round_price(floor / 0.9))
        self.assertIsNone(r['likelihood']['rules'])
        self.assertTrue(all('competitive' not in s.lower() for s in r['reasoning']))

    def test_missing_route_is_progressive_not_an_error(self):
        r = self.post(distance_km=0, fuel_cost=None, toll_cost=None, your_price=None, vehicle_type='')
        self.assertTrue(r['success'])
        self.assertEqual(r['missing'][0], 'route')
        # QUOTE-RULES §3: no truck chosen -> the suggested truck for the load.
        self.assertNotIn('vehicle', r['missing'])
        self.assertEqual(r['costing']['resolution']['vehicle_selection'], 'suggested')
        self.assertIn('customer', r['missing'])
        self.assertIsNone(r['cost_floor'])
        self.assertEqual(r['choices'], [])
        self.assertIn('distance_missing', {w['code'] for w in r['warnings']})
        self.assertIn('distance_missing', r['blocking'])

    def test_unauthenticated_is_refused(self):
        self.assertEqual(APIClient().post(URL, base_payload(), format='json').status_code, 401)


class CostFloorTests(_Base):
    def test_floor_is_the_quote_rules_costing(self):
        # QUOTE-RULES.md: the builder's fuel figure is NOT consumed; fuel is
        # recomputed from the truck, load and the official price in force.
        from core.services.quote_costing import compute
        r = self.analyze(include_return=False)
        lines = {ln['key']: ln for ln in r['cost_floor']['lines']}
        costing = r['costing']
        self.assertEqual(r['cost_floor']['total'], compute(costing['inputs'])['floor'])
        litres = 568 * (38 * (0.70 + 0.30 * 28 / 34)) / 100
        self.assertAlmostEqual(lines['fuel']['litres'], litres)
        self.assertEqual(lines['fuel']['amount'], round(litres * 32.7989 + 1e-9, 2))
        self.assertEqual(lines['tolls']['amount'], 1200)
        # 'Tautliner' VehicleType -> tri-axle class estimate, R14.50/km.
        self.assertEqual(lines['fixed_cost']['amount'], round(568 * 14.50, 2))
        self.assertEqual(lines['fixed_cost']['label'], 'Operating costs')
        self.assertIn('tri-axle', lines['fixed_cost']['source']['label'])
        self.assertEqual(r['cost_floor']['fixed_cost_per_km']['class'], 'tri_axle')
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
        self.company.driver_allowance_per_night = None   # no company figure: the approved rate
        self.company.save()
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

    def test_no_allowance_warns_and_prices_nights_at_zero(self):
        self.company.driver_allowance_per_night = None
        self.company.save()
        r = self.analyze(duration_minutes=1200, include_return=False)
        w = next(w for w in r['warnings'] if w['code'] == 'driver_allowance_missing')
        self.assertEqual(w['severity'], 'warn')
        self.assertTrue(r['cost_floor']['complete'])
        r = self.analyze(duration_minutes=1200, include_return=False, driver_cost=900)
        self.assertNotIn('driver_allowance_missing', {w['code'] for w in r['warnings']})

    def test_empty_return_toggle_and_company_default(self):
        # QUOTE-RULES §5: one-way >= 300 km includes the empty return by default.
        self.assertTrue(self.analyze()['cost_floor']['include_return'])
        self.assertFalse(self.analyze(distance_km=250, one_way_distance_km=250)['cost_floor']['include_return'])
        off = self.analyze(include_return=False)
        on = self.analyze(include_return=True)
        ret = next(ln for ln in on['cost_floor']['lines'] if ln['key'] == 'return_leg')
        self.assertTrue(on['cost_floor']['include_return'])
        self.assertAlmostEqual(on['cost_floor']['total'], off['cost_floor']['total'] + ret['amount'], places=2)
        # Empty running burns less than the loaded leg; tolls the same plazas.
        self.assertLess(ret['amount'], off['cost_floor']['total'])
        self.assertIn('back empty', ret['basis'])
        self.company.include_empty_return_default = False
        self.company.save()
        self.assertFalse(self.analyze()['cost_floor']['include_return'])
        self.assertTrue(self.analyze(include_return=True)['cost_floor']['include_return'])
        self.assertFalse(self.analyze(include_return=False)['cost_floor']['include_return'])
        # Round trips never add a second return.
        self.assertFalse(self.analyze(legs=2, include_return=True)['cost_floor']['include_return'])


class RulesWithMarketTests(_Base):
    def setUp(self):
        super().setUp()
        platform_market(self.company, self.customer)

    def test_platform_tier_drives_choices_and_bands(self):
        r = self.analyze(your_price=26000, customer_id=self.customer.id, include_return=False)
        m = r['market']
        self.assertEqual(m['tier'], 'platform')
        self.assertFalse(m['is_estimate'])
        self.assertEqual(m['n'], 12)                  # the 3 other operators only (own quotes excluded)
        for k in ('p25', 'median', 'p75'):
            self.assertEqual(m[k] % 500, 0)            # privacy: R500 only
            self.assertNotIn('raw_' + k, m)
        safe, balanced, stretch = r['choices']
        # Safe sits at p25, or up to 3% under it in a tight market (to keep
        # Balanced at the median rather than pushing it above).
        self.assertGreaterEqual(safe['price'], m['p25'] * 0.97)
        self.assertGreaterEqual(balanced['price'], safe['price'] * 1.03 - 1)
        self.assertGreaterEqual(balanced['price'], m['median'])
        self.assertGreater(stretch['price'], balanced['price'])
        self.assertTrue(balanced['recommended'])
        th = r['likelihood']['rules']['thresholds']
        # R5 (K-5): edges rounded UP to R500, Even at least max(R1 000, 6% of the median) wide.
        raw = r['likelihood']['rules']['raw_thresholds']
        self.assertEqual(th['likely_max'], pa._ceil_to(raw['likely_max'], 500))
        self.assertEqual(th['even_max'], max(pa._ceil_to(raw['even_max'], 500),
                                             th['likely_max'] + pa._ceil_to(max(1000, 0.06 * m['median']), 500)))
        self.assertEqual(r['likelihood']['level'], 'rules')
        self.assertEqual(safe['likelihood']['band'], 'likely')
        self.assertEqual(r['your_price']['market_position'], 'within')
        # Customer evidence: their own lane quotes, newest first.
        self.assertEqual(len(r['customer']['recent_lane_quotes']), 3)
        self.assertEqual(r['customer']['acceptance'], {'won': 3, 'decided': 3, 'rate_pct': 100, 'scope': 'all_lanes',
                                                       'window_days': 180})
        self.assertEqual(r['customer']['lane_acceptance']['scope'], 'this_lane')
        # Balanced (the median, rounded up) sits in the median's band.
        self.assertEqual(balanced['likelihood']['band'], 'likely')
        self.assertLessEqual(balanced['price'], th['likely_max'])
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
        self.assertEqual(ra['market']['tier'], 'none')
        self.assertIsNone(ra['customer'])
        self.assertIn('customer', ra['missing'])


class MarginConsistencyTests(_Base):
    def test_one_margin_definition_everywhere(self):
        r = self.post(your_price=26000)
        floor = r['cost_floor']['total']
        for c in r['choices']:
            self.assertAlmostEqual(c['margin'], c['price'] - floor, places=2)
            self.assertEqual(c['margin_pct'], round((c['price'] - floor) / c['price'] * 100))
        self.assertAlmostEqual(r['your_price']['margin'], 26000 - floor, places=2)
        # The guard's additive floor fields use the same margin definition on
        # the direct cost it is given (+ operating cost per km).
        direct = 6500 + 1200 + 0 + 0   # fuel + tolls + driver + border
        g = self.api.post('/api/v1/quotes/guard/', {'total_cost': direct, 'quote_price': 26000,
                                                     'distance_km': 568, 'customer_id': self.customer.id},
                          format='json').json()
        self.assertEqual(g['margin_vs_floor'], pa.margin_against_floor(26000, g['full_cost_floor'])['margin'])
        self.assertEqual(g['margin_floor_pct'], pa.margin_against_floor(26000, g['full_cost_floor'])['margin_pct'])
        # Old fields keep their meaning (direct-cost margin) for existing clients.
        self.assertAlmostEqual(g['margin_pct'], (26000 - direct) / 26000 * 100)   # unrounded


class _ModelMixin:
    def train_company_model(self, company, user, customer, n=400):
        make_outcomes(company, customer, user, n, prefix=f'M{company.id}')
        self.no_market_range()
        result = quote_training.retrain_win_model_for_scope('company', company_id=company.id)
        self.assertTrue(result.get('trained'), result)
        from core.services.quote_ml import _MODEL_CACHE
        _MODEL_CACHE.clear()

    def no_market_range(self):
        # make_outcomes adds a real platform market on JHB->CPT (median 38 900)
        # as the win model's reference. These tests price from the cost floor
        # (as when that reference was the old, unusable SA estimate), so the
        # displayed market RANGE is left out; the model's reference stays.
        from unittest import mock
        none = {'available': False, 'tier': 'none', 'p25': None, 'median': None, 'p75': None, 'n': 0,
                'is_estimate': False, 'tier_label': 'No market data for this lane yet', 'window_days': None,
                'vehicle_specific': False}
        if getattr(self, '_no_range_patched', False):
            return
        patcher = mock.patch.object(pa, 'market_range', return_value=none)
        patcher.start()
        self._no_range_patched = True
        self.addCleanup(patcher.stop)

    def model_payload(self, **over):
        # JHB->CPT: the lane make_outcomes prices (16k-29k against the 38,900
        # SA estimate the training market rate resolves to).
        # Floor ≈ 5 000 + 700 + 1 000 km × R11,50 = R17 200 -> choices ≈ R19k–R23k.
        # QUOTE-RULES: a tri-axle at 15,55 L/100km rated, no empty return ->
        # floor ≈ R20 200 (fuel 4 998 + tolls 700 + 1 000 km × R14,50).
        VehicleType.objects.get_or_create(company=self.company, name='Model Tri-axle', defaults=dict(
            capacity=30, max_distance=3000, base_rate=10, fuel_consumption_l_per_100km=Decimal('15.55')))
        p = dict(origin='JHB', destination='CPT', fuel_cost=5000, toll_cost=700, distance_km=1000,
                 one_way_distance_km=1000, duration_minutes=480, vehicle_type='Model Tri-axle', route={},
                 include_return=False)
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
        self.assertEqual(lk['model']['n_closed'], 400)
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
        r = self.analyze(**self.model_payload(driver_cost=90000, your_price=120000))
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
        make_outcomes(donor, make_customer(donor, 'Marula Co-op'), donor_user, 400, prefix='G')
        self.no_market_range()
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
        self.assertEqual(n, 400)


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
        # The SERVER's band: no real market on this lane (estimate only) and no
        # customer history -> no band; the client's 'likely' is kept aside.
        self.assertIsNone(detail['pricing_decision']['band_at_final'])
        self.assertEqual(detail['pricing_decision']['client_band'], 'likely')
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
        # JHB->CPT at R20 000: inside what the company model was trained on.
        resp = self.api.post('/api/v1/quotes/', self.quote_payload(
            delivery_location='Cape Town', total_amount='20000', distance='1400'), format='json')
        qid = resp.json()['id']
        resp = self.api.patch(f'/api/v1/quotes/{qid}/', {'pricing_decision': self.decision(
            final_price=20000, floor=16900, likelihood_level='model', likelihood_at_final_pct=99,
            model_version='company:1:x')}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        d = QuotePricingDecision.objects.get(quote_id=qid)
        # The SERVER's model % at the final price, not the client's 99.
        self.assertEqual(d.likelihood_level, 'model')
        self.assertIsNotNone(d.likelihood_at_final_pct)
        self.assertNotEqual(d.likelihood_at_final_pct, 99)
        self.assertEqual(d.payload['client_pct'], 99)
        self.assertTrue(d.model_version.startswith('company:'))
        self.assertEqual(Quote.objects.get(id=qid).win_probability, Decimal(d.likelihood_at_final_pct))
        # A price far outside the model's range -> rules level, cleared to null.
        self.api.patch(f'/api/v1/quotes/{qid}/', {'total_amount': '90000', 'pricing_decision': self.decision(
            final_price=90000, likelihood_level='model', likelihood_at_final_pct=70)}, format='json')
        self.assertIsNone(Quote.objects.get(id=qid).win_probability)
        self.assertEqual(QuotePricingDecision.objects.get(quote_id=qid).likelihood_level, 'rules')


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
        # No real quotes on the lane: no market figure at all (the hard-coded
        # SA lane table is never shown as market data).
        r = self.api.get('/api/v1/quotes/benchmark/?origin=JHB&destination=DBN&vehicle_type=truck'
                         '&your_rate=15000').json()
        self.assertIsNone(r['market_avg_rate'])
        self.assertNotEqual(r.get('source'), 'estimate')
        self.assertNotIn('competitive', (r.get('recommendation') or '').lower())

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


class Round1FixTests(_Base):
    """Round 1 review fixes (accuracy, phone and UX audits)."""

    def _decision(self, **over):
        d = {'version': 'pa-1', 'picked_choice': 'balanced', 'final_price': 22700, 'floor': 14000,
             'market': {'tier': 'platform', 'n': 9}, 'likelihood_level': 'rules', 'band_at_final': 'likely',
             'floor_lines': [{'key': 'fuel', 'label': 'Fuel', 'amount': 6500, 'source': {'kind': 'official'}},
                             {'key': 'fixed_cost', 'label': 'Operating costs', 'amount': 7046,
                              'source_kind': 'estimate'},
                             {'key': 'bogus', 'label': 'x', 'amount': 1}]}
        d.update(over)
        return d

    def _quote(self, **over):
        p = {'customer': self.customer.id, 'pickup_location': 'Johannesburg', 'delivery_location': 'Durban',
             'cargo_description': 'Maize', 'weight': '28000', 'base_rate': '15000', 'total_amount': '22700',
             'valid_until': str(date.today() + timedelta(days=7))}
        p.update(over)
        return p

    # B1
    def test_decision_write_failure_fails_the_save(self):
        from unittest import mock
        from django.db import OperationalError
        boom = mock.patch('core.models.QuotePricingDecision.objects.update_or_create',
                          side_effect=OperationalError('database is locked'))
        with boom:
            resp = self.api.post('/api/v1/quotes/', self._quote(pricing_decision=self._decision()), format='json')
        self.assertEqual(resp.status_code, 503, resp.content)
        self.assertFalse(Quote.objects.filter(company=self.company).exists())
        ok = self.api.post('/api/v1/quotes/', self._quote(pricing_decision=self._decision()), format='json')
        qid = ok.json()['id']
        with boom:
            resp = self.api.patch(f'/api/v1/quotes/{qid}/', {'total_amount': '30000',
                                                              'pricing_decision': self._decision(final_price=30000)},
                                  format='json')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(Quote.objects.get(id=qid).total_amount, Decimal('22700'))
        self.assertEqual(QuotePricingDecision.objects.get(quote_id=qid).final_price, Decimal('22700'))

    def test_floor_lines_stored_and_returned(self):
        resp = self.api.post('/api/v1/quotes/', self._quote(pricing_decision=self._decision()), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        lines = resp.json()['pricing_decision']['floor_lines']
        self.assertEqual(lines, [{'key': 'fuel', 'label': 'Fuel', 'amount': 6500, 'source_kind': 'official'},
                                 {'key': 'fixed_cost', 'label': 'Operating costs', 'amount': 7046,
                                  'source_kind': 'estimate'}])
        detail = self.api.get(f"/api/v1/quotes/{resp.json()['id']}/").json()
        self.assertEqual(len(detail['pricing_decision']['floor_lines']), 2)

    # B3 / B4
    def test_empty_return_uses_unrounded_fuel_price_and_counts_nights(self):
        VehicleType.objects.create(company=self.company, name='Tautliner', capacity=Decimal('34'),
                                   max_distance=Decimal('2000'), base_rate=Decimal('20'),
                                   fuel_consumption_l_per_100km=Decimal('38'))
        # 568 km at the official R32,7989 (unrounded); 9 h each way -> one way
        # 0 nights, round trip 1. Empty burn = rated × 0.70 (QUOTE-RULES §4).
        r = self.analyze(include_return=True, fuel_price_used=30.0049, fuel_usage_litres=216.6,
                         fuel_cost=6499, duration_minutes=540)
        fuel = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'fuel')
        self.assertAlmostEqual(fuel['price_per_litre'], 32.7989)
        by = {ln['key']: ln for ln in r['costing']['lines']}
        from core.services.quote_costing import cents
        self.assertEqual(by['fuel_return']['amount'], cents(568 * 38 * 0.70 / 100 * 32.7989))
        self.assertEqual(by['driver_return']['nights'], 1)

    # Drafts and one definition of won
    def test_drafts_and_the_edited_quote_are_not_customer_evidence(self):
        sent = make_quote(self.company, self.customer, number='D-1', status='SENT', pickup_location='Johannesburg',
                          delivery_location='Durban', origin='JHB', destination='DBN')
        make_quote(self.company, self.customer, number='D-2', status='DRAFT', outcome='accepted',
                   pickup_location='Johannesburg', delivery_location='Durban', origin='JHB', destination='DBN')
        editing = make_quote(self.company, self.customer, number='D-3', status='SENT', origin='JHB',
                             destination='DBN', pickup_location='Johannesburg', delivery_location='Durban')
        r = self.analyze(customer_id=self.customer.id, quote_id=editing.id)
        ids = [q['id'] for q in r['customer']['recent_lane_quotes']]
        self.assertEqual(ids, [sent.id])
        self.assertEqual(r['customer']['acceptance']['decided'], 0)
        # Recorded won on a SENT quote counts as won (same as the market tier).
        make_quote(self.company, self.customer, number='D-4', status='SENT', outcome='accepted')
        r = self.analyze(customer_id=self.customer.id)
        self.assertEqual(r['customer']['acceptance']['won'], 1)

    # Copy
    def test_sa_number_style_and_true_safe_summary(self):
        r = self.analyze(origin='BFN', destination='PLK', fuel_cost=300, toll_cost=0, distance_km=10,
                         one_way_distance_km=10, route={})
        safe = r['choices'][0]
        self.assertEqual(safe['summary'], f"{safe['margin_pct']}% margin, priced from your cost floor.")
        text = ' '.join(r['reasoning'])
        self.assertRegex(text, 'R\u00a0\\d')
        self.assertNotRegex(text, r'R\d')
        fixed = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'fixed_cost')
        self.assertIn('R\u00a014,50/km', fixed['basis'])

    def test_empty_return_summaries_match_each_card(self):
        platform_market(self.company, self.customer, start=11000)
        r = self.analyze(include_return=True, your_price=26000)
        m = r['market']
        for c in r['choices']:
            # R5 (K-6): where the price sits, never a margin % that could disagree with the card.
            if c['summary'].endswith('priced from your cost floor.'):
                self.assertTrue(c['summary'].startswith(f"{c['margin_pct']}% margin"))
            elif c['price'] > m['p75']:
                self.assertEqual(c['summary'], 'Above the middle half of the market.')
        # Every choice is above p75 with the empty run home in the floor, so
        # all are "Less likely": none is labelled Recommended.
        self.assertFalse(any(c['recommended'] for c in r['choices']))
        self.assertIsNone(r['recommendation']['key'])

    # Payment risk, below floor, driver needs input
    def test_payment_risk_attention_and_no_likelihood_below_floor(self):
        from unittest import mock
        risk = {'band': 'HIGH', 'stats': {'invoice_count': 6, 'late_count': 4}}
        with mock.patch('core.services.customer_risk.compute_customer_risk', return_value=risk):
            r = self.analyze(customer_id=self.customer.id, your_price=3000)
        self.assertEqual(r['attention'][0]['code'], 'payment_risk')
        self.assertEqual(r['attention'][0]['level'], 'high')
        self.assertIn('deposit', r['attention'][0]['message'])
        self.assertEqual(r['recommendation']['key'], 'balanced')
        self.assertTrue(r['your_price']['below_floor'])
        self.assertIsNone(r['your_price']['likelihood'])

    def test_driver_line_needs_input_when_nights_and_no_rate(self):
        self.company.driver_allowance_per_night = None
        self.company.save()
        r = self.analyze(duration_minutes=1200)
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'driver_allowance')
        self.assertEqual(line['status'], 'needs_input')
        self.assertEqual(line['nights'], 2)
        r = self.analyze(duration_minutes=1200, driver_cost=900)
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'driver_allowance')
        self.assertEqual(line['status'], 'ok')

    # Operating costs
    def test_operating_cost_setting_wins_and_classes(self):
        self.company.operating_cost_per_km = Decimal('14.25')
        self.company.save()
        r = self.analyze()
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'fixed_cost')
        self.assertEqual(line['source']['kind'], 'user')
        self.assertEqual(line['amount'], round(568 * 14.25))
        self.assertNotIn('estimate_fixed_cost', {w['code'] for w in r['warnings']})
        from core.serializers import CompanySerializer
        ser = CompanySerializer(self.company, data={'operating_cost_per_km': '0.10'}, partial=True)
        self.assertFalse(ser.is_valid())
        self.assertIn('operating_cost_per_km', ser.errors)
        from core.services.pricing_analysis import vehicle_class
        self.assertEqual(vehicle_class(None, 'Superlink 34t'), 'superlink')
        self.assertEqual(vehicle_class(None, 'Reefer'), 'reefer')
        self.assertEqual(vehicle_class(None, '8 ton rigid'), 'rigid')
        self.assertEqual(vehicle_class(None, ''), 'tri_axle')

    def test_company_level_costs_count_in_actuals(self):
        from core.models import Driver, Load, Vehicle
        vt = VehicleType.objects.create(name='OpTruck', capacity=Decimal('34'), max_distance=Decimal('2000'),
                                        base_rate=Decimal('15'))
        vehicle = Vehicle.objects.create(company=self.company, vin='OPVIN1', plate='OP001GP', vehicle_type=vt,
                                         make='Merc', model='Actros', year=2020, type='Truck',
                                         capacity=Decimal('34'), fuel_type='Diesel', status='AVAILABLE')
        du = User.objects.create_user(username='op_driver', email='d@op.test', password='x')
        driver = Driver.objects.create(company=self.company, user=du, license_number='OP-1',
                                       license_expiry=date.today() + timedelta(days=365), license_state='GP',
                                       hire_date=date.today() - timedelta(days=365))
        for i in range(10):
            load = Load.objects.create(
                load_number=f'OPL-{i}', company=self.company, customer=self.customer, pickup_location='A',
                pickup_city='A', pickup_state='', pickup_zip='', pickup_date=timezone.now(),
                delivery_location='B', delivery_city='B', delivery_state='', delivery_zip='',
                delivery_date=timezone.now(), cargo_description='x', weight=1, rate=1, total_amount=1)
            Trip.objects.create(load=load, vehicle=vehicle, driver=driver, status='COMPLETED',
                                distance_km=Decimal('1000'), estimated_distance_km=Decimal('1000'),
                                estimated_duration_hours=Decimal('12'), origin='A', destination='B',
                                start_time=timezone.now())
        # Company-level (no trip): insurance R60 000 + salaries R50 000 excl. VAT, fuel excluded.
        Expense.objects.create(company=self.company, expense_number='OPX-1', category='INSURANCE', description='i',
                               amount=Decimal('60000'), expense_date=date.today(), status='APPROVED')
        Expense.objects.create(company=self.company, expense_number='OPX-2', category='DRIVER_COST', description='s',
                               amount=Decimal('50000'), expense_date=date.today(), status='APPROVED')
        Expense.objects.create(company=self.company, expense_number='OPX-3', category='FUEL', description='f',
                               amount=Decimal('90000'), expense_date=date.today(), status='APPROVED')
        cache.clear()
        r = self.analyze()
        fixed = r['cost_floor']['fixed_cost_per_km']
        self.assertEqual(fixed['source'], 'company_actuals')
        self.assertAlmostEqual(fixed['value'], 11.0)      # 110 000 / 10 000 km

    # Outcome endpoint keeps status in step
    def test_outcome_endpoint_moves_status(self):
        q = make_quote(self.company, self.customer, number='ST-1', status='DRAFT', created_by=self.user)
        resp = self.api.patch(f'/api/v1/quotes/{q.id}/outcome/', {'outcome': 'accepted'}, format='json')
        self.assertEqual(resp.json()['status'], 'ACCEPTED')
        q2 = make_quote(self.company, self.customer, number='ST-2', status='SENT', created_by=self.user)
        self.api.patch(f'/api/v1/quotes/{q2.id}/outcome/', {'outcome': 'rejected', 'loss_reason': 'price'},
                       format='json')
        q2.refresh_from_db()
        self.assertEqual((q2.status, q2.outcome), ('DECLINED', 'rejected'))

    def test_price_adjustment_and_list_margin(self):
        resp = self.api.post('/api/v1/quotes/', self._quote(pricing_decision=self._decision(price_adjustment=-250)),
                             format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['pricing_decision']['price_adjustment'], -250)
        listing = self.api.get('/api/v1/quotes/').json()
        row = (listing.get('results', listing))[0]
        self.assertEqual(row['pricing_margin_pct'], round((22700 - 14000) / 22700 * 100))
        self.assertNotIn('pricing_decision', row)

    def test_convert_to_load_accepts_dates(self):
        q = make_quote(self.company, self.customer, number='CD-1', created_by=self.user, status='ACCEPTED')
        bad = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/',
                            {'pickup_date': '2026-11-10', 'delivery_date': '2026-11-09'}, format='json')
        self.assertEqual(bad.status_code, 400)
        resp = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/',
                             {'pickup_date': '2026-11-10', 'delivery_date': '2026-11-12'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.json()['pickup_date'].startswith('2026-11-10'))
        self.assertTrue(resp.json()['delivery_date'].startswith('2026-11-12'))


class Round3Tests(_ModelMixin, _Base):
    """Round 3: exact model range, server band, scoring outside the write
    transaction, recommendation by expected profit, return-trip market,
    empty-return context, labels and short reasons."""

    def _platform(self, n_own=3, n_other=4, start=24000, **kw):
        return platform_market(self.company, self.customer, n_own, n_other, start, **kw)[0]

    def test_round_trip_market_is_one_way_times_two(self):
        self._platform()
        one = self.analyze()
        rt = self.analyze(legs=2, trip_type='ROUND_TRIP', distance_km=1136)
        self.assertTrue(rt['market']['legs_scaled'])
        self.assertIn('×2 for a return trip', rt['market']['tier_label'])
        self.assertIn('one-way', rt['market']['tier_label'])
        for k in ('p25', 'median', 'p75'):
            self.assertLessEqual(abs(rt['market'][k] - one['market'][k] * 2), 500)   # each rounded to R500
            self.assertEqual(rt['market'][k] % 500, 0)
        self.assertEqual(rt['market']['basis'], 'one_way_x2')
        self.assertEqual(rt['market']['basis_label'], 'one-way quotes ×2')
        # Choices and bands come from the scaled range.
        self.assertGreaterEqual(rt['choices'][1]['price'], rt['market']['median'])
        self.assertEqual(rt['likelihood']['rules']['thresholds']['likely_max'], pa.round_price(rt['market']['median']))

    def test_round_trip_quotes_are_not_in_the_one_way_sample(self):
        self._platform()
        before = self.analyze()['market']['n']
        for i in range(3):
            make_quote(self.company, self.customer, number=f'RT-{i}', total=60000, status='ACCEPTED',
                       outcome='accepted', trip_type='ROUND_TRIP', pickup_location='Johannesburg',
                       delivery_location='Durban')
        pa._MARKET_MEMO.clear()
        self.assertEqual(self.analyze()['market']['n'], before)

    def test_vehicle_filter_is_named_in_the_label(self):
        VehicleType.objects.create(company=self.company, name='Superlink', capacity=34, max_distance=3000,
                                   base_rate=20, fuel_consumption_l_per_100km=42)
        self._platform(n_own=3, n_other=4, vehicle_type='Superlink')
        r = self.analyze(vehicle_type='Superlink')
        self.assertTrue(r['market']['vehicle_specific'])
        self.assertIn('Superlink only', r['market']['tier_label'])

    def test_empty_return_context_always_present(self):
        r = self.analyze(include_return=False)
        f = r['cost_floor']
        self.assertFalse(f['include_return'])
        self.assertAlmostEqual(f['floor_with_return'], f['total'] + f['return_leg_amount'], places=2)
        for c in r['choices']:
            self.assertEqual(c['margin_pct_if_empty_return'],
                             round((c['price'] - f['floor_with_return']) / c['price'] * 100))
        rt = self.analyze(legs=2, trip_type='ROUND_TRIP')
        self.assertIsNone(rt['cost_floor']['floor_with_return'])
        self.assertIsNone(rt['choices'][0]['margin_pct_if_empty_return'])

    def test_reasoning_whole_rand_and_nbsp(self):
        r = self.analyze()
        first = r['reasoning'][0]
        self.assertIn(pa._fmt(r['cost_floor']['total']), first)       # whole rand, same as the cost card
        self.assertNotRegex(first, r'/km\D*,\d\d/km')
        self.assertNotIn('R ', ' '.join(r['reasoning']))   # no plain space after R

    def test_short_reason_without_model(self):
        r = self.analyze()
        self.assertEqual(r['likelihood']['short'], 'Bands · 0 of 400 closed quotes')
        self.assertLessEqual(len(r['likelihood']['short']), 40)


class Round3ModelTests(_ModelMixin, _Base):
    def setUp(self):
        super().setUp()
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn not installed')
        self.train_company_model(self.company, self.user, self.customer)

    def test_model_range_is_exactly_the_scoring_domain(self):
        r = self.analyze(**self.model_payload(your_price=21000))
        m = r['likelihood']['model']
        self.assertIsNotNone(m, r['likelihood'].get('reason'))
        lo, hi = m['range']
        self.assertEqual(m['curve'][0]['price'], lo)
        self.assertEqual(m['curve'][-1]['price'], hi)
        inside = self.analyze(**self.model_payload(your_price=lo))
        self.assertEqual(inside['your_price']['likelihood']['level'], 'model')
        self.assertEqual(inside['likelihood']['model']['range'][0], lo)
        # in_range() is True exactly on [lo, hi] — the published range.
        from core.services.win_prediction import resolve_prediction_context
        ctx = resolve_prediction_context(self.user, self.company)
        p = base_payload(**self.model_payload(your_price=21000))
        o, d = pa._resolve_lane(p)
        floor = r['cost_floor']['total']
        block, _r, (predict, in_range) = pa.model_likelihood(
            ctx=ctx, company=self.company, user=self.user, payload=p, origin=o, destination=d, vt_name=None,
            floor_total=floor, probe_prices=[c['price'] for c in r['choices']] + [21000], customer_id=None)
        self.assertEqual(block['range'], [lo, hi])
        self.assertTrue(in_range(lo) and in_range(hi))
        self.assertFalse(in_range(lo - 1) or in_range(hi + 1))
        self.assertLessEqual(len(r['likelihood']['short']), 40)
        self.assertEqual(r['likelihood']['short'], 'From 400 closed quotes')

    def test_recommendation_is_best_expected_profit_or_balanced_within_3pct(self):
        r = self.analyze(**self.model_payload())
        scored = {c['key']: c['likelihood']['pct'] / 100 * c['margin'] for c in r['choices']
                  if c['likelihood']['level'] == 'model'}
        best = max(scored, key=scored.get)
        rec = r['recommendation']['key']
        if rec == 'balanced' and best != 'balanced':
            self.assertGreaterEqual(scored['balanced'], scored[best] * 0.97)
        else:
            self.assertEqual(rec, best)
        # Round 4: the curve's best is named only when it beats the pick after
        # rounding to R100 (else the best is stated once, as the pick's).
        self.assertIn(r['recommendation']['reason'], r['reasoning'])
        peak = r['likelihood']['model']['best']
        self.assertEqual(peak['expected_profit'], max(p['expected_profit'] for p in r['likelihood']['model']['curve']))

    def test_model_without_market_says_so(self):
        from unittest import mock
        with mock.patch.object(pa, 'market_rate', return_value=(None, 'none')):
            r = self.analyze(**self.model_payload())
        self.assertEqual(r['likelihood']['level'], 'rules')
        self.assertIn('no market figures for this lane', r['likelihood']['reason'])
        self.assertEqual(r['likelihood']['short'], 'No market figures for this lane')

    def test_save_scores_outside_the_transaction_and_db_errors_propagate(self):
        from unittest import mock
        from django.db import OperationalError, connection
        from core.services import pricing_decisions as pdm
        seen = {}
        real = pdm.score_final_price

        def spy(*a, **kw):
            seen['in_atomic'] = connection.in_atomic_block and not getattr(self, '_outer_atomic_only', False)
            seen['depth'] = len(connection.savepoint_ids)
            return real(*a, **kw)
        payload = {'customer': self.customer.id, 'pickup_location': 'Johannesburg', 'delivery_location': 'Cape Town',
                   'cargo_description': 'x', 'weight': '28000', 'base_rate': '15000', 'total_amount': '20000',
                   'distance': '1000', 'valid_until': str(date.today() + timedelta(days=7)),
                   'pricing_decision': {'final_price': 20000, 'floor': 17200, 'likelihood_level': 'rules',
                                        'band_at_final': 'less_likely'}}
        baseline = len(connection.savepoint_ids)
        with mock.patch.object(pdm, 'score_final_price', side_effect=spy):
            resp = self.api.post('/api/v1/quotes/', payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        # TestCase wraps each test in one atomic block; scoring must not add a savepoint.
        self.assertEqual(seen['depth'], baseline)
        d = QuotePricingDecision.objects.get(quote_id=resp.json()['id'])
        self.assertEqual(d.likelihood_level, 'model')        # server, not the client's 'rules'
        self.assertEqual(d.payload['client_band'], 'less_likely')
        # A DatabaseError while scoring -> 503, nothing saved.
        n = Quote.objects.count()
        with mock.patch.object(pa, 'customer_evidence', side_effect=OperationalError('database is locked')):
            resp = self.api.post('/api/v1/quotes/', payload, format='json')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(Quote.objects.count(), n)

    def test_rules_band_at_save_comes_from_server_thresholds(self):
        other = Company.objects.create(company_name='Ridgeback Freight')
        won_quotes(other, make_customer(other, 'Saltpan Traders'), 4, start=25500, prefix='B')
        won_quotes(self.company, self.customer, 3, start=24000, prefix='A')
        from core.services.pricing_decisions import score_final_price
        fields = {'customer': self.customer, 'pickup_location': 'Johannesburg', 'delivery_location': 'Durban',
                  'vehicle_type': '', 'weight': 28000, 'distance': 568, 'trip_type': 'ONE_WAY'}
        out = score_final_price(fields, {'final_price': 99000, 'band_at_final': 'likely'}, company=self.company,
                                user=self.user)
        self.assertEqual(out['level'], 'rules')
        self.assertEqual(out['band'], 'less_likely')


def _choice(key, price, margin, pct):
    return {'key': key, 'label': pa.CHOICE_LABELS[key], 'price': price, 'margin': margin,
            'likelihood': {'level': 'model', 'pct': pct}}


class Round4Tests(_Base):
    """Round 4: exact your_price echo, expected profit from the unrounded
    probability, the 25% rule, empty-return hold, per km driven, return-trip
    market basis, display rounding, never-sent quotes, driver allowance
    setting, company profile fields, agreed price, benchmark back-compat."""

    def _platform(self, n_own=3, n_other=4, start=24000, **kw):
        return platform_market(self.company, self.customer, n_own, n_other, start, **kw)[0]
        return other

    # B1
    def test_your_price_is_echoed_exactly(self):
        r = self.analyze(your_price=23322.5)
        self.assertEqual(r['your_price']['price'], 23322.5)
        r = self.analyze(your_price=26401.37)
        self.assertEqual(r['your_price']['price'], 26401.37)

    # B2 + 25% rule
    def test_recommendation_uses_unrounded_probability(self):
        # S4a: Safe 0.6346 × 7 063 = 4 482 vs Balanced 0.5355 × 8 113 = 4 345
        # (3.06% below). Rounded % (63 / 54) would keep Balanced; raw picks Safe.
        choices = [_choice('safe', 30000, 7063, 63), _choice('balanced', 31100, 8113, 54),
                   _choice('stretch', 33000, 10013, 30)]
        raw = {'safe': 0.6346, 'balanced': 0.5355, 'stretch': 0.30}
        rec = pa._recommend(choices, None, None, raw_p=raw)
        self.assertEqual(rec['key'], 'safe')
        self.assertIn('about R 4 500 expected profit per quote', rec['reason'])   # R100 rounding
        self.assertEqual(rec['short'], 'the highest expected profit of the three, about R 4 500 per quote.')
        # Within 3% on the raw figures -> Balanced kept.
        raw['balanced'] = 0.5400
        self.assertEqual(pa._recommend(choices, None, None, raw_p=raw)['key'], 'balanced')

    def test_balanced_within_3pct_is_one_plain_sentence(self):
        choices = [_choice('safe', 22000, 2000, 70), _choice('balanced', 24000, 4000, 62),
                   _choice('stretch', 26000, 6000, 42)]
        raw = {'safe': 0.70, 'balanced': 0.62, 'stretch': 0.42}      # EP 2 480 vs Stretch 2 520
        block = {'best': {'price': 24870, 'pct': 52, 'expected_profit': 2600, 'choice': None}}
        rec = pa._recommend(choices, None, block, raw_p=raw)
        self.assertEqual(rec['key'], 'balanced')
        self.assertEqual(rec['reason'], 'Balanced is recommended: about R 2 500 expected profit per quote '
                                        '(62% chance × R 4 000 margin), level with Stretch (about R 2 500), '
                                        'with a better chance to win.')
        self.assertEqual(rec['short'], 'level with Stretch on expected profit, with a better chance to win.')
        self.assertEqual(rec['code'], 'level_with')

    def test_no_market_reason_does_not_claim_a_market(self):
        r = self.analyze(origin='BFN', destination='PLK')
        self.assertFalse(r['market']['available'])
        reason = r['recommendation']['reason']
        self.assertNotIn('what this lane pays', reason)
        self.assertIn('while this lane has no market data', reason)
        self.assertEqual(r['recommendation']['code'], 'no_market')
        self.assertFalse(r['recommendation']['short'].startswith('Balanced'))

    def test_never_recommend_under_25_pct_unless_all_are(self):
        choices = [_choice('safe', 30000, 2000, 90), _choice('balanced', 31000, 3000, 40),
                   _choice('stretch', 40000, 12000, 20)]
        raw = {'safe': 0.9, 'balanced': 0.4, 'stretch': 0.2}     # Stretch EP 2 400 is the highest
        rec = pa._recommend(choices, None, None, raw_p=raw)
        self.assertEqual(rec['key'], 'safe')
        self.assertIn('Stretch is not recommended: under a 25% chance', rec['reason'])
        low = {'safe': 0.2, 'balanced': 0.15, 'stretch': 0.1}
        self.assertEqual(pa._recommend(choices, None, None, raw_p=low)['key'], 'stretch')

    def test_best_expected_profit_is_never_below_a_stated_choice(self):
        choices = [_choice('safe', 30000, 7063, 63), _choice('balanced', 31100, 8113, 54),
                   _choice('stretch', 33000, 10013, 30)]
        raw = {'safe': 0.6346, 'balanced': 0.5355, 'stretch': 0.30}
        block = {'best': {'price': 30000, 'pct': 63, 'expected_profit': 4482, 'choice': 'safe'}}
        rec = pa._recommend(choices, None, block, raw_p=raw)
        self.assertNotIn('model\'s best', rec['reason'])     # the best is a stated choice: said once
        block = {'best': {'price': 29500, 'pct': 66, 'expected_profit': 4600, 'choice': None}}
        rec = pa._recommend(choices, None, block, raw_p=raw)
        # R5 (D3): the curve's peak is never named; only the three prices are discussed.
        self.assertNotIn('29\u00a0500', rec['reason'])
        self.assertNotIn('best expected profit', rec['reason'])

    def test_payment_risk_override_first_then_both_profits(self):
        cust = {'payment_risk': {'band': 'high'}}
        choices = [_choice('safe', 30000, 7063, 63), _choice('balanced', 31100, 8113, 54),
                   _choice('stretch', 33000, 10013, 30)]
        rec = pa._recommend(choices, cust, None, raw_p={'safe': 0.6346, 'balanced': 0.5355, 'stretch': 0.3})
        self.assertEqual(rec['key'], 'balanced')
        self.assertTrue(rec['reason'].startswith('Safe is not recommended for this customer'))
        self.assertIn('R 4 500', rec['reason'])
        self.assertIn('R 4 300', rec['reason'])

    # 3: empty return can't be paid by this lane
    def test_empty_return_unpaid_keeps_balanced_with_attention(self):
        self._platform(start=12000)
        r = self.analyze(include_return=True)
        # All three are less likely: no price is recommended, the gap is said plainly.
        self.assertIsNone(r['recommendation']['key'])
        self.assertEqual(r['recommendation']['code'], 'empty_return_gap')
        codes = [a['code'] for a in r['attention']]
        self.assertIn('empty_return_unpaid', codes)
        msg = next(a for a in r['attention'] if a['code'] == 'empty_return_unpaid')['message']
        self.assertEqual(msg, 'This lane pays less than your full cost when the truck returns empty. '
                              'Price for a backload or charge for the empty return.')
        # Not raised for a one-way price without the return in the floor.
        self.assertNotIn('empty_return_unpaid',
                         [a['code'] for a in self.analyze(include_return=False)['attention']])

    def test_empty_return_unpaid_says_target_margin_when_p75_covers_the_cost(self):
        # r5 L1: p75 above the full cost (with the return) but within the
        # target margin -> never "pays less than your full cost".
        from unittest import mock
        self._platform(start=12000)
        floor = self.analyze(include_return=True)['cost_floor']['total']
        real = pa.trip_market

        def shifted(*a, **kw):
            m = real(*a, **kw)
            m.update(p25=floor * 0.96, median=floor * 1.0, p75=floor * 1.04)
            return m

        with mock.patch.object(pa, 'trip_market', side_effect=shifted):
            r = self.analyze(include_return=True)
        msg = next(a for a in r['attention'] if a['code'] == 'empty_return_unpaid')['message']
        self.assertEqual(msg, 'This lane leaves less than your 10% target margin once the empty run home is '
                              'included. Price for a backload or charge for the empty return.')
        self.assertNotIn('full cost', msg)
        self.assertEqual(r['recommendation']['code'], 'empty_return_gap')
        # A different target is named.
        self.company.margin_target_pct = 15
        self.company.save()
        with mock.patch.object(pa, 'trip_market', side_effect=shifted):
            r = self.analyze(include_return=True)
        msg = next(a for a in r['attention'] if a['code'] == 'empty_return_unpaid')['message']
        self.assertTrue(msg.startswith('This lane leaves less than your 15% target margin'), msg)

    # 4
    def test_per_km_is_per_km_driven(self):
        one = self.analyze(include_return=False)['cost_floor']
        self.assertEqual(one['per_km'], round(one['total'] / 568, 2))
        self.assertEqual(one['per_km_label'], 'per km driven')
        self.assertEqual(one['floor_with_return_per_km'], round(one['floor_with_return'] / 1136, 2))
        ret = self.analyze(include_return=True)
        f = ret['cost_floor']
        self.assertEqual(f['km_driven'], 1136)
        self.assertEqual(f['per_km'], round(f['total'] / 1136, 2))
        self.assertIn(' per km driven, both legs', ret['reasoning'][0])
        self.assertIn(f"R\u00a0{f['per_km_rand']} per km driven", ret['reasoning'][0])

    # 5a
    def test_round_trip_prefers_real_return_trip_quotes(self):
        self._platform()
        rt = self.analyze(legs=2, trip_type='ROUND_TRIP', distance_km=1136)
        self.assertEqual(rt['market']['basis'], 'one_way_x2')
        for i, name in enumerate(OTHER_OPERATORS):
            other = Company.objects.get(company_name=name)
            won_quotes(other, make_customer(other, f'Kraal Foods {i}'), 4, start=48000, prefix=f'RB{i}',
                       trip_type='ROUND_TRIP')
        pa._MARKET_MEMO.clear()
        rt = self.analyze(legs=2, trip_type='ROUND_TRIP', distance_km=1136)
        m = rt['market']
        self.assertEqual((m['basis'], m['basis_label'], m['n'], m['legs_scaled']),
                         ('round_trip', 'return-trip quotes', 12, False))
        self.assertIn('return-trip quotes', m['tier_label'])
        self.assertTrue(47000 <= m['median'] <= 50000)
        # One-way sample unchanged by the return-trip quotes.
        self.assertEqual(self.analyze()['market']['n'], 12)

    # 5b + 8
    def test_round_trip_nights_and_company_allowance_setting(self):
        self.company.driver_allowance_per_night = None
        self.company.save()
        r = self.analyze(legs=2, trip_type='ROUND_TRIP', distance_km=1136, duration_minutes=420)
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'driver_allowance')
        self.assertEqual(line['nights'], 1)                 # 14 h driving over both legs
        self.assertEqual(line['status'], 'needs_input')
        self.company.driver_allowance_per_night = Decimal('650')
        self.company.save()
        r = self.analyze(legs=2, trip_type='ROUND_TRIP', distance_km=1136, duration_minutes=420)
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'driver_allowance')
        self.assertEqual((line['amount'], line['suggested'], line['status']), (650, 650, 'ok'))
        self.assertEqual(line['source'], {'kind': 'user', 'label': 'Your setting', 'url': None, 'as_of': None})
        self.assertNotIn('no_driver_allowance', {w['code'] for w in r['warnings']})
        # QUOTE-RULES §6: nights × the COMPANY allowance; an approved
        # allowance on record is used only when the company has none.
        from unittest import mock
        with mock.patch('core.services.quote_ai_pricing.stored_allowance',
                        return_value={'rate_per_night': 500, 'label': 'NBCRFLI driver allowance'}):
            r = self.analyze(legs=2, trip_type='ROUND_TRIP', distance_km=1136, duration_minutes=420)
        line = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'driver_allowance')
        self.assertEqual((line['amount'], line['source']['kind']), (650, 'user'))

    # 6
    def test_market_display_rounding_keeps_raw_values(self):
        self._platform(start=24030)
        m = self.analyze(your_price=25000)['market']
        for k in ('p25', 'median', 'p75'):
            self.assertEqual(m[k] % 500, 0)            # platform: R500, no raw figures (privacy)
            self.assertNotIn('raw_' + k, m)
        self.assertEqual(m['rounded_to'], 500)
        none = self.analyze(origin='JHB', destination='CPT')['market']
        self.assertFalse(none['available'])            # no hard-coded estimate is shown
        self.assertIsNone(none['median'])

    # Never-sent quotes are not evidence
    def test_never_sent_won_quotes_are_not_market_or_customer_evidence(self):
        others = platform_market(self.company, self.customer)   # 12 one-way wins from 3 others, created as won
        base_n = self.analyze()['market']['n']
        # Another operator's quote decided without ever being sent: not market evidence.
        o = others[0]
        o_cust = make_customer(o, 'Other Lane Customer')
        ns = make_quote(o, o_cust, number='ONS-1', total=25000, destination='DBN', status='DRAFT',
                        pickup_location='Johannesburg', delivery_location='Durban')
        ns.status, ns.outcome = 'ACCEPTED', 'accepted'
        ns.save()
        os_ = make_quote(o, o_cust, number='OS-1', total=25500, destination='DBN', status='DRAFT',
                         pickup_location='Johannesburg', delivery_location='Durban')
        os_.status, os_._skip_send_guard = 'SENT', True
        os_.save()
        os_.status = 'ACCEPTED'
        os_.save()
        q = make_quote(self.company, self.customer, number='NS-1', total=25000, destination='DBN',
                       status='DRAFT', pickup_location='Johannesburg', delivery_location='Durban')
        q.status = 'ACCEPTED'
        q.outcome = 'accepted'
        q.save()
        q.refresh_from_db()
        self.assertIs(q.was_sent, False)
        sent = make_quote(self.company, self.customer, number='S-1', total=25500, destination='DBN',
                          status='DRAFT', pickup_location='Johannesburg', delivery_location='Durban')
        sent.status = 'SENT'
        sent._skip_send_guard = True     # the subject here is was_sent, not the send guard
        sent.save()
        sent.status = 'ACCEPTED'
        sent.save()
        sent.refresh_from_db()
        self.assertIs(sent.was_sent, True)
        pa._MARKET_MEMO.clear()
        cache.clear()
        r = self.analyze(customer_id=self.customer.id)
        self.assertEqual(r['market']['n'], base_n + 1)         # only the other operator's sent one joins
        self.assertNotIn(q.id, [x['id'] for x in r['customer']['recent_lane_quotes']])
        self.assertIn(sent.id, [x['id'] for x in r['customer']['recent_lane_quotes']])
        # Existing callers of compute_lane_benchmark are unchanged (default).
        from core.services.lane_benchmark import compute_lane_benchmark
        own = self.company.id
        self.assertEqual(compute_lane_benchmark('JHB', 'DBN', exclude_company_id=own)['sample_size'], base_n + 2)
        self.assertEqual(compute_lane_benchmark('JHB', 'DBN', sent_only=True, exclude_company_id=own)['sample_size'],
                         base_n + 1)

    # 7 (M2)
    def test_market_rate_one_way_only_is_opt_in(self):
        from core.services.lane_benchmark import resolve_market_rate
        won_quotes(self.company, self.customer, 3, start=20000, prefix='OW')
        won_quotes(self.company, self.customer, 3, start=60000, prefix='RT', trip_type='ROUND_TRIP')
        both, _ = resolve_market_rate('JHB', 'DBN', company=self.company)
        one, src = resolve_market_rate('JHB', 'DBN', company=self.company, one_way_only=True)
        self.assertEqual(src, 'company')
        self.assertAlmostEqual(one, 20500)
        self.assertGreater(both, one)

    # 8: company profile
    def test_company_profile_pricing_fields(self):
        from core.serializers import CompanySerializer
        self.company.driver_allowance_per_night = None
        self.company.save()
        data = CompanySerializer(self.company).data
        self.assertIsNone(data['driver_allowance_per_night'])
        use = data['operating_cost_in_use']
        self.assertEqual((use['source'], use['value'], use['window']), ('vehicle_default', 14.5, 'last 12 months'))
        self.assertTrue(use['label'].startswith('Now using the typical SA estimate'))
        self.assertEqual(use['estimates']['superlink'], 16.0)
        ser = CompanySerializer(self.company, data={'margin_target_pct': '15', 'driver_allowance_per_night': '650'},
                                partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save()
        self.company.refresh_from_db()
        self.assertEqual((self.company.margin_target_pct, self.company.driver_allowance_per_night),
                         (Decimal('15'), Decimal('650')))
        self.assertEqual(self.analyze()['target_margin_pct'], 15)
        for bad in ({'margin_target_pct': '0'}, {'margin_target_pct': '100'}, {'driver_allowance_per_night': '0.5'}):
            self.assertFalse(CompanySerializer(self.company, data=bad, partial=True).is_valid(), bad)
        self.company.operating_cost_per_km = Decimal('13.99')
        self.company.save()
        use = CompanySerializer(self.company).data['operating_cost_in_use']
        self.assertEqual((use['source'], use['label']), ('setting', 'Now using your setting of R 13,99/km'))
        self.assertTrue(CompanySerializer(self.company).fields['operating_cost_in_use'].read_only)

    def test_operating_cost_classes_recalibrated(self):
        self.assertEqual({k: pa._class_default(k)[0] for k in pa.OPERATING_COST_CLASSES},
                         {'light': 8.0, 'rigid': 11.0, 'tri_axle': 14.5, 'reefer': 17.0, 'superlink': 16.0})

    # 9
    def test_agreed_price_on_quote_detail(self):
        q = make_quote(self.company, self.customer, number='AG-1', total=25100, status='ACCEPTED',
                       outcome='accepted')
        url = f'/api/v1/quotes/{q.id}/'
        self.assertIsNone(self.api.get(url).json()['agreed_price'])
        QuoteOutcome.objects.create(quote=q, company=self.company, outcome='accepted', final_price=Decimal('24500'))
        self.assertEqual(self.api.get(url).json()['agreed_price'], 24500.0)
        QuoteOutcome.objects.filter(quote=q).update(final_price=Decimal('25100'))
        self.assertIsNone(self.api.get(url).json()['agreed_price'])

    # 11: benchmark backward compatibility
    def test_benchmark_new_lane_code_without_data_answers_as_before(self):
        resp = self.api.get('/api/v1/quotes/benchmark/?origin=Johannesburg&destination=Gaborone&vehicle_type=superlink')
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('data_points', resp.json())
        self.assertEqual(resp.json(), {'success': False, 'error': 'origin, destination, and vehicle_type are required'})
        # A known lane without data still answers 200 with data_points 0.
        resp = self.api.get('/api/v1/quotes/benchmark/?origin=BFN&destination=PE&vehicle_type=superlink')
        if resp.json().get('source') != 'estimate':
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()['data_points'], 0)


class Round4ModelTests(_ModelMixin, _Base):
    def setUp(self):
        super().setUp()
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn not installed')
        self.train_company_model(self.company, self.user, self.customer)

    def test_model_level_expected_profit_is_raw_and_best_covers_choices(self):
        r = self.analyze(**self.model_payload())
        self.assertEqual(r['likelihood']['level'], 'model', r['likelihood'].get('reason'))
        from core.services.win_prediction import resolve_prediction_context
        ctx = resolve_prediction_context(self.user, self.company)
        p = base_payload(**self.model_payload())
        o, d = pa._resolve_lane(p)
        floor = r['cost_floor']['total']
        _b, _r, (predict, in_range) = pa.model_likelihood(
            ctx=ctx, company=self.company, user=self.user, payload=p, origin=o, destination=d, vt_name=None,
            floor_total=floor, probe_prices=[c['price'] for c in r['choices']] + [0], customer_id=None)
        raw = {c['key']: predict(c['price']) * c['margin'] for c in r['choices']
               if c['likelihood']['level'] == 'model'}
        best = r['likelihood']['model']['best']
        for v in raw.values():
            self.assertGreaterEqual(best['expected_profit'], v - 0.5)
        rec = r['recommendation']['key']
        top = max(raw, key=raw.get)
        if rec != top:
            self.assertEqual(rec, 'balanced')
            self.assertGreaterEqual(raw['balanced'], raw[top] * 0.97)
        # Expected-profit figures in the text are whole hundreds.
        import re
        for amount in re.findall(r'about R ([\d ]+)', r['recommendation']['reason']):
            self.assertEqual(int(amount.replace(' ', '')) % 100, 0)


BANNED_WORDS = __import__('re').compile(r'\bmiddle\b|likelihood|\bbands?\b', __import__('re').IGNORECASE)
ITEM_CODES = {'cost', 'market', 'margin', 'recommendation', 'customer', 'last_quote', 'model_basis', 'risk'}


class Round5Tests(_Base):
    """Round 5 (desk gate): display-ready headline / short recommendation /
    reasoning items, price-sensitive notice, R500 band edges with a minimum
    width, position-only summaries, half-up rounding, estimate below floor,
    fuel zone label, agreed margin, target range, PDF cargo, never-sent
    quotes out of the win model's evidence."""

    def _platform(self, n_own=3, n_other=4, start=24000, **kw):
        return platform_market(self.company, self.customer, n_own, n_other, start, **kw)[0]
        return other

    def _items_ok(self, r):
        self.assertEqual(r['reasoning'], [it['text'] for it in r['reasoning_items']])
        for it in r['reasoning_items']:
            self.assertIn(it['code'], ITEM_CODES)
            self.assertIsNone(BANNED_WORDS.search(it['text']), it['text'])

    # K-1
    def test_headline_per_case(self):
        none = self.analyze(origin='BFN', destination='PLK')
        self.assertEqual(none['likelihood']['headline'], 'No chance to win yet: no real quotes on this lane.')
        self.assertEqual(none['likelihood']['reason_code'], 'no_basis')
        self._platform()
        rules = self.analyze(include_return=False)
        self.assertEqual(rules['likelihood']['headline'],
                         'Chance to win as Likely, Even chance or Less likely. A % needs 200 won + 200 lost '
                         '(you have 0, 0).')
        self.assertEqual(rules['likelihood']['reason_code'], 'few_closed')
        self.assertIsNotNone(rules['likelihood']['short'])           # old field kept
        self.assertIsNone(self.analyze(distance_km=0)['likelihood']['headline'])
        th = {'likely_max': 1, 'even_max': 2}
        for code in ('few_closed', 'needs_both', 'trains_tonight', 'outside_range', 'no_market_for_model', None):
            self.assertLessEqual(len(pa.likelihood_headline(code, thresholds=th, won=17, lost=17)), 111)
        self.assertEqual(pa.likelihood_headline('outside_range', thresholds=th),
                         'Chance to win as Likely, Even chance or Less likely: these prices are outside what your '
                         'model has learned from.')
        self.assertEqual(pa.BAND_LABELS['even'], 'Even chance')

    # K-2 / K-14
    def test_recommendation_short_cases(self):
        c3 = [_choice('safe', 30000, 7063, 63), _choice('balanced', 31100, 8113, 54),
              _choice('stretch', 33000, 10013, 30)]
        cases = {
            'highest_ep': pa._recommend(c3, None, raw_p={'safe': 0.6346, 'balanced': 0.5355, 'stretch': 0.3}),
            'within_ep': pa._recommend(c3, None, raw_p={'safe': 0.6346, 'balanced': 0.5450, 'stretch': 0.3}),
            'payment_risk': pa._recommend(c3, {'payment_risk': {'band': 'high'}},
                                          raw_p={'safe': 0.6346, 'balanced': 0.5355, 'stretch': 0.3}),
            'excluded_low_chance': pa._recommend(
                [_choice('safe', 30000, 2000, 90), _choice('balanced', 31000, 3000, 40),
                 _choice('stretch', 40000, 12000, 20)], None, raw_p={'safe': 0.9, 'balanced': 0.4, 'stretch': 0.2}),
        }
        for code, rec in cases.items():
            self.assertEqual(rec['code'], code, rec)
            self.assertLessEqual(len(rec['short']), 90, rec['short'])
            if code not in ('payment_risk', 'excluded_low_chance'):    # those name the OTHER choice by design
                self.assertFalse(rec['short'].startswith(('Safe ', 'Balanced ', 'Stretch ')), rec['short'])
            self.assertNotIn('best expected profit', rec['reason'])
        self.assertEqual(cases['within_ep']['short'],
                         'within R 100 of Safe on expected profit, with a higher margin.')
        self.assertEqual(cases['payment_risk']['short'],
                         'Safe isn\'t recommended for a late payer; ask for a deposit instead.')
        self.assertEqual(cases['excluded_low_chance']['short'],
                         'Stretch has under a 25% chance to win; this is the best of the rest.')
        rules = [dict(c, likelihood={'level': 'rules', 'band': 'likely'}, margin_pct=20) for c in c3]
        market = {'p25': 30000, 'median': 31000, 'p75': 33000}
        rec = pa._recommend(rules, None, market=market, target=10)
        self.assertEqual((rec['code'], rec['short']),
                         ('rules_median', 'at the lane median, with a 20% margin after all costs.'))
        rec = pa._recommend(rules, None, market=dict(market, median=29000), target=10)
        self.assertEqual(rec['code'], 'rules_middle_half')
        rec = pa._recommend(rules, None, market=None, target=10)
        self.assertEqual(rec['short'], 'a 20% margin, a buffer above your 10% target while this lane has no market data.')
        rec = pa._recommend(rules, None, hold={'p75': 18950}, market=market, target=10)
        self.assertEqual(rec['code'], 'empty_return_gap')
        self.assertEqual(rec['short'], 'with the empty run home included, even this price is R 12 200 '
                                       'above the top of the market; price one-way if a load back is likely.')
        # r5 L1: the old `empty_return_unpaid` recommendation (gap <= 0) is gone;
        # a hold without a positive gap falls through to the normal reason.
        rec = pa._recommend(rules, None, hold={'p75': 31100}, market=market, target=10)
        self.assertEqual(rec, pa._recommend(rules, None, market=market, target=10))
        self.assertNotEqual(rec['code'], 'empty_return_unpaid')

    def test_empty_return_hold_states_what_was_tested(self):
        # r4 B1: never "pays less than your target at any of the three prices" next to 10/13/16% cards.
        self._platform(start=12000)
        r = self.analyze(include_return=True)
        rec = r['recommendation']
        self.assertEqual(rec['code'], 'empty_return_gap')
        self.assertNotIn('at any of the three prices', rec['reason'])
        bal = next(c for c in r['choices'] if c['key'] == 'balanced')
        if rec['code'] == 'empty_return_gap':
            gap = pa._round_to(bal['price'] - r['market']['p75'], 100)
            self.assertIn(pa._fmt(gap), rec['short'])
            self.assertGreater(bal['price'], r['market']['p75'])

    def test_cold_estimate_never_claims_the_lane_pays(self):
        r = self.analyze(origin='JHB', destination='CPT', distance_km=1400, one_way_distance_km=1400)
        self.assertFalse(r['market']['available'])
        self.assertEqual(r['recommendation']['code'], 'no_market')
        self.assertNotIn('what this lane pays', r['recommendation']['reason'])
        self.assertEqual(r['likelihood']['headline'], 'No chance to win yet: no real quotes on this lane.')

    # K-3 / K-7
    def test_reasoning_items_and_vocabulary(self):
        self._platform()
        for r in (self.analyze(customer_id=self.customer.id, your_price=3000), self.analyze(include_return=True),
                  self.analyze(origin='BFN', destination='PLK'), self.analyze(legs=2, distance_km=1136)):
            self._items_ok(r)
            f = r['cost_floor']
            self.assertEqual(f['per_km_rand'], pa._half_up(f['total'] / f['km_driven']))
            cost = next(it['text'] for it in r['reasoning_items'] if it['code'] == 'cost')
            self.assertIn(f"R {f['per_km_rand']} per km driven", cost)
        r = self.analyze(customer_id=self.customer.id)
        self.assertIn('(median R', ' '.join(r['reasoning']))

    def test_half_up_rounding(self):
        self.assertEqual(pa.margin_against_floor(200, 175)['margin_pct'], 13)    # 12.5 -> 13 (not banker's 12)
        self.assertEqual(pa.margin_against_floor(200, 225)['margin_pct'], -13)   # -12.5 -> -13
        self.assertEqual(pa.margin_against_floor(200, 179)['margin_pct'], 11)    # 10.5 -> 11
        self.assertEqual(pa._rand(-2.5), -3)
        r = self.analyze()
        fwr = r['cost_floor']['floor_with_return']
        for c in r['choices']:
            self.assertEqual(c['margin_pct_if_empty_return'], pa.pct_half_up(c['price'] - fwr, c['price']))
        self.assertEqual(self.analyze(your_price=23322.5)['your_price']['price'], 23322.5)

    # K-4
    def test_price_sensitive_notice(self):
        self._platform()
        prices = [26600, 28700, 25000, 25500, 26000, 27000, 27500, 24000]
        for i, p in enumerate(prices):     # newest last: created in this order
            make_quote(self.company, self.customer, number=f'PS-{i}', total=p, destination='DBN', status='DECLINED',
                       outcome='rejected', pickup_location='Johannesburg', delivery_location='Durban',
                       was_sent=True)
        r = self.analyze(customer_id=self.customer.id)
        ps = [a for a in r['attention'] if a['code'] == 'price_sensitive']
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]['level'], 'info')
        self.assertLessEqual(len(ps[0]['message']), 150)
        lane = r['customer']['lane_acceptance']
        self.assertTrue(ps[0]['message'].startswith(
            f"Kestrel Mills accepted {lane['won']} of their last {lane['decided']} quotes on this lane."))
        self.assertTrue(ps[0]['message'].endswith('Declined at R 27 500 and R 24 000.'))
        # Display only: the prices don't move.
        self.assertEqual([c['price'] for c in r['choices']], [c['price'] for c in self.analyze()['choices']])

    def test_price_sensitive_by_recent_declines_above_median(self):
        lane = {'won': 3, 'decided': 5, 'rate_pct': 60}
        hist = [('rejected', 30000), ('accepted', 24000), ('rejected', 29000), ('accepted', 23000)]
        self.assertEqual(pa.price_sensitivity(lane, hist, market_median=25000),
                         {'won': 3, 'decided': 5, 'declined_prices': [30000, 29000]})
        self.assertIsNone(pa.price_sensitivity(lane, hist, market_median=31000))
        self.assertIsNone(pa.price_sensitivity(lane, hist, market_median=None))

    def test_payment_risk_message_is_short(self):
        from unittest import mock
        self.customer.name = 'Kloofnek Fresh Produce (Demo)'
        self.customer.save()
        risk = {'band': 'CRITICAL', 'stats': {'invoice_count': 9, 'late_count': 9}}
        with mock.patch('core.services.customer_risk.compute_customer_risk', return_value=risk):
            r = self.analyze(customer_id=self.customer.id)
        msg = next(a for a in r['attention'] if a['code'] == 'payment_risk')['message']
        self.assertEqual(msg, 'Kloofnek Fresh Produce (Demo) often pays very late: 9 of 9 recent invoices over 30 '
                              'days late. Ask for a deposit (e.g. 50% upfront) or shorter terms.')
        self.assertLessEqual(len(msg), 150)
        self.assertEqual(r['customer']['payment_risk']['basis'],
                         '9 of 9 recent invoices paid late or still unpaid more than 30 days after due')

    # K-5
    def test_band_edges_round_and_wide(self):
        for start in (12000, 18000, 24030, 40000):
            pa._MARKET_MEMO.clear()
            Quote.objects.all().delete()
            Company.objects.exclude(id=self.company.id).delete()
            self._platform(start=start)
            r = self.analyze(customer_id=self.customer.id)
            th = r['likelihood']['rules']['thresholds']
            med = r['market']['median']
            self.assertEqual(th['likely_max'] % 500, 0)
            self.assertEqual(th['even_max'] % 500, 0)
            self.assertGreaterEqual(th['even_max'] - th['likely_max'], max(1000, 0.06 * med))
            self.assertIn('raw_thresholds', r['likelihood']['rules'])

    def test_saved_band_equals_shown_band_for_200_prices(self):
        from core.services.pricing_decisions import score_final_price
        self._platform()
        fields = {'customer': self.customer, 'origin': 'JHB', 'destination': 'DBN', 'vehicle_type': 'Tautliner',
                  'distance': 568, 'trip_type': 'ONE_WAY', 'weight': 28000,
                  'pickup_location': 'Johannesburg', 'delivery_location': 'Durban'}
        floor = self.analyze()['cost_floor']['total']
        checked = 0
        for i in range(200):
            price = round(floor + 37.37 * i * 3.1, 2)
            shown = self.analyze(customer_id=self.customer.id, your_price=price)['your_price']['likelihood']
            saved = score_final_price(fields, {'final_price': price, 'floor': floor}, company=self.company,
                                      user=self.user)
            self.assertEqual(saved['band'], shown['band'], price)
            checked += 1
        self.assertEqual(checked, 200)

    # K-6
    def test_summaries_say_position_only(self):
        m = {'p25': 22000, 'median': 23400, 'p75': 26200}
        s = lambda p: pa._choice_summary('balanced', p, 20, m, 10, False)
        self.assertEqual(s(23400), 'At the lane median.')
        self.assertEqual(s(23600), 'At the lane median.')        # within ±1%
        self.assertEqual(s(22500), 'In the lower half of the market.')
        self.assertEqual(s(25000), 'In the upper half of the market.')
        self.assertEqual(s(21000), 'Below the middle half of the market.')
        self.assertEqual(s(27000), 'Above the middle half of the market.')
        self.assertEqual(pa._choice_summary('safe', 20000, 12, None, 10, False), '12% margin, priced from your cost floor.')

    # K-12
    def test_no_estimate_so_no_estimate_warning(self):
        # The hard-coded lane table is gone as market evidence, so there is
        # never an "estimate below floor" warning or an estimate tier.
        r = self.analyze(origin='JHB', destination='CPT', distance_km=1400, one_way_distance_km=1400)
        self.assertNotIn('estimate_below_floor', {w['code'] for w in r['warnings']})
        self.assertNotEqual(r['market']['tier'], 'estimate')

    # K-13
    def test_fuel_label_names_the_zone_setting(self):
        self.company.fuel_zone = 'COASTAL'
        self.company.save()
        fuel = next(ln for ln in self.analyze(fuel_zone='COASTAL')['cost_floor']['lines'] if ln['key'] == 'fuel')
        if fuel['source']['kind'] == 'official':
            self.assertTrue(fuel['source']['label'].endswith('(your fuel zone setting)'), fuel['source'])
        self.assertTrue(fuel['source']['zone_from_setting'])
        # QUOTE-RULES §1: the zone is the company's; a payload zone can't change it.
        other = next(ln for ln in self.analyze(fuel_zone='INLAND')['cost_floor']['lines'] if ln['key'] == 'fuel')
        self.assertIn('coastal', other['source']['label'])

    def test_floor_working_never_reads_as_wrong_arithmetic(self):
        f = self.analyze(distance_km=605.8, one_way_distance_km=605.8, fuel_cost=7524, fuel_usage_litres=254.56,
                         fuel_price_used=29.5551)['cost_floor']
        fuel = next(ln for ln in f['lines'] if ln['key'] == 'fuel')
        self.assertTrue(fuel['basis'].startswith('≈ '), fuel['basis'])
        fixed = next(ln for ln in f['lines'] if ln['key'] == 'fixed_cost')
        self.assertIn('605,8 km', fixed['basis'].replace(' km', ' km'))

    # K-8
    def test_agreed_margin_on_detail(self):
        from core.services.pricing_decisions import save_pricing_decision
        q = make_quote(self.company, self.customer, number='AM-1', total=25100, status='ACCEPTED',
                       outcome='accepted')
        url = f'/api/v1/quotes/{q.id}/'
        body = self.api.get(url).json()
        self.assertIsNone(body['agreed_margin'])
        self.assertIsNone(body['agreed_margin_pct'])
        save_pricing_decision(q, {'version': 'pa-1', 'picked_choice': 'balanced', 'final_price': 25100,
                                  'floor': 20000}, user=self.user)
        QuoteOutcome.objects.create(quote=q, company=self.company, outcome='accepted', final_price=Decimal('24500'))
        body = self.api.get(url).json()
        self.assertEqual((body['agreed_margin'], body['agreed_margin_pct']), (4500, 18))
        self.assertIsNotNone(body['pricing_decision'])

    # K-9
    def test_company_profile_exposes_target_range(self):
        from core.serializers import CompanySerializer
        data = CompanySerializer(self.company).data
        self.assertEqual(data['margin_target_range'], [1, 40])
        self.assertTrue(CompanySerializer(self.company).fields['margin_target_range'].read_only)

    # K-11
    def test_pdf_cargo_row_only_with_real_cargo(self):
        from unittest import mock
        from core.services import quote_pdf
        self.assertIsNone(quote_pdf.quote_cargo_text('28t Superlink Tautliner', 'Superlink Tautliner'))
        self.assertIsNone(quote_pdf.quote_cargo_text('28 t', None))
        self.assertIsNone(quote_pdf.quote_cargo_text('', 'Tautliner'))
        self.assertEqual(quote_pdf.quote_cargo_text('maize in bags', 'Tautliner'), 'Maize in bags')

        def cells(q):
            with mock.patch.object(quote_pdf, 'Table', wraps=quote_pdf.Table) as table:
                self.assertTrue(quote_pdf.generate_quote_pdf_bytes(q).startswith(b'%PDF'))
            return [c for call in table.call_args_list for row in call.args[0] for c in row if isinstance(c, str)]
        blank = make_quote(self.company, self.customer, number='PDF-1', cargo_description='28t Tautliner',
                           vehicle_type='Tautliner')
        self.assertNotIn('Cargo', cells(blank))
        real = make_quote(self.company, self.customer, number='PDF-2', cargo_description='Maize in bags',
                          vehicle_type='Tautliner')
        self.assertIn('Cargo', cells(real))

    # r4 M1: never-sent quotes out of the win model's features and market reference
    def test_never_sent_quotes_not_in_model_features(self):
        from core.services import quote_features
        from core.services.lane_benchmark import resolve_market_rate
        won_quotes(self.company, self.customer, 3, start=24000, prefix='F', created_by=self.user)
        as_of = timezone.now() + timedelta(seconds=5)
        before = (quote_features.user_signals(self.user.id, as_of),
                  quote_features.lane_historical_acceptance_rate(self.company, 'JHB', 'DBN', as_of),
                  quote_features.customer_signals(self.company, self.customer.id, as_of)[:3],
                  resolve_market_rate('JHB', 'DBN', None, company=self.company, one_way_only=True, sent_only=True))
        q = make_quote(self.company, self.customer, number='NSF-1', total=90000, destination='DBN', status='DRAFT',
                       outcome='pending', created_by=self.user, pickup_location='Johannesburg',
                       delivery_location='Durban')
        q.status, q.outcome = 'DECLINED', 'rejected'
        q.save()
        q.refresh_from_db()
        self.assertIs(q.was_sent, False)
        after = (quote_features.user_signals(self.user.id, as_of),
                 quote_features.lane_historical_acceptance_rate(self.company, 'JHB', 'DBN', as_of),
                 quote_features.customer_signals(self.company, self.customer.id, as_of)[:3],
                 resolve_market_rate('JHB', 'DBN', None, company=self.company, one_way_only=True, sent_only=True))
        self.assertEqual(before, after)


class Round5ModelTests(_ModelMixin, _Base):
    def setUp(self):
        super().setUp()
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn not installed')
        self.train_company_model(self.company, self.user, self.customer)

    def test_model_headline_short_and_vocabulary(self):
        r = self.analyze(**self.model_payload())
        lk = r['likelihood']
        self.assertEqual(lk['level'], 'model', lk.get('reason'))
        self.assertEqual(lk['reason_code'], 'model')
        self.assertEqual(lk['headline'], f"Chance to win from {lk['model']['basis_label']}.")
        rec = r['recommendation']
        self.assertIn(rec['code'], ('highest_ep', 'level_with', 'within_ep', 'excluded_low_chance'))
        self.assertLessEqual(len(rec['short']), 90)
        self.assertIn('best', lk['model'])                                   # kept for audits
        text = ' '.join(r['reasoning'])
        self.assertEqual(text.count('best expected profit'), 0)
        self.assertIn('Chance to win comes from a model trained on', text)
        for it in r['reasoning_items']:
            self.assertIsNone(BANNED_WORDS.search(it['text']), it['text'])
        # Every model % is half-up of the raw probability.
        for c in r['choices']:
            if c['likelihood']['level'] == 'model':
                self.assertIsInstance(c['likelihood']['pct'], int)


class Round5ThrottleTests(_DecisionHelpers, _Base):
    """r5 M2: the analysis POST has its own throttle bucket, so analysis
    traffic can never 429 a quote Save."""

    def test_rate_default_and_view_scope(self):
        from django.conf import settings
        from core.throttling import PricingAnalysisRateThrottle
        from core.views_pricing_analysis import QuotePricingAnalysisView
        self.assertEqual(settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['pricing_analysis'], '600/minute')
        self.assertEqual(QuotePricingAnalysisView.throttle_classes, [PricingAnalysisRateThrottle])
        self.assertEqual(PricingAnalysisRateThrottle.scope, 'pricing_analysis')

    def test_many_analysis_calls_do_not_429_a_save(self):
        from unittest import mock
        from core.throttling import UserWriteRateThrottle
        # Shrink the write bucket so 12 analysis calls would exhaust it if they counted.
        with mock.patch.dict(UserWriteRateThrottle.THROTTLE_RATES, {'user_write': '3/minute'}):
            for i in range(12):
                resp = self.api.post(URL, base_payload(), format='json')
                self.assertEqual(resp.status_code, 200, f'analysis call {i + 1}: {resp.status_code}')
            resp = self.api.post('/api/v1/quotes/', self.quote_payload(), format='json')
            self.assertEqual(resp.status_code, 201, resp.content)

    def test_analysis_bucket_is_limited_on_its_own(self):
        from unittest import mock
        from core.throttling import PricingAnalysisRateThrottle
        with mock.patch.dict(PricingAnalysisRateThrottle.THROTTLE_RATES, {'pricing_analysis': '2/minute'}):
            codes = [self.api.post(URL, base_payload(), format='json').status_code for _ in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            # ...and a Save is still allowed: the write bucket was never touched.
            resp = self.api.post('/api/v1/quotes/', self.quote_payload(), format='json')
            self.assertEqual(resp.status_code, 201, resp.content)


class Round5NeverSentClosedQuoteTests(_ModelMixin, _Base):
    """r5 M1: never-sent quotes (was_sent False) are not training rows and are
    not counted as "closed quotes"; was_sent NULL still counts."""

    def _outcomes(self, n, never_sent, prefix='NS', sent_true=0):
        make_outcomes(self.company, self.customer, self.user, n, prefix=prefix)
        self.no_market_range()
        ids = list(Quote.objects.filter(company=self.company, quote_number__startswith=f'{prefix}-')
                   .order_by('id').values_list('id', flat=True))
        # Spread across both classes (make_outcomes puts accepted first).
        ns = ids[::max(1, len(ids) // never_sent)][:never_sent] if never_sent else []
        Quote.objects.filter(id__in=ns).update(was_sent=False)
        rest = [i for i in ids if i not in ns]
        Quote.objects.filter(id__in=rest[:sent_true]).update(was_sent=True)
        return ns

    def test_counts_and_training_rows_exclude_never_sent(self):
        from core.services.win_prediction import model_progress
        self._outcomes(10, never_sent=3, sent_true=2)        # 5 NULL + 2 True count; 3 False don't
        self.assertEqual(QuoteOutcome.objects.filter(company=self.company).count(), 10)
        self.assertEqual(quote_training.closed_outcomes().filter(quote__company=self.company).count(), 7)
        X, y, n, _ = quote_training.build_win_training_matrix_for_scope('company', company_id=self.company.id)
        self.assertEqual(n, 7)
        _, _, n_user, _ = quote_training.build_win_training_matrix_for_scope('user', user_id=self.user.id)
        self.assertEqual(n_user, 7)
        prog = model_progress(self.user, self.company)
        self.assertEqual(prog['company']['outcomes_collected'], 7)
        self.assertEqual(prog['user']['outcomes_collected'], 7)
        self.assertEqual(quote_training.win_model_status(self.company)['outcomes_collected'], 7)
        reason, short, code, won, lost = pa._model_unavailable_reason(self.company, with_code=True)
        self.assertEqual((code, won + lost), ('few_closed', 7))
        self.assertIn(f'you have {won} won and {lost} lost so far', reason)
        self.assertEqual(short, 'Bands · 7 of 400 closed quotes')
        r = self.analyze()
        self.assertIn(f'you have {won} won and {lost} lost so far', r['likelihood']['reason'])
        self.assertEqual(r['likelihood']['short'], 'Bands · 7 of 400 closed quotes')
        self.assertEqual(
            pa.likelihood_headline('few_closed', thresholds={'likely_max': 1}, won=won, lost=lost),
            f'Chance to win as Likely, Even chance or Less likely. A % needs 200 won + 200 lost '
            f'(you have {won}, {lost}).')

    def test_nightly_sweep_counts_only_sent_or_unknown(self):
        self._outcomes(43, never_sent=5)                     # 38 count: well below the 200/200 gate
        summary = quote_training.retrain_company_win_models()
        self.assertEqual(summary['considered'], 0)
        self.assertNotIn(self.company.id, summary['results'])

    def test_model_n_closed_and_label_exclude_never_sent(self):
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn not installed')
        # 440, not 406: never_sent removal isn't perfectly class-balanced
        # (it spreads evenly over row INDEX, not over the accepted/rejected
        # split), so a thin 200/200 margin can leave one class one short.
        self._outcomes(440, never_sent=6, prefix='M')          # 434 trainable
        result = quote_training.retrain_win_model_for_scope('company', company_id=self.company.id)
        self.assertTrue(result.get('trained'), result)
        from core.services.quote_ml import _MODEL_CACHE
        _MODEL_CACHE.clear()
        r = self.analyze(**self.model_payload())
        lk = r['likelihood']
        self.assertEqual(lk['level'], 'model', lk.get('reason'))
        self.assertEqual(lk['model']['n_closed'], 434)
        self.assertTrue(lk['model']['basis_label'].startswith('434 closed quotes'), lk['model']['basis_label'])
        self.assertEqual(lk['headline'], f"Chance to win from {lk['model']['basis_label']}.")


class QuoteRulesFloorTests(_Base):
    """QUOTE-RULES.md §3, §6, §7, §10 through the pricing analysis."""

    def test_no_vehicle_types_blocks_never_a_generic_class(self):
        VehicleType.objects.all().delete()
        r = self.analyze(vehicle_type='')
        self.assertIn('vehicle', r['missing'])
        self.assertIn('no_vehicle', r['blocking'])
        self.assertFalse(r['cost_floor']['complete'])
        self.assertEqual(r['choices'], [])
        self.assertIsNone(r['cost_floor']['fixed_cost_per_km'])

    def test_choices_never_below_minimum_charge_or_target(self):
        self.company.minimum_charge = Decimal('60000')
        self.company.save()
        r = self.analyze(include_return=False)
        self.assertTrue(all(c['price'] >= 60000 for c in r['choices']))
        r = self.analyze(include_return=False, your_price=30000)
        self.assertIn('below_minimum_charge', r['blocking'])

    def test_warnings_have_the_contract_shape(self):
        r = self.analyze(your_price=1000, toll_cost=None, tolls_unknown=True)
        for w in r['warnings']:
            for key in ('code', 'severity', 'title', 'detail', 'impact_zar', 'actions', 'message'):
                self.assertIn(key, w)
            self.assertIn(w['severity'], ('block', 'warn'))
        self.assertIn('tolls_unknown', r['blocking'])
        self.assertIn('tolls', r['missing'])

    def test_own_diesel_off_warns_with_impact(self):
        self.company.fuel_price_mode, self.company.fuel_price_own = 'OWN', Decimal('29.00')
        self.company.save()
        r = self.analyze(include_return=False)
        w = next(w for w in r['warnings'] if w['code'] == 'diesel_own_off')
        self.assertLess(w['impact_zar'], 0)
        fuel = next(ln for ln in r['cost_floor']['lines'] if ln['key'] == 'fuel')
        self.assertEqual(fuel['price_per_litre'], 29.0)
        self.assertEqual(fuel['source']['kind'], 'user')

    def test_kg_capacity_and_per_class_operating_cost(self):
        VehicleType.objects.create(company=self.company, name='Rigid 8t', capacity=8000, max_distance=1000,
                                   base_rate=10, fuel_consumption_l_per_100km=24)
        self.company.operating_cost_per_km = Decimal('14.50')   # the fleet's (tri-axle) figure
        self.company.save()
        r = self.analyze(vehicle_type='Rigid 8t', weight=6000, include_return=False)
        self.assertEqual(r['costing']['vehicle']['capacity_t'], 8.0)
        # rigid class: the fleet (tri-axle) figure scaled by 11.00 / 14.50 -> R11,00/km
        self.assertEqual(r['cost_floor']['fixed_cost_per_km']['class'], 'rigid')
        self.assertEqual(r['cost_floor']['fixed_cost_per_km']['value'], 11.0)



class RecommendationHonestyTests(_Base):
    def test_less_likely_choice_is_never_recommended(self):
        choices = [{'key': k, 'label': k.title(), 'price': p, 'margin': p - 20000, 'margin_pct': 10,
                    'likelihood': {'level': 'rules', 'band': b}}
                   for k, p, b in (('safe', 25000, 'likely'), ('balanced', 26000, 'less_likely'),
                                   ('stretch', 27000, 'less_likely'))]
        rec = pa._never_recommend_less_likely(choices, {'key': 'balanced', 'code': 'rules_median'}, {})
        self.assertEqual(rec['key'], 'safe')
        for c in choices:
            c['likelihood']['band'] = 'less_likely'
        rec = pa._never_recommend_less_likely(choices, {'key': 'balanced', 'code': 'rules_median'}, {})
        self.assertIsNone(rec['key'])
        self.assertEqual(rec['code'], 'all_less_likely')

    def test_alternative_with_return_load(self):
        r = self.analyze()                 # 568 km one way: empty return in the floor by default
        alt = r['alternative_with_return_load']
        self.assertTrue(r['cost_floor']['include_return'])
        self.assertLess(alt['floor'], r['cost_floor']['total'])
        self.assertEqual(len(alt['choices']), 3)
        self.assertIsNone(self.analyze(include_return=False)['alternative_with_return_load'])


class FinalCopyTests(_Base):
    def test_minimum_charge_choices_never_mention_target_margin(self):
        self.company.minimum_charge = Decimal('90000')
        self.company.save()
        r = self.analyze(include_return=False, your_price=60000)
        self.assertTrue(all(c['summary'].startswith('At your minimum charge') for c in r['choices']
                            if c['price'] <= 90000))
        self.assertNotIn('below_target', {w['code'] for w in r['warnings']})
        self.assertIn('below_minimum_charge', {w['code'] for w in r['warnings']})

    def test_empty_return_gap_without_recommendation_says_none(self):
        rec = pa._never_recommend_less_likely(
            [{'key': k, 'label': k.title(), 'price': 1, 'margin': 0, 'likelihood': {'level': 'rules',
                                                                                  'band': 'less_likely'}}
             for k in ('safe', 'balanced', 'stretch')],
            {'key': 'balanced', 'code': 'empty_return_gap', 'short': 'x.', 'reason': 'Balanced is kept: x.'}, {})
        self.assertIsNone(rec['key'])
        self.assertNotIn('Balanced is kept', rec['reason'])


class PlatformPrivacyTests(_Base):
    """GBE -> HRE: a lane with one other operator (and the requesting
    company's own quotes) must never show that operator's prices."""

    def test_single_other_operator_never_exposed(self):
        other = Company.objects.create(company_name='Lone Operator')
        won_quotes(other, make_customer(other, 'Lone Customer'), 12, start=41000, prefix='LO',
                   origin='GBE', destination='HRE')
        won_quotes(self.company, self.customer, 12, start=30000, prefix='OWN', origin='GBE', destination='HRE')
        r = self.analyze(origin='GBE', destination='HRE', pickup_location='Gaborone', delivery_location='Harare')
        self.assertNotEqual(r['market']['tier'], 'platform')
        # Only the company's own quotes (30 000-35 500) may show; the other
        # operator's (41 000+) never.
        self.assertTrue(all(r['market'][k] is None or r['market'][k] < 40000 for k in ('p25', 'median', 'p75')))

    def test_platform_needs_three_others_and_ten_quotes(self):
        from core.services.lane_benchmark import resolve_market_range
        for i in range(2):
            o = Company.objects.create(company_name=f'Op {i}')
            won_quotes(o, make_customer(o, f'C{i}'), 6, start=40000, prefix=f'P{i}', origin='GBE', destination='HRE')
        self.assertNotEqual(resolve_market_range('GBE', 'HRE', company=self.company)['tier'], 'platform')
        o = Company.objects.create(company_name='Op 2')
        won_quotes(o, make_customer(o, 'C2'), 2, start=40000, prefix='P2', origin='GBE', destination='HRE')
        out = resolve_market_range('GBE', 'HRE', company=self.company)
        self.assertEqual(out['tier'], 'platform')
        self.assertTrue(all(out[k] % 500 == 0 for k in ('p25', 'median', 'p75')))


class RoundTripLikeForLikeTests(_Base):
    def test_customer_one_way_prices_doubled_for_round_trip_bands(self):
        platform_market(self.company, self.customer)
        one = self.analyze(customer_id=self.customer.id, include_return=False)
        rt = self.analyze(customer_id=self.customer.id, legs=2, trip_type='ROUND_TRIP', distance_km=1136)
        self.assertTrue(rt['market']['legs_scaled'])
        th1, th2 = one['likelihood']['rules']['thresholds'], rt['likelihood']['rules']['thresholds']
        # Both scaled together: the round-trip edges sit at about twice the one-way ones.
        self.assertGreater(th2['likely_max'], 1.8 * th1['likely_max'])


class FinalLowFixesTests(_Base):
    def test_customer_all_lanes_acceptance_is_last_180_days(self):
        from datetime import timedelta
        old = make_quote(self.company, self.customer, number='AL-OLD', status='ACCEPTED', outcome='accepted',
                         was_sent=True, origin='BFN', destination='PLK')
        Quote.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=200))
        make_quote(self.company, self.customer, number='AL-NEW', status='DECLINED', outcome='rejected',
                   was_sent=True, origin='BFN', destination='PLK')
        acc = self.analyze(customer_id=self.customer.id)['customer']['acceptance']
        self.assertEqual((acc['won'], acc['decided'], acc['window_days']), (0, 1, 180))

    def test_retrain_user_scope_without_id_exits_non_zero(self):
        from django.core.management import CommandError, call_command
        with self.assertRaises(CommandError):
            call_command('retrain_win_model', '--scope', 'user')
