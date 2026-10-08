"""Tests for the quote price check (core.services.quote_ai_pricing,
AIQuotePriceAnalysisView, AdminAIUsageView).

Since the 2026-10 cost redesign the per-quote check reads only stored,
verified figures: the SANRAL tariffs on TollPlaza, the approved driver
allowance (VerifiedRate), the official FuelPrice row and the lane benchmark.
It makes no web or OpenAI call and needs no API key; NoOutboundCalls proves
that for every check run here. The refresh job that keeps the figures
current is tested in test_verified_rates.py.

Mock points: quote_ai_pricing.official_fuel_price / lane_benchmark (fuel and
base rate come from the app's own data). Fixture dates are relative to today,
so "current" figures stay current whenever the suite runs."""

import socket
import unittest
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import AIQuotePriceAnalysis, Company, Customer, Quote, TollPlaza, VerifiedRate
from core.services.quote_ai_pricing import _toll_schedule_start

User = get_user_model()

TODAY = timezone.localdate()
PERIOD = _toll_schedule_start(TODAY)          # tolls + allowances run from 1 March
Y = PERIOD.year

TOLL_URL = 'https://sanral.test/tolls'
DRIVER_URL = 'https://nbcrfli.test/rates'
VERIFIED_ON = PERIOD + timedelta(days=1) if PERIOD + timedelta(days=1) <= TODAY else PERIOD

# What the app's own FIASA record and resolve_market_rate would return.
OFFICIAL_FUEL = {'price_per_litre': 29.11, 'other_zone_price': 28.24, 'zone': 'inland',
                 'effective_date': (TODAY - timedelta(days=5)).isoformat(), 'source': 'FIASA',
                 'verified_at': (TODAY - timedelta(days=4)).isoformat(), 'error': None}
# 13,391 fuel + 1,608.70 tolls (excl. VAT) + 243.63 driver (1 night) + 21.50 x 1,400 base:
# 21.50 is the implied rate.
BENCHMARK = {'rate': 13391 + 1608.70 + 243.63 + 21.5 * 1400, 'source': 'platform'}


def _stored_plaza(name, incl_vat, *, route='N1', verified_at=VERIFIED_ON, effective_from=PERIOD, found=True,
                  source_url=TOLL_URL, source_name='SANRAL tariffs'):
    from core.services.toll_calculator import tariff_excl_vat
    return {'plaza': name, 'route': route, 'plaza_id': 1, 'found': found, 'ambiguous': False,
            'tariff_incl_vat': incl_vat if found else None,
            'tariff_excl_vat': float(tariff_excl_vat(incl_vat)) if found else None,
            'effective_from': effective_from, 'verified_at': verified_at,
            'source_url': source_url, 'source_name': source_name}


# The stored SANRAL Class 4 tariffs (incl. VAT, as published) for the route.
STORED_TOLLS = {'sanral_class': 4, 'class_label': 'Class 4 (5+ axle heavy vehicle / combination)',
                'plazas': [_stored_plaza('Grasmere', 950.0), _stored_plaza('Huguenot', 900.0)]}
# The approved allowance in force (a test figure, not a real NBCRFLI rate).
ALLOWANCE = {'id': 1, 'rate_per_night': 243.63, 'allowance_type': 'nbcrfli', 'label': 'NBCRFLI driver allowance',
             'effective_from': PERIOD, 'verified_at': VERIFIED_ON, 'source_url': DRIVER_URL,
             'source_name': 'NBCRFLI rates'}


class NoOutboundCalls:
    """Fails the test if anything opens a socket to a non-loopback host,
    resolves a name, fetches a source page or constructs an OpenAI client."""

    def __init__(self, test):
        self.test = test
        real_connect = socket.socket.connect
        self.attempts = []

        def connect(sock, addr):
            host = addr[0] if isinstance(addr, tuple) else addr
            if sock.family in (socket.AF_INET, socket.AF_INET6) and host not in ('127.0.0.1', '::1', 'localhost'):
                self.attempts.append(('connect', addr))
                raise OSError('outbound network used by the price check')
            return real_connect(sock, addr)

        real_gai = socket.getaddrinfo

        def gai(host, *a, **k):
            if host not in ('127.0.0.1', '::1', 'localhost', None):
                self.attempts.append(('dns', host))
                raise socket.gaierror('outbound DNS used by the price check')
            return real_gai(host, *a, **k)

        self.patchers = [mock.patch.object(socket.socket, 'connect', connect),
                         mock.patch.object(socket, 'getaddrinfo', gai)]
        self.openai = mock.patch('openai.OpenAI')
        self.fetch = mock.patch('core.services.source_verification._fetch_uncached')
        self.batch = mock.patch('core.services.source_verification.SourceFetchBatch')

    def __enter__(self):
        for p in self.patchers:
            p.start()
        self.openai_mock = self.openai.start()
        self.fetch_mock = self.fetch.start()
        self.batch_mock = self.batch.start()
        return self

    def __exit__(self, *exc):
        for p in (*self.patchers, self.openai, self.fetch, self.batch):
            p.stop()
        if exc[0] is None:
            self.test.assertEqual(self.attempts, [], 'the price check made an outbound call')
            self.test.assertFalse(self.openai_mock.called, 'the price check constructed an OpenAI client')
            self.test.assertFalse(self.fetch_mock.called or self.batch_mock.called,
                                  'the price check fetched a source page')
        return False


def _no_win_model():
    """Pipeline tests must not score with whatever model files happen to
    be in this machine's MEDIA_ROOT."""
    from core.services.win_prediction import PredictionContext, heuristic_win_proba
    return mock.patch('core.services.win_prediction.resolve_prediction_context',
                      return_value=PredictionContext(False, None, 0, heuristic_win_proba))


def _own_data(fuel=OFFICIAL_FUEL, benchmark=BENCHMARK):
    """Patch the app-data lookups (fuel record + lane benchmark) used by the check."""
    return (mock.patch('core.services.quote_ai_pricing.official_fuel_price', return_value=dict(fuel)),
            mock.patch('core.services.quote_ai_pricing.lane_benchmark', return_value=dict(benchmark)))


def _seed_route_tariffs(verified_at=VERIFIED_ON, effective_from=PERIOD):
    """Grasmere and Huguenot (N1) with Class 4 (tariff_class_5) = R950 / R900 incl. VAT."""
    out = []
    for name, km, class4 in (('Grasmere', '1290.0', '950.00'), ('Huguenot', '105.0', '900.00')):
        # The real 2026 plazas are already in the test DB (migration 0070);
        # pin them to known test figures.
        out.append(TollPlaza.objects.update_or_create(name=name, route='N1', defaults=dict(
            direction='Cape Town → Johannesburg', location_km=Decimal(km), is_active=True,
            tariff_class_2=Decimal('50.00'), tariff_class_3=Decimal('150.00'), tariff_class_4=Decimal('236.00'),
            tariff_class_5=Decimal(class4), tariff_year=effective_from.year, tariff_effective_from=effective_from,
            tariff_source_url=TOLL_URL, tariff_source_name='SANRAL tariffs', tariff_verified_at=verified_at))[0])
    return out


def _approve_allowance(value='243.63', effective_from=PERIOD, key='nbcrfli', verified_at=VERIFIED_ON):
    return VerifiedRate.objects.create(
        kind='driver_allowance', key=key, label='NBCRFLI driver allowance', value=Decimal(value),
        published_value=Decimal(value), unit='per_night', effective_from=effective_from, source_url=DRIVER_URL,
        source_name='NBCRFLI rates', verified_at=verified_at, status='approved', approved_at=timezone.now())


def _make_company_customer_user(suffix=''):
    company = Company.objects.create(company_name=f'Acme Freight{suffix}')
    customer = Customer.objects.create(
        company=company, name='Big Client', email=f'client{suffix}@x.test',
        phone='', address='', city='', state='', zip_code='',
    )
    user = User.objects.create_user(username=f'ops-1{suffix}', password='x', company=company)
    return company, customer, user


def _make_quote(company, customer, number='AI-Q1'):
    return Quote.objects.create(
        company=company, customer=customer, quote_number=number,
        pickup_location='Johannesburg', delivery_location='Cape Town',
        origin='JHB', destination='CPT',
        cargo_description='general freight', weight=Decimal('18000'),
        base_rate=Decimal('30100'), fuel_surcharge=Decimal('12000'),
        toll_charges=Decimal('1800'), driver_allowance=Decimal('0'),
        additional_charges=Decimal('0'), total_amount=Decimal('43900'),
        valid_until=date.today() + timedelta(days=14), status='DRAFT',
    )


# Operator's own figures: 12000 + 1800 + 0 + 21.5 x 1400 = 43,900.
# Market: fuel 460 L x 29.11 = 13,390.60 -> R13,391 (whole rand, as
# QuoteBuilder rounds); tolls: stored 950 + 900 incl. VAT = 826.09 + 782.61
# = 1,608.70 excl. VAT (quotes price tolls excl. VAT, like main's route calc);
# driver 16 h driving -> 2 driving days = 1 night away x 243.63; base 21.50 =
# the benchmark's implied rate.
ANALYSIS_PAYLOAD = {
    'distance_km': 1400, 'one_way_distance_km': 1400, 'legs': 1, 'trip_type': 'ONE_WAY',
    'duration_minutes': 960, 'origin': 'JHB', 'destination': 'CPT', 'vehicle_type': 'Flatbed',
    'weight': 18000, 'fuel_cost': 12000, 'toll_cost': 1800, 'driver_cost': 0, 'cross_border_cost': 0,
    'fuel_usage_litres': 460, 'fuel_price_used': 26.09, 'fuel_consumption_l_per_100km': 32.9,
    'fuel_type': 'Diesel', 'fuel_zone': 'INLAND', 'base_rate_per_km': 21.50, 'market_rate': 44000,
    'route': {'road_type': 'Mostly Highway', 'terrain': ['Coastal'],
              'toll_breakdown': [{'plaza': 'Grasmere', 'tariff': 900}, {'plaza': 'Huguenot', 'tariff': 900}]},
}
EXPECTED_MARKET_PRICE = 13391 + 1608.70 + 243.63 + 30100


class CondensedContextTests(SimpleTestCase):
    def test_never_includes_geometry_sections_or_totals(self):
        from core.services.quote_ai_pricing import build_condensed_context

        payload = dict(ANALYSIS_PAYLOAD)
        payload['route'] = dict(payload['route'], geometry=[{'lat': 1, 'lon': 2}], sections=[{'type': 'motorway'}])
        flattened = str(build_condensed_context(payload))
        for banned in ('geometry', 'sections', 'margin_pct', 'quote_total', 'direct_cost'):
            self.assertNotIn(banned, flattened)

    def test_records_exact_toll_class_and_one_way_tolls(self):
        from core.services.quote_ai_pricing import build_condensed_context

        context = build_condensed_context(dict(ANALYSIS_PAYLOAD, legs=2, toll_cost=3600, distance_km=2800))
        self.assertEqual(context['tolls']['sanral_class'], 4)  # Flatbed = combination = SANRAL Class 4
        self.assertEqual(context['tolls']['operator_one_way_total_zar'], 1800)
        self.assertEqual(context['tolls']['plazas_one_way'], ['Grasmere', 'Huguenot'])
        self.assertEqual(context['lane']['legs'], 2)


def _clear_caches(test):
    cache.clear()


class AnalyzeQuotePriceTests(TestCase):
    """The whole check against real rows: TollPlaza tariffs, an approved
    allowance, and (patched) fuel + benchmark. No key, no network."""

    def setUp(self):
        _clear_caches(self)
        self.company, self.customer, self.user = _make_company_customer_user()
        self.quote = _make_quote(self.company, self.customer)
        _seed_route_tariffs()
        _approve_allowance()
        # QUOTE-RULES: the check prices on the cost floor, which needs an
        # official diesel price in force now.
        from core.models import VehicleType
        from core.tests.quote_rules_fixtures import add_vehicle, official_price_now
        official_price_now()
        add_vehicle(self.company, VehicleType.objects.create(
            company=self.company, name='Flatbed', capacity=30, max_distance=3000, base_rate=20,
            fuel_consumption_l_per_100km=33))
        for patcher in (_no_win_model(), *_own_data()):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, payload=ANALYSIS_PAYLOAD):
        from core.services.quote_ai_pricing import analyze_quote_price
        with NoOutboundCalls(self):
            return analyze_quote_price(payload=dict(payload), user=self.user, company=self.company, quote=self.quote)

    @override_settings(OPENAI_API_KEY='')
    def test_happy_path_from_stored_figures_with_no_key_and_no_outbound_call(self):
        with mock.patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            result = self._run()
        self.assertTrue(result['success'])
        items = result['cost_breakdown']
        self.assertEqual(items['fuel']['verdict'], 'needs_adjustment')
        self.assertEqual(items['fuel']['ai_value_zar'], 13390.6)        # to the cent, as compute()
        self.assertEqual(items['tolls']['verdict'], 'needs_adjustment')
        self.assertEqual(items['tolls']['ai_value_zar'], 1608.70)
        self.assertEqual(items['driver_allowance']['ai_value_zar'], 243.63)
        self.assertEqual((items['driver_allowance']['detail']['days'], items['driver_allowance']['detail']['nights']),
                         (2, 1))
        self.assertEqual({t: items[t]['verification_kind'] for t in items},
                         {'fuel': 'official', 'tolls': 'source', 'driver_allowance': 'source',
                          'base_rate': 'benchmark'})
        self.assertIn('NBCRFLI', items['driver_allowance']['reason'])
        self.assertEqual(result['verification_status'], 'verified')

        # QUOTE-RULES §7: the suggested combination is never below the target
        # price over the full cost floor (here the 1 400 km empty run home is
        # in the floor, so the benchmark's base rate is lifted to reach it).
        default = result['combinations'][result['default_choice_key']]
        floor = result['cost_floor']
        self.assertGreaterEqual(default['price_zar'], floor['target_price'] - 1)
        self.assertTrue(items['base_rate'].get('floor_adjusted'))
        # fuel, tolls, driver and the (floor-lifted) base rate are all toggleable
        self.assertEqual(len(result['combinations']), 16)
        self.assertEqual(result['win_model']['reason'], 'not_enough_history')
        # The empty run home comes from compute(): empty burn, operating cost,
        # return tolls and the extra nights.
        by = {ln['key']: ln['amount'] for ln in floor['lines'] if ln['leg'] == 'empty_return'}
        self.assertAlmostEqual(result['return_leg']['total_zar'], sum(by.values()), places=2)

        row = AIQuotePriceAnalysis.objects.get(id=result['usage_log_id'])
        self.assertEqual((row.status, row.trigger_type, row.model), ('success', 'check', 'stored-rates'))
        self.assertEqual((row.total_cost_usd, row.web_search_cost_usd, row.research_web_search_calls),
                         (Decimal('0'), Decimal('0'), 0))
        self.assertAlmostEqual(float(row.suggested_price_zar), default['price_zar'], places=2)
        self.assertEqual(row.raw_result['requested_trigger'], 'auto')

    def test_items_carry_verified_at_source_url_and_source_name(self):
        items = self._run()['cost_breakdown']
        self.assertEqual((items['tolls']['verified_at'], items['tolls']['source_url'], items['tolls']['source_name']),
                         (VERIFIED_ON.isoformat(), TOLL_URL, 'SANRAL tariffs'))
        self.assertEqual((items['driver_allowance']['verified_at'], items['driver_allowance']['source_url'],
                          items['driver_allowance']['source_name']),
                         (VERIFIED_ON.isoformat(), DRIVER_URL, 'NBCRFLI rates'))
        self.assertEqual(items['fuel']['source_url'],
                         'https://fuelsindustry.org.za/consumer-information/fuel-prices-current-past/')
        self.assertEqual(items['fuel']['verified_at'], OFFICIAL_FUEL['verified_at'])
        self.assertEqual(items['base_rate']['source_name'], 'platform benchmark for this lane')
        self.assertIsNone(items['base_rate']['verified_at'])
        plaza = items['tolls']['detail']['plazas'][0]
        self.assertEqual((plaza['verified_at'], plaza['effective_from'], plaza['source_url']),
                         (VERIFIED_ON.isoformat(), PERIOD.isoformat(), TOLL_URL))

    def test_toll_class_comes_from_the_vehicle_type(self):
        from core.models import VehicleType
        # This fleet's Flatbed is a 2-axle: SANRAL Class 2 = column tariff_class_3 = R150 at both plazas.
        VehicleType.objects.create(company=self.company, name='Flatbed', capacity=8, max_distance=1000,
                                   base_rate=10, sanral_toll_class=2)
        route = {'toll_breakdown': [{'plaza': 'Grasmere', 'tariff': 130.43}, {'plaza': 'Huguenot', 'tariff': 130.43}]}
        tolls = self._run(dict(ANALYSIS_PAYLOAD, toll_cost=260.86, route=route))['cost_breakdown']['tolls']
        self.assertEqual(tolls['detail']['sanral_class'], 2)
        self.assertEqual([p['published_tariff_incl_vat_zar'] for p in tolls['detail']['plazas']], [150.0, 150.0])
        self.assertEqual((tolls['verdict'], tolls['detail']['market_one_way_zar']), ('accurate', 260.86))

    def test_route_code_pins_the_plaza_and_unknown_or_inactive_plazas_are_not_verified(self):
        TollPlaza.objects.filter(name='Huguenot').update(is_active=False)
        route = {'toll_breakdown': [{'plaza': 'grasmere', 'tariff': 826.09, 'route': 'N1'},
                                    {'plaza': 'Huguenot', 'tariff': 782.61}]}
        tolls = self._run(dict(ANALYSIS_PAYLOAD, route=route))['cost_breakdown']['tolls']
        self.assertEqual([p['verified'] for p in tolls['detail']['plazas']], [True, False])
        self.assertEqual(tolls['detail']['plazas'][1]['note'], 'not in the SANRAL tariff table')
        self.assertEqual(tolls['verdict'], 'could_not_verify')
        # Wrong route code: no such plaza.
        wrong = {'toll_breakdown': [{'plaza': 'Grasmere', 'tariff': 826.09, 'route': 'N3'}]}
        self.assertFalse(self._run(dict(ANALYSIS_PAYLOAD, route=wrong))['cost_breakdown']['tolls']
                         ['detail']['plazas'][0]['verified'])

    def test_no_approved_allowance_is_unverified_with_a_note(self):
        VerifiedRate.objects.all().delete()
        driver = self._run()['cost_breakdown']['driver_allowance']
        self.assertEqual((driver['verdict'], driver['verification_kind'], driver['verification_note']),
                         ('could_not_verify', 'unverified', 'no approved allowance on record'))
        self.assertIn('admin', driver['reason'])

    def test_pending_or_future_allowances_are_not_used_and_the_newest_approved_wins(self):
        VerifiedRate.objects.all().delete()
        VerifiedRate.objects.create(kind='driver_allowance', key='nbcrfli', value=Decimal('999.00'), unit='per_night',
                                    effective_from=PERIOD, status='pending')
        self.assertEqual(self._run()['cost_breakdown']['driver_allowance']['verdict'], 'could_not_verify')
        _approve_allowance('200.00')
        _approve_allowance('250.00', effective_from=PERIOD)  # approved later, same date: the later approval wins
        _approve_allowance('300.00', effective_from=TODAY + timedelta(days=10))  # not in force yet
        self.assertEqual(self._run()['cost_breakdown']['driver_allowance']['detail']['rate_per_night_zar'], 250.0)

    def test_pricing_crash_writes_a_failed_row_at_zero_cost(self):
        with mock.patch('core.services.quote_ai_pricing.compute_pricing', side_effect=OverflowError('boom')):
            result = self._run()
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'failed')
        self.assertNotIn('—', result['message'])
        row = AIQuotePriceAnalysis.objects.get(id=result['usage_log_id'])
        self.assertEqual((row.status, row.failed_at_call, row.trigger_type, row.total_cost_usd),
                         ('failed', 'pricing', 'check', Decimal('0')))

    @override_settings(AI_PRICE_ANALYSIS_ENABLED=False)
    def test_kill_switch(self):
        with NoOutboundCalls(self):
            from core.services.quote_ai_pricing import analyze_quote_price
            result = analyze_quote_price(payload=dict(ANALYSIS_PAYLOAD), user=self.user, company=self.company)
        self.assertEqual((result['code'], result['reason']), ('unavailable', 'disabled'))
        self.assertEqual(AIQuotePriceAnalysis.objects.count(), 0)


class StoredTollTariffTests(TestCase):
    def test_lookup_by_name_class_and_route(self):
        from core.services.verified_rates import stored_toll_tariffs
        _seed_route_tariffs()
        rows = stored_toll_tariffs([{'plaza': 'GRASMERE'}, {'plaza': 'Huguenot', 'route': 'N1'},
                                    {'plaza': 'Nowhere'}], 4)
        self.assertEqual([r['found'] for r in rows], [True, True, False])
        self.assertEqual((rows[0]['tariff_incl_vat'], rows[0]['tariff_excl_vat']), (950.0, 826.09))
        self.assertEqual(rows[0]['verified_at'], VERIFIED_ON)
        self.assertEqual(stored_toll_tariffs([{'plaza': 'Grasmere'}], 1)[0]['tariff_incl_vat'], 50.0)
        self.assertFalse(stored_toll_tariffs([{'plaza': 'Grasmere'}], None)[0]['found'])


class SpendCapTests(TestCase):
    def setUp(self):
        self.company, self.customer, self.user = _make_company_customer_user()
        self.other, _, _ = _make_company_customer_user(suffix='-b')

    def _rows(self, company, n, cost='0', status='success'):
        for _ in range(n):
            AIQuotePriceAnalysis.objects.create(company=company, status=status, trigger_type='check',
                                                total_cost_usd=Decimal(cost))

    def test_defaults_are_cheap_check_friendly(self):
        from django.conf import settings
        self.assertEqual(settings.AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS, 200)
        self.assertEqual(settings.AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD, 5)
        self.assertEqual(settings.AI_QUOTE_ANALYSIS_COOLDOWN_SECONDS, 3)

    @override_settings(AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS=3)
    def test_company_daily_run_cap_counts_failed_runs_and_is_per_company(self):
        from core.services.quote_ai_pricing import check_spend_caps
        self._rows(self.company, 2)
        self.assertIsNone(check_spend_caps(self.company))
        self._rows(self.company, 1, status='failed')
        capped = check_spend_caps(self.company)
        self.assertEqual((capped['code'], capped['limit']), ('budget', 'company_daily_runs'))
        self.assertGreater(capped['retry_after_seconds'], 0)
        self.assertLessEqual(capped['retry_after_seconds'], 86401)
        self.assertIsNone(check_spend_caps(self.other))

    @override_settings(AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS=3)
    def test_yesterdays_runs_do_not_count(self):
        from core.services.quote_ai_pricing import check_spend_caps
        self._rows(self.company, 3)
        AIQuotePriceAnalysis.objects.update(created_at=timezone.now() - timedelta(days=2))
        self.assertIsNone(check_spend_caps(self.company))

    @override_settings(AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS=0, AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD=1.0)
    def test_platform_usd_budget_never_blocks_a_free_check(self):
        from core.services.quote_ai_pricing import check_spend_caps
        from core.services.verified_rate_refresh import budget_exhausted
        # The refresh job spent the whole day's budget ...
        AIQuotePriceAnalysis.objects.create(status='success', trigger_type='refresh', total_cost_usd=Decimal('1.5'))
        self.assertTrue(budget_exhausted())
        # ... which stops the job, not the (free) per-quote check.
        self.assertIsNone(check_spend_caps(self.company))


class ComputePricingTests(SimpleTestCase):
    def _price(self, tolls=None, allowance=..., fuel=None, benchmark=None, **payload_overrides):
        from core.services.quote_ai_pricing import compute_pricing
        return compute_pricing(dict(ANALYSIS_PAYLOAD, **payload_overrides), TODAY,
                               official_fuel=dict(fuel or OFFICIAL_FUEL), benchmark=dict(benchmark or BENCHMARK),
                               tolls=tolls or STORED_TOLLS, allowance=ALLOWANCE if allowance is ... else allowance)

    def _tolls(self, *plazas):
        return dict(STORED_TOLLS, plazas=list(plazas))

    # ---- fuel (official FIASA price) ----
    def test_fuel_within_one_percent_is_at_market(self):
        fuel = self._price(fuel_price_used=29.0)['cost_breakdown']['fuel']  # 0.4% below 29.11
        self.assertEqual((fuel['verdict'], fuel['ai_value_zar']), ('accurate', 12000.0))
        self.assertIn('inland', fuel['reason'])
        self.assertIn('coastal R 28,24/L', fuel['reason'])

    def test_no_official_price_means_fuel_is_not_verified(self):
        p = self._price(fuel={'price_per_litre': None, 'error': 'only diesel has an official monthly price (Petrol)'})
        self.assertEqual(p['cost_breakdown']['fuel']['verdict'], 'could_not_verify')
        self.assertIn('only diesel', p['cost_breakdown']['fuel']['reason'])

    def test_manual_fuel_price_names_truckwys_as_its_source(self):
        fuel = self._price(fuel=dict(OFFICIAL_FUEL, source='MANUAL'))['cost_breakdown']['fuel']
        self.assertEqual((fuel['source_name'], fuel['source_url'], fuel['verification_kind']),
                         ('Official price entered by TruckWys', None, 'official'))

    # ---- combinations / rounding ----
    def test_every_combination_price_is_the_sum_of_its_chosen_lines(self):
        p = self._price(cross_border_cost=750)
        for key, combo in p['combinations'].items():
            v = combo['values']
            self.assertAlmostEqual(combo['price_zar'],
                                   v['fuel'] + v['tolls'] + v['driver_allowance'] + v['base_rate'] + 750, places=2)
            self.assertEqual(key, '|'.join(f'{t}={combo["choices"][t]}' for t in
                                           ('fuel', 'tolls', 'driver_allowance', 'base_rate')))

    def test_base_rounds_to_whole_rand_and_fuel_to_the_cent(self):
        # 1398.6 km x R21.50 = 30,069.90 -> 30,070 (the builder's base line);
        # 459.73 L x R29.11 = 13,382.74 to the cent (the cost floor's fuel line).
        p = self._price(distance_km=1398.6, fuel_usage_litres=459.73)
        self.assertEqual(p['cost_breakdown']['base_rate']['current_value_zar'], 30070.0)
        self.assertEqual(p['cost_breakdown']['fuel']['ai_value_zar'], 13382.74)

    # ---- tolls (stored SANRAL tariffs) ----
    def test_correct_excl_vat_toll_is_at_market(self):
        # Main's route calc prices tolls excl. VAT: R950 / 1.15 = 826.09 and
        # R900 / 1.15 = 782.61. The stored tariffs are VAT inclusive, as
        # published, and are compared excl. VAT.
        route = {'toll_breakdown': [{'plaza': 'Grasmere', 'tariff': 826.09}, {'plaza': 'Huguenot', 'tariff': 782.61}]}
        tolls = self._price(toll_cost=1608.70, route=route)['cost_breakdown']['tolls']
        self.assertEqual((tolls['verdict'], tolls['ai_value_zar'], tolls['verification_kind']),
                         ('accurate', 1608.70, 'source'))
        self.assertEqual(tolls['detail']['market_one_way_zar'], 1608.70)
        self.assertEqual(tolls['detail']['vat_basis'], 'excl_vat')
        self.assertEqual([r['matches_yours'] for r in tolls['detail']['plazas']], [True, True])
        self.assertEqual([r['published_tariff_incl_vat_zar'] for r in tolls['detail']['plazas']], [950.0, 900.0])

    def test_vat_inclusive_toll_is_adjusted_down_to_excl_vat(self):
        tolls = self._price(toll_cost=1850)['cost_breakdown']['tolls']
        self.assertEqual((tolls['verdict'], tolls['ai_value_zar']), ('needs_adjustment', 1608.70))
        self.assertIn('excl. VAT', tolls['reason'])

    def test_implied_base_rate_uses_excl_vat_tolls(self):
        base = self._price()['cost_breakdown']['base_rate']
        self.assertEqual(base['detail']['implied_rate_per_km'], 21.5)

    def test_round_trip_tolls_are_one_way_market_times_legs(self):
        p = self._price(legs=2, toll_cost=3600, distance_km=2800)
        self.assertEqual(p['cost_breakdown']['tolls']['ai_value_zar'], 3217.4)

    def test_a_plaza_never_verified_on_its_source_is_not_used(self):
        tolls = self._price(self._tolls(_stored_plaza('Grasmere', 950.0),
                                        _stored_plaza('Huguenot', 900.0, verified_at=None)))['cost_breakdown']['tolls']
        self.assertEqual(tolls['verdict'], 'could_not_verify')
        self.assertEqual({r['plaza']: r['verified'] for r in tolls['detail']['plazas']},
                         {'Grasmere': True, 'Huguenot': False})
        self.assertEqual(tolls['detail']['plazas'][1]['note'], 'tariff not yet verified on its source')

    def test_last_years_schedule_is_not_used(self):
        old = PERIOD.replace(year=PERIOD.year - 1)
        tolls = self._price(self._tolls(_stored_plaza('Grasmere', 950.0, effective_from=old),
                                        _stored_plaza('Huguenot', 900.0, effective_from=old)))['cost_breakdown']['tolls']
        self.assertEqual(tolls['verdict'], 'could_not_verify')
        self.assertTrue(all(r['note'].startswith('stored tariff is from an earlier schedule')
                            for r in tolls['detail']['plazas']))

    def test_plaza_missing_from_the_table_is_not_verified(self):
        tolls = self._price(self._tolls(_stored_plaza('Grasmere', 950.0),
                                        _stored_plaza('Huguenot', 0, found=False)))['cost_breakdown']['tolls']
        self.assertEqual(tolls['detail']['plazas'][1]['note'], 'not in the SANRAL tariff table')
        self.assertIn('Huguenot not verified', tolls['reason'])

    def test_route_without_plazas_is_not_verified(self):
        tolls = self._price(route={'toll_breakdown': []}, toll_cost=0)['cost_breakdown']['tolls']
        self.assertEqual((tolls['verdict'], tolls['verification_note']), ('could_not_verify', 'no plazas on route'))

    def test_toll_provenance_is_the_oldest_verification(self):
        older = VERIFIED_ON - timedelta(days=30)
        tolls = self._price(self._tolls(_stored_plaza('Grasmere', 950.0),
                                        _stored_plaza('Huguenot', 900.0, verified_at=older, source_url='https://b.test/',
                                                      source_name='B')))['cost_breakdown']['tolls']
        self.assertEqual((tolls['verified_at'], tolls['source_url'], tolls['source_name']),
                         (older.isoformat(), 'https://b.test/', 'B'))
        self.assertEqual(len(tolls['sources']), 2)

    def test_unverified_items_say_so(self):
        empty = dict(STORED_TOLLS, plazas=[_stored_plaza('Grasmere', 0, found=False),
                                           _stored_plaza('Huguenot', 0, found=False)])
        p = self._price(empty, None, fuel={'price_per_litre': None}, benchmark={'rate': None, 'source': 'none'})
        self.assertEqual({t: i['verification_kind'] for t, i in p['cost_breakdown'].items()},
                         {t: 'unverified' for t in ('fuel', 'tolls', 'driver_allowance', 'base_rate')})
        self.assertEqual(p['verification_status'], 'unverified')

    def test_latest_but_not_current_fuel_price_is_labelled_as_such(self):
        stale = dict(OFFICIAL_FUEL, current=False, effective_date=(TODAY - timedelta(days=40)).isoformat())
        fuel = self._price(fuel=stale)['cost_breakdown']['fuel']
        self.assertIn('the latest official inland diesel price', fuel['reason'])
        self.assertIn('(effective ', fuel['reason'])
        self.assertEqual(fuel['verification_note'], 'latest official price on record (FIASA)')
        current = self._price()['cost_breakdown']['fuel']
        self.assertIn('the official inland diesel price', current['reason'])

    # ---- driver ----
    def test_same_day_trip_gets_no_night_out_allowance(self):
        d = self._price(duration_minutes=180)['cost_breakdown']['driver_allowance']  # 3 h, home that night
        self.assertEqual((d['verdict'], d['ai_value_zar'], d['detail']['nights']), ('accurate', 0.0, 0))
        self.assertEqual(d['detail']['market_total_zar'], 0.0)
        # A same-day round trip (2 x 4 h) is still one driving day.
        rt = self._price(duration_minutes=240, legs=2, distance_km=2800, toll_cost=3600)
        self.assertEqual(rt['cost_breakdown']['driver_allowance']['detail']['nights'], 0)
        self.assertEqual(rt['cost_breakdown']['driver_allowance']['verdict'], 'accurate')

    def test_multi_day_trip_pays_one_allowance_per_night_away(self):
        for minutes, days, nights in ((540, 1, 0), (541, 2, 1), (1080, 2, 1), (1081, 3, 2)):
            d = self._price(duration_minutes=minutes)['cost_breakdown']['driver_allowance']
            self.assertEqual((d['detail']['days'], d['detail']['nights']), (days, nights), minutes)
            self.assertEqual(d['detail']['market_total_zar'], round(243.63 * nights, 2), minutes)
            self.assertEqual(d['detail']['allowance_basis'], 'per_night_away')

    def test_driver_above_the_approved_allowance_is_flagged(self):
        d = self._price(duration_minutes=1500)['cost_breakdown']['driver_allowance']  # 25 h -> 3 days, 2 nights
        self.assertEqual((d['detail']['days'], d['detail']['nights'], d['ai_value_zar']), (3, 2, round(243.63 * 2, 2)))
        # An allowance above the approved figure overstates the night-out
        # allowance, so it is flagged (adjust down to the approved figure),
        # not silently kept as it was before.
        high = self._price(driver_cost=1000)['cost_breakdown']['driver_allowance']  # 960 min -> 2 days, 1 night
        self.assertEqual((high['verdict'], high['ai_value_zar']), ('needs_adjustment', 243.63))
        self.assertIn('above it', high['reason'])
        # An allowance matching the approved figure is at market.
        exact = self._price(driver_cost=243.63)['cost_breakdown']['driver_allowance']
        self.assertEqual((exact['verdict'], exact['ai_value_zar']), ('accurate', 243.63))

    def test_driver_without_driving_time_is_not_verified(self):
        self.assertEqual(self._price(duration_minutes=None)['cost_breakdown']['driver_allowance']['verdict'],
                         'could_not_verify')

    def test_allowance_from_before_the_current_period_is_not_used(self):
        old = dict(ALLOWANCE, effective_from=PERIOD.replace(year=PERIOD.year - 1))
        d = self._price(allowance=old)['cost_breakdown']['driver_allowance']
        self.assertEqual((d['verdict'], d['verification_note']), ('could_not_verify', 'out of date'))

    def test_sars_fallback_is_labelled(self):
        sars = dict(ALLOWANCE, allowance_type='sars_subsistence',
                    label='SARS daily subsistence allowance (meals & incidentals)', rate_per_night=595.0)
        d = self._price(allowance=sars)['cost_breakdown']['driver_allowance']
        self.assertEqual(d['verdict'], 'needs_adjustment')
        self.assertIn('SARS daily subsistence allowance', d['reason'])

    # ---- base rate (lane benchmark) ----
    def test_base_rate_inside_the_benchmark_band_is_at_market(self):
        base = self._price()['cost_breakdown']['base_rate']
        self.assertEqual(base['verdict'], 'accurate')
        self.assertEqual((base['detail']['market_low_per_km'], base['detail']['market_high_per_km']), (19.35, 23.65))

    def test_base_rate_below_or_above_the_band_moves_to_its_edge(self):
        low = self._price(base_rate_per_km=15)['cost_breakdown']['base_rate']
        self.assertEqual((low['verdict'], low['detail']['ai_rate_per_km'], low['ai_value_zar']),
                         ('needs_adjustment', 19.35, float(round(19.35 * 1400))))
        high = self._price(base_rate_per_km=30)['cost_breakdown']['base_rate']
        self.assertEqual(high['detail']['ai_rate_per_km'], 23.65)
        p = self._price(base_rate_per_km=15, distance_km=1398.6)
        default = p['combinations'][p['default_choice_key']]
        rate = p['cost_breakdown']['base_rate']['detail']['ai_rate_per_km']
        self.assertAlmostEqual(default['price_zar'], default['pass_through_zar'] + round(1398.6 * rate), places=2)

    def test_no_real_benchmark_or_a_round_trip_is_not_verified(self):
        for bench, overrides, note in (({'rate': 44000, 'source': 'estimate'}, {}, 'no benchmark for this lane'),
                                        ({'rate': None, 'source': 'none'}, {}, 'no benchmark for this lane'),
                                        (BENCHMARK, {'legs': 2}, 'benchmark is one-way'),
                                        ({'rate': 5000, 'source': 'company'}, {}, 'benchmark below pass-through')):
            base = self._price(benchmark=bench, **overrides)['cost_breakdown']['base_rate']
            self.assertEqual((base['verdict'], base['verification_note']), ('could_not_verify', note), note)

    # ---- return leg / status ----
    def test_empty_return_note_only_for_one_way(self):
        one_way = self._price()['return_leg']
        self.assertEqual((one_way['fuel_zar'], one_way['tolls_zar'], one_way['driver_zar']), (13391.0, 1608.7, 487.26))
        self.assertIsNone(self._price(legs=2, distance_km=2800, toll_cost=3600)['return_leg'])

    def test_status_is_derived_from_verified_count(self):
        self.assertEqual(self._price()['verification_status'], 'verified')
        self.assertEqual(self._price(allowance=None)['verification_status'], 'partially_verified')


def _sast(y, m, d):
    """00:01 SAST on that day: when an SA fuel adjustment takes effect."""
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, 0, 1, tzinfo=ZoneInfo('Africa/Johannesburg'))


class OfficialFuelPriceTests(TestCase):
    """Rows shaped like main's FuelPrice pipeline (0128 provenance fields)."""

    def _record(self, month, source='FIASA', effective=None, grade='50ppm', fetched=None,
                inland='29.1111', coastal='28.2391'):
        from core.models.fuel_price import FuelPrice
        return FuelPrice.objects.create(
            date=month, diesel_inland=Decimal(inland), diesel_coastal=Decimal(coastal),
            petrol_95=Decimal('26.92'), petrol_93=Decimal('26.10'), source=source,
            diesel_grade=grade, effective_from=effective,
            fetched_at=fetched or datetime(month.year, month.month, 20, 12, tzinfo=dt_timezone.utc))

    def _lookup(self, today, zone='INLAND', fuel_type='Diesel'):
        from core.services.quote_ai_pricing import official_fuel_price
        # Must never scrape from the request path: any call here is a failure.
        with mock.patch('core.services.fuel_price.fetch_fuel_prices') as fetch, \
                mock.patch('core.services.fuel_price._fetch_live') as live:
            out = official_fuel_price(fuel_type, zone, today)
        self.assertFalse(fetch.called or live.called, 'official_fuel_price scraped from the request path')
        return out

    def test_fiasa_record_for_the_month_in_force(self):
        self._record(date(2026, 9, 1), effective=_sast(2026, 9, 2))
        out = self._lookup(date(2026, 9, 24))
        self.assertEqual((out['price_per_litre'], out['zone'], out['effective_date'], out['current'], out['source']),
                         (29.1111, 'inland', '2026-09-02', True, 'FIASA'))
        self.assertEqual(self._lookup(date(2026, 9, 24), zone='COASTAL')['price_per_litre'], 28.2391)

    def test_before_the_first_wednesday_last_months_price_is_in_force(self):
        self._record(date(2026, 8, 1), effective=_sast(2026, 8, 5), inland='26.1721')
        self._record(date(2026, 9, 1), effective=_sast(2026, 9, 2))
        out = self._lookup(date(2026, 9, 1))  # the September change is on Wednesday 2 September
        self.assertEqual((out['price_per_litre'], out['effective_date'], out['current']),
                         (26.1721, '2026-08-05', True))

    def test_effective_from_not_fetched_at_decides_which_adjustment_it_is(self):
        # Re-scraped on 20 September, but FIASA still showed the August column.
        self._record(date(2026, 9, 1), effective=_sast(2026, 8, 5), inland='26.1721',
                     fetched=datetime(2026, 9, 20, 12, tzinfo=dt_timezone.utc))
        out = self._lookup(date(2026, 9, 24))
        self.assertEqual((out['price_per_litre'], out['effective_date'], out['current']),
                         (26.1721, '2026-08-05', False))

    def test_manual_override_is_official(self):
        self._record(date(2026, 9, 1), source='MANUAL', grade=None, effective=None, inland='30.0000')
        out = self._lookup(date(2026, 9, 24))
        self.assertEqual((out['price_per_litre'], out['effective_date'], out['current'], out['source']),
                         (30.0, '2026-09-01', True, 'MANUAL'))

    def test_fallback_scraper_and_legacy_grade_rows_are_not_official(self):
        from core.models.fuel_price import FuelPrice
        for source, grade in (('FALLBACK', None), ('AA_SA', None), ('FIASA', None), ('FIASA', '500ppm')):
            FuelPrice.objects.all().delete()
            self._record(date(2026, 9, 1), source=source, grade=grade, effective=_sast(2026, 9, 2))
            out = self._lookup(date(2026, 9, 24))
            self.assertIsNone(out['price_per_litre'], (source, grade))
            self.assertIsNotNone(out['error'])

    def test_no_row_means_no_price_and_no_scrape(self):
        self.assertEqual(self._lookup(date(2026, 9, 24))['error'], 'no official price on record')

    def test_a_long_out_of_date_price_is_not_used(self):
        self._record(date(2026, 6, 1), effective=_sast(2026, 6, 3))
        self.assertIsNone(self._lookup(date(2026, 9, 24))['price_per_litre'])

    def test_non_diesel_has_no_official_price(self):
        self.assertIsNone(self._lookup(date(2026, 9, 24), fuel_type='Petrol')['price_per_litre'])


class FuelVerifiedAtTests(TestCase):
    def test_fiasa_row_is_verified_when_it_was_scraped(self):
        from core.models.fuel_price import FuelPrice
        from core.services.quote_ai_pricing import official_fuel_price
        FuelPrice.objects.create(date=date(2026, 9, 1), diesel_inland=Decimal('29.1111'),
                                 diesel_coastal=Decimal('28.2391'), petrol_95=Decimal('26.92'),
                                 petrol_93=Decimal('26.10'), source='FIASA', diesel_grade='50ppm',
                                 effective_from=_sast(2026, 9, 2),
                                 fetched_at=datetime(2026, 9, 20, 12, tzinfo=dt_timezone.utc))
        self.assertEqual(official_fuel_price('Diesel', 'INLAND', date(2026, 9, 24))['verified_at'], '2026-09-20')


def _pricing_for_win_tests():
    from core.services.quote_ai_pricing import compute_pricing
    return compute_pricing(ANALYSIS_PAYLOAD, TODAY, official_fuel=dict(OFFICIAL_FUEL), benchmark=dict(BENCHMARK),
                           tolls=STORED_TOLLS, allowance=ALLOWANCE)


def _fitted_model(margin_mean, margin_sd, n=200):
    """A real StandardScaler + LogisticRegression pipeline on synthetic CORE
    rows, wrapped like WinProbabilityModel (model / metadata / predict_proba)."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from core.services import quote_features

    names = list(quote_features.CORE_FEATURES)
    rng = np.random.default_rng(7)
    rows, labels = [], []
    for _ in range(n):
        ratio = float(rng.normal(1.03, 0.08))
        f = {name: 0.0 for name in names}
        f.update(price_ratio=ratio, price_ratio_available=1.0, cost_to_market_ratio=ratio,
                 quoted_margin_pct=float(rng.normal(margin_mean, margin_sd)) if margin_sd else margin_mean)
        rows.append(quote_features.vectorize(f, names))
        labels.append(int(ratio < 1.03))
    pipe = make_pipeline(StandardScaler(), LogisticRegression()).fit(rows, labels)

    class Model:
        model = pipe
        metadata = {'feature_names': names}

        def predict_proba(self, features):
            return float(self.model.predict_proba([quote_features.vectorize(features, names)])[0, 1])
    return Model()


def _has_sklearn():
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


class WinProbabilityTests(TestCase):
    """The AI check scores through pricing_analysis.model_likelihood: the
    same model, market reference, domain and curve gates as the analysis."""
    def setUp(self):
        from core.services import pricing_analysis
        pricing_analysis._MARKET_MEMO.clear()
        self.addCleanup(pricing_analysis._MARKET_MEMO.clear)
        self.company, self.customer, self.user = _make_company_customer_user()

    def _attach(self, ctx, market=(44000.0, 'platform'), payload=None, floor_share=0.8):
        from core.services.quote_ai_pricing import _attach_win_probabilities
        p = _pricing_for_win_tests()
        floor = min(c['price_zar'] for c in p['combinations'].values()) * floor_share
        with mock.patch('core.services.win_prediction.resolve_prediction_context', return_value=ctx), \
                mock.patch('core.services.lane_benchmark.resolve_market_rate', return_value=market):
            info = _attach_win_probabilities(p['combinations'], p['default_choice_key'],
                                             payload or dict(ANALYSIS_PAYLOAD, customer_id=self.customer.id),
                                             self.user, self.company, floor_total=floor)
        return info, p

    @staticmethod
    def _model(fn, ratio_range=(0.5, 1.6)):
        """A model object like WinProbabilityModel: metadata with the training
        price-ratio range, predict_proba bound to it."""
        class M:
            metadata = {'price_ratio_range': list(ratio_range), 'feature_names': []}

            def predict_proba(self, f):
                return fn(f)
        return M().predict_proba

    def test_no_trained_model_means_no_probability(self):
        from core.services.win_prediction import PredictionContext, heuristic_win_proba
        info, p = self._attach(PredictionContext(available=False, scope=None, sample_count=3,
                                                 predict_proba=heuristic_win_proba))
        self.assertEqual((info['available'], info['reason']), (False, 'not_enough_history'))
        self.assertTrue(all(c['win_probability'] is None for c in p['combinations'].values()))

    def test_trained_model_scores_every_combination_on_the_training_market_rate(self):
        from core.services.win_prediction import PredictionContext
        seen = []

        def predict(f):
            seen.append(f)
            return max(0.0, min(1.0, 1.5 - f['price_ratio']))
        # The panel's displayed benchmark (payload market_rate) is ignored for
        # scoring: resolve_market_rate is what training used.
        info, p = self._attach(PredictionContext(True, 'user', 50, self._model(predict)), market=(40000.0, 'platform'),
                               payload=dict(ANALYSIS_PAYLOAD, market_rate=99999, customer_id=self.customer.id))
        self.assertEqual((info['available'], info['scope'], info['training_samples']), (True, 'user', 50))
        default = p['combinations'][p['default_choice_key']]
        self.assertAlmostEqual(default['win_probability'], round(1.5 - default['price_zar'] / 40000.0, 3), places=3)
        self.assertTrue(all(f['price_ratio_available'] == 1.0 and f['quoted_margin_pct'] == 0.0 for f in seen))
        cheapest = min(p['combinations'].values(), key=lambda c: c['price_zar'])
        dearest = max(p['combinations'].values(), key=lambda c: c['price_zar'])
        self.assertGreater(cheapest['win_probability'], dearest['win_probability'])

    def test_no_market_rate_means_no_probability(self):
        from core.services.win_prediction import PredictionContext
        info, p = self._attach(PredictionContext(True, 'global', 62, self._model(lambda f: 0.5)), market=(None, 'none'))
        self.assertEqual((info['available'], info['reason']), (False, 'no_market_rate'))

    def test_no_floor_means_no_probability(self):
        from core.services.quote_ai_pricing import _attach_win_probabilities
        from core.services.win_prediction import PredictionContext
        p = _pricing_for_win_tests()
        with mock.patch('core.services.win_prediction.resolve_prediction_context',
                        return_value=PredictionContext(True, 'global', 62, self._model(lambda f: 0.5))):
            info = _attach_win_probabilities(p['combinations'], p['default_choice_key'], dict(ANALYSIS_PAYLOAD),
                                             self.user, self.company, floor_total=None)
        self.assertEqual((info['available'], info['reason']), (False, 'floor_incomplete'))

    def test_flat_curve_is_not_used_like_the_analysis(self):
        from core.services.win_prediction import PredictionContext
        info, p = self._attach(PredictionContext(True, 'global', 62, self._model(lambda f: 0.5)))
        self.assertEqual((info['available'], info['reason']), (False, 'model_curve_unusable'))
        self.assertTrue(all(c['win_probability'] is None for c in p['combinations'].values()))

    def test_unreadable_training_range_is_not_used(self):
        from core.services.win_prediction import PredictionContext
        info, p = self._attach(PredictionContext(True, 'global', 62, lambda f: 0.4))
        self.assertEqual((info['available'], info['reason']), (False, 'outside_training_range'))

    def test_prices_past_the_model_domain_get_no_probability(self):
        from core.services.win_prediction import PredictionContext
        # Trained on 0.5-1.02 x market: a combination above 1.02 x R44 000 gets no %.
        info, p = self._attach(PredictionContext(True, 'user', 50, self._model(
            lambda f: max(0.0, min(1.0, 1.5 - f['price_ratio'])), ratio_range=(0.5, 1.02))), floor_share=0.5)
        self.assertTrue(info['available'])
        cap = 1.02 * 44000
        above = [c for c in p['combinations'].values() if c['price_zar'] > cap]
        below = [c for c in p['combinations'].values() if c['price_zar'] <= cap]
        self.assertTrue(above and below)
        self.assertTrue(all(c['win_probability'] is None for c in above))
        self.assertTrue(all(c['win_probability'] is not None for c in below))

    def test_prediction_failure_is_not_reported_as_missing_history(self):
        from core.services.win_prediction import PredictionContext

        def broken(_):
            raise AttributeError('sklearn version mismatch')
        info, p = self._attach(PredictionContext(True, 'global', 62, self._model(broken)))
        self.assertEqual((info['available'], info['reason']), (False, 'prediction_failed'))
        self.assertTrue(all(c['win_probability'] is None for c in p['combinations'].values()))

    @unittest.skipUnless(_has_sklearn(), 'scikit-learn not installed')
    def test_quote_unlike_the_training_data_gets_no_probability(self):
        from core.services.win_prediction import PredictionContext
        model = _fitted_model(margin_mean=22.5, margin_sd=11.0)  # served margin 0 is ~2 SD out
        info, p = self._attach(PredictionContext(True, 'global', 62, model.predict_proba))
        self.assertEqual((info['available'], info['reason']), (False, 'outside_training_range'))
        self.assertTrue(all(c['win_probability'] is None for c in p['combinations'].values()))

    @unittest.skipUnless(_has_sklearn(), 'scikit-learn not installed')
    def test_real_model_trained_like_the_panel_moves_with_price(self):
        from core.services.win_prediction import PredictionContext
        model = _fitted_model(margin_mean=0.0, margin_sd=0.0)  # QuoteBuilder-style rows: margin always 0
        info, p = self._attach(PredictionContext(True, 'global', 200, model.predict_proba))
        self.assertTrue(info['available'])
        probs = sorted({c['win_probability'] for c in p['combinations'].values()})
        self.assertGreater(len(probs), 1)


class TollClassTests(TestCase):
    def test_vehicle_type_sanral_class_wins_over_the_name(self):
        from core.models import VehicleType
        from core.services.quote_ai_pricing import _toll_class, build_condensed_context
        company, _, _ = _make_company_customer_user()
        # "Flatbed" guesses Class 4 by name; this fleet's Flatbed is a 2-axle.
        VehicleType.objects.create(company=company, name='Flatbed', capacity=8, max_distance=1000,
                                   base_rate=10, sanral_toll_class=2)
        self.assertEqual(_toll_class('Flatbed')[0], 4)
        self.assertEqual(_toll_class('Flatbed', company)[0], 2)
        self.assertEqual(build_condensed_context(ANALYSIS_PAYLOAD, company=company)['tolls']['sanral_class'], 2)


class AIQuotePriceAnalysisViewTests(TestCase):
    def setUp(self):
        _clear_caches(self)
        self.company, self.customer, self.user = _make_company_customer_user()
        self.quote = _make_quote(self.company, self.customer)
        _seed_route_tariffs()
        _approve_allowance()
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.user)
        for patcher in (_no_win_model(), *_own_data()):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, quote_id, **overrides):
        payload = dict(ANALYSIS_PAYLOAD, quote_id=quote_id, trigger_type='auto', **overrides)
        with NoOutboundCalls(self):
            return self.client_api.post('/api/v1/quotes/ai-price-analysis/', payload, format='json')

    def _captured_payload(self, **overrides):
        from core.services import quote_ai_pricing
        seen = {}
        real = quote_ai_pricing.analyze_quote_price

        def capture(**kwargs):
            seen.update(kwargs['payload'])
            return real(**kwargs)
        with mock.patch.object(quote_ai_pricing, 'analyze_quote_price', side_effect=capture):
            resp = self._post(overrides.pop('quote_id', self.quote.id), **overrides)
        return resp, seen

    @override_settings(OPENAI_API_KEY='')
    def test_works_without_an_openai_key_and_makes_no_outbound_call(self):
        with mock.patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            resp = self._post(self.quote.id)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['verification_status'], 'verified')
        row = AIQuotePriceAnalysis.objects.get(id=body['usage_log_id'])
        self.assertEqual((row.trigger_type, row.total_cost_usd, row.company_id), ('check', Decimal('0'), self.company.id))

    def test_cooldown_blocks_rapid_repeat_call_for_same_quote(self):
        self.assertEqual(self._post(self.quote.id).status_code, 200)
        resp2 = self._post(self.quote.id)
        self.assertEqual(resp2.status_code, 429)
        self.assertEqual(resp2.json()['error'], 'cooldown')
        self.assertEqual(AIQuotePriceAnalysis.objects.count(), 1)

    def test_cooldown_is_per_quote_not_global(self):
        other = _make_quote(self.company, self.customer, number='AI-Q2')
        self.assertEqual(self._post(self.quote.id).status_code, 200)
        self.assertEqual(self._post(other.id).status_code, 200)

    def test_view_forwards_trip_fuel_customer_and_route_keys(self):
        route = dict(ANALYSIS_PAYLOAD['route'],
                     toll_breakdown=[{'plaza': 'Grasmere', 'tariff': 826.09, 'route': 'N1'}])
        resp, seen = self._captured_payload(legs=2, trip_type='ROUND_TRIP', toll_cost=3600, distance_km=2800,
                                            cross_border_cost=750, customer_id=self.customer.id, route=route)
        data = resp.json()
        self.assertEqual(data['legs'], 2)
        self.assertEqual(data['cross_border_zar'], 750.0)
        self.assertEqual(seen['customer_id'], self.customer.id)
        self.assertEqual(seen['fuel_zone'], 'INLAND')
        self.assertEqual(seen['route']['toll_breakdown'], [{'plaza': 'Grasmere', 'tariff': 826.09, 'route': 'N1'}])

    def test_another_companys_customer_is_dropped(self):
        _, foreign_customer, _ = _make_company_customer_user(suffix='-b')
        _, seen = self._captured_payload(customer_id=foreign_customer.id)
        self.assertIsNone(seen['customer_id'])

    def test_absurd_input_never_crashes(self):
        resp, seen = self._captured_payload(distance_km=1e200, base_rate_per_km=1e200, route=['not', 'a', 'dict'])
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['success'])
        self.assertIsNone(seen['distance_km'])
        self.assertEqual(seen['route'], {})

    @override_settings(AI_PRICE_ANALYSIS_ENABLED=False)
    def test_kill_switch_returns_unavailable_and_keeps_the_cooldown_free(self):
        resp = self._post(self.quote.id)
        body = resp.json()
        self.assertEqual((resp.status_code, body['code'], body['reason'], body['retry_after_seconds']),
                         (503, 'unavailable', 'disabled', None))
        self.assertNotIn('—', body['message'])
        self.assertEqual(AIQuotePriceAnalysis.objects.count(), 0)
        with override_settings(AI_PRICE_ANALYSIS_ENABLED=True):
            self.assertEqual(self._post(self.quote.id).status_code, 200)

    @override_settings(AI_PRICE_ANALYSIS_COMPANY_DAILY_RUNS=1)
    def test_company_run_cap(self):
        self.assertEqual(self._post(self.quote.id).status_code, 200)
        other = _make_quote(self.company, self.customer, number='AI-Q2')
        resp = self._post(other.id)
        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual((body['code'], body['limit']), ('budget', 'company_daily_runs'))
        self.assertGreater(body['retry_after_seconds'], 0)
        self.assertEqual(resp['Retry-After'], str(body['retry_after_seconds']))
        self.assertEqual(AIQuotePriceAnalysis.objects.count(), 1)

    @override_settings(AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD=0.01)
    def test_spent_platform_budget_does_not_block_a_free_check(self):
        AIQuotePriceAnalysis.objects.create(status='success', trigger_type='refresh', total_cost_usd=Decimal('0.02'))
        self.assertEqual(self._post(self.quote.id).status_code, 200)

    def test_cooldown_and_throttle_have_stable_codes(self):
        self._post(self.quote.id)
        body = self._post(self.quote.id).json()
        self.assertEqual((body['code'], body['error'], body['retry_after_seconds']), ('cooldown', 'cooldown', 3))
        self.assertNotIn('—', body['message'])
        from rest_framework.throttling import ScopedRateThrottle
        with mock.patch.object(ScopedRateThrottle, 'allow_request', return_value=False), \
                mock.patch.object(ScopedRateThrottle, 'wait', return_value=11.2):
            resp = self._post(self.quote.id)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual((resp.json()['code'], resp.json()['retry_after_seconds']), ('throttled', 12))

    def test_success_response_shape_per_item(self):
        data = self._post(self.quote.id).json()
        for item in data['cost_breakdown'].values():
            self.assertIn(item['verification_kind'], ('official', 'benchmark', 'source', 'unverified'))
            for key in ('verification', 'verified_at', 'source_url', 'source_name', 'verdict', 'toggleable',
                        'current_value_zar', 'ai_value_zar', 'detail', 'sources'):
                self.assertIn(key, item)
        for key in ('combinations', 'default_choice_key', 'win_model', 'return_leg', 'references'):
            self.assertIn(key, data)

    def test_another_companys_quote_id_is_ignored(self):
        other_company, other_customer, _ = _make_company_customer_user(suffix='-b')
        foreign = _make_quote(other_company, other_customer, number='AI-FOREIGN')
        resp = self._post(foreign.id)
        self.assertEqual(resp.status_code, 200)
        row = AIQuotePriceAnalysis.objects.get(id=resp.json()['usage_log_id'])
        self.assertIsNone(row.quote_id)
        self.assertEqual(row.company_id, self.company.id)

    def test_malformed_quote_and_customer_ids_are_ignored(self):
        resp, seen = self._captured_payload(quote_id='abc', customer_id='1e999')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(seen['quote_id'])
        self.assertIsNone(seen['customer_id'])


class AdminAIUsageViewTests(TestCase):
    def setUp(self):
        self.company, self.customer, self.user = _make_company_customer_user()
        self.quote = _make_quote(self.company, self.customer)
        self.superuser = User.objects.create_user(username='super-1', password='x', is_superuser=True, is_staff=True)

    def _make_row(self, *, status='success', cost='0.050000', trigger='manual'):
        return AIQuotePriceAnalysis.objects.create(
            quote=self.quote, company=self.company, triggered_by=self.user, trigger_type=trigger,
            status=status, total_cost_usd=Decimal(cost),
            research_input_tokens=1000, research_output_tokens=200,
            structuring_input_tokens=500, structuring_output_tokens=100,
        )

    def test_totals_by_user_and_by_trigger(self):
        self._make_row(cost='0.05')
        self._make_row(cost='0.03')
        self._make_row(status='failed', cost='0.01')
        AIQuotePriceAnalysis.objects.create(company=self.company, triggered_by=self.user, trigger_type='check',
                                            status='success', total_cost_usd=Decimal('0'))
        client = APIClient()
        client.force_authenticate(user=self.superuser)
        data = client.get('/api/v1/admin/ai-usage/').json()
        self.assertEqual(data['all_time']['calls'], 4)
        self.assertEqual(data['all_time']['success_calls'], 3)
        self.assertEqual(data['all_time']['failed_calls'], 1)
        self.assertAlmostEqual(data['all_time']['total_cost_usd'], 0.09, places=4)
        self.assertEqual(data['all_time']['total_tokens'], 3 * (1000 + 200 + 500 + 100))
        self.assertEqual(data['by_user'][0]['calls'], 4)
        self.assertEqual(data['by_trigger']['check'], {'calls': 1, 'total_cost_usd': 0.0})
        self.assertEqual(data['by_trigger']['manual']['calls'], 3)

    def test_non_superuser_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        self.assertEqual(client.get('/api/v1/admin/ai-usage/').status_code, 403)


class RouteSnapshotSerializerTests(TestCase):
    def setUp(self):
        self.company, self.customer, self.user = _make_company_customer_user()
        self.quote = _make_quote(self.company, self.customer)
        Quote.objects.filter(id=self.quote.id).update(route_snapshot={'request': {'origin': 'JHB'}})
        self.quote.refresh_from_db()

    def test_size_cap(self):
        from rest_framework import serializers as drf_serializers
        from core.serializers import QuoteSerializer
        s = QuoteSerializer()
        self.assertEqual(s.validate_route_snapshot({'a': 1}), {'a': 1})
        with self.assertRaises(drf_serializers.ValidationError):
            s.validate_route_snapshot({'geometry': 'x' * 200_001})
        with self.assertRaises(drf_serializers.ValidationError):
            s.validate_route_snapshot(['not', 'an', 'object'])

    def test_left_out_of_list_responses_but_kept_on_detail(self):
        from core.serializers import QuoteSerializer
        self.assertEqual(QuoteSerializer(self.quote).data['route_snapshot'], {'request': {'origin': 'JHB'}})
        listed = QuoteSerializer(Quote.objects.filter(id=self.quote.id), many=True).data
        self.assertNotIn('route_snapshot', listed[0])
        self.assertIn('base_rate_per_km', listed[0])
