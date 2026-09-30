"""Tests for the OpenAI-based quote price-analysis feature
(core.services.quote_ai_pricing, AIQuotePriceAnalysisView, AdminAIUsageView).

Mock points: core.services.quote_ai_pricing._client (the OpenAI client),
core.services.source_verification._fetch_uncached (the source-page fetch),
and quote_ai_pricing.official_fuel_price / lane_benchmark (fuel and base
rate come from the app's own data, not the web). The two research calls
(tolls, driver) run in parallel, so the fake client routes by request
content. Fixture dates are relative to today, so "current" figures stay
current whenever the suite runs."""

import json
import threading
import types
import unittest
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import AIQuotePriceAnalysis, Company, Customer, Quote
from core.services.quote_ai_pricing import _toll_schedule_start

User = get_user_model()

TODAY = timezone.localdate()
PERIOD = _toll_schedule_start(TODAY)          # tolls + allowances run from 1 March
Y = PERIOD.year

TOLL_URL = 'https://sanral.test/tolls'
DRIVER_URL = 'https://nbcrfli.test/rates'

PAGES = {
    TOLL_URL: f'New toll tariffs from 1 March {Y}. Grasmere Class 4 R 950.00. Huguenot Class 4 R 900.00.',
    DRIVER_URL: f'Main agreement, from 1 March {Y}: night-out allowance R243.63 per day.',
}

# What the app's own FIASA record and resolve_market_rate would return.
OFFICIAL_FUEL = {'price_per_litre': 29.11, 'other_zone_price': 28.24, 'zone': 'inland',
                 'effective_date': (TODAY - timedelta(days=5)).isoformat(), 'error': None}
# 13,391 fuel + 1,608.70 tolls (excl. VAT) + 243.63 driver (1 night) + 21.50 x 1,400 base:
# 21.50 is the implied rate.
BENCHMARK = {'rate': 13391 + 1608.70 + 243.63 + 21.5 * 1400, 'source': 'platform'}


def _usage(input_tokens=1000, cached_tokens=0, output_tokens=200, reasoning_tokens=0):
    return types.SimpleNamespace(
        input_tokens=input_tokens,
        input_tokens_details=types.SimpleNamespace(cached_tokens=cached_tokens),
        output_tokens=output_tokens,
        output_tokens_details=types.SimpleNamespace(reasoning_tokens=reasoning_tokens),
        total_tokens=input_tokens + output_tokens,
    )


def _citation(url, title=None):
    return types.SimpleNamespace(type='url_citation', url=url, title=title or url)


def _research_response(text, citations, usage=None):
    content = types.SimpleNamespace(type='output_text', text=text, annotations=citations)
    output = [types.SimpleNamespace(type='web_search_call'), types.SimpleNamespace(type='message', content=[content])]
    return types.SimpleNamespace(output_text=text, output=output, usage=usage or _usage())


def _structuring_response(extracted, usage=None, raw_text=None):
    text = json.dumps(extracted) if raw_text is None else raw_text
    return types.SimpleNamespace(output_text=text, output=[], usage=usage or _usage())


def _topic_of(kwargs):
    prompt = kwargs['input'][1]['content']
    for phrase, topic in (('SANRAL toll tariffs', 'tolls'), ('subsistence', 'driver_allowance')):
        if phrase in prompt:
            return topic
    raise AssertionError(f'unknown topic prompt: {prompt}')


DEFAULT_RESEARCH = {
    'tolls': ('Grasmere R950, Huguenot R900', [_citation(TOLL_URL, 'SANRAL tariffs')]),
    'driver_allowance': ('R243.63 per day', [_citation(DRIVER_URL, 'NBCRFLI rates')]),
}


def _default_extracted(ids):
    """Figures as the structuring model would extract them, cited by the ids
    the analysis assigned to each URL. A URL the research never cited has no
    id, so (like the strict enum) it can't be referenced."""
    def cite(url):
        return [ids[url]] if url in ids else []
    return {
        'tolls': {'plazas': [
            {'plaza': 'Grasmere', 'tariff_zar': 950.0, 'effective_date': PERIOD.isoformat(), 'sources': cite(TOLL_URL)},
            {'plaza': 'Huguenot', 'tariff_zar': 900.0, 'effective_date': PERIOD.isoformat(), 'sources': cite(TOLL_URL)},
        ]},
        'driver_allowance': {'rate_per_day_zar': 243.63, 'allowance_type': 'nbcrfli',
                             'effective_date': PERIOD.isoformat(), 'sources': cite(DRIVER_URL)},
    }


class FakeOpenAI:
    """Routes responses.create by content: research calls carry `tools`."""

    def __init__(self, research=None, extracted_fn=_default_extracted, fail_topics=(), structuring_error=None,
                 structuring_raw_text=None):
        self.research = dict(DEFAULT_RESEARCH, **(research or {}))
        self.extracted_fn = extracted_fn
        self.fail_topics = set(fail_topics)
        self.structuring_error = structuring_error
        self.structuring_raw_text = structuring_raw_text
        self.calls = []
        self._lock = threading.Lock()
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        if 'tools' in kwargs:
            topic = _topic_of(kwargs)
            if topic in self.fail_topics:
                raise RuntimeError(f'{topic} search failed')
            text, citations = self.research[topic]
            return _research_response(text, citations)
        if self.structuring_error:
            raise self.structuring_error
        sources = json.loads(kwargs['input'][1]['content'])['available_sources']
        ids = {s['url']: s['id'] for s in sources}
        return _structuring_response(self.extracted_fn(ids), raw_text=self.structuring_raw_text)

    @property
    def research_calls(self):
        return [c for c in self.calls if 'tools' in c]

    @property
    def structuring_calls(self):
        return [c for c in self.calls if 'tools' not in c]


def _fake_fetch(pages):
    def fetch(url):
        text = pages.get(url)
        return {'text': text, 'error': None} if text else {'text': None, 'error': 'http 403'}
    return fetch


def _no_win_model():
    """Pipeline tests must not score with whatever model files happen to
    be in this machine's MEDIA_ROOT."""
    from core.services.win_prediction import PredictionContext, heuristic_win_proba
    return mock.patch('core.services.win_prediction.resolve_prediction_context',
                      return_value=PredictionContext(False, None, 0, heuristic_win_proba))


def _own_data(fuel=OFFICIAL_FUEL, benchmark=BENCHMARK):
    """Patch the app-data lookups (fuel record + lane benchmark) used by the pipeline."""
    return (mock.patch('core.services.quote_ai_pricing.official_fuel_price', return_value=dict(fuel)),
            mock.patch('core.services.quote_ai_pricing.lane_benchmark', return_value=dict(benchmark)))


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
# QuoteBuilder rounds); tolls: published 950 + 900 incl. VAT = 826.09 + 782.61
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

    def test_sends_exact_toll_class_and_one_way_tolls(self):
        from core.services.quote_ai_pricing import build_condensed_context

        context = build_condensed_context(dict(ANALYSIS_PAYLOAD, legs=2, toll_cost=3600, distance_km=2800))
        self.assertEqual(context['tolls']['sanral_class'], 4)  # Flatbed = combination = SANRAL Class 4
        self.assertEqual(context['tolls']['operator_one_way_total_zar'], 1800)
        self.assertEqual(context['tolls']['plazas_one_way'], ['Grasmere', 'Huguenot'])
        self.assertEqual(context['lane']['legs'], 2)

    def test_prompts_ask_for_the_period_in_force_today(self):
        from core.services.quote_ai_pricing import _topic_prompt, build_condensed_context
        ctx = build_condensed_context(ANALYSIS_PAYLOAD, date(2026, 9, 24))
        self.assertIn('1 March 2026', _topic_prompt('tolls', ctx))
        driver = _topic_prompt('driver_allowance', ctx)
        self.assertIn('1 March 2026 to 28 February 2027', driver)
        self.assertIn('"2027" year of assessment', driver)
        self.assertIn('1 March 2025', _topic_prompt('tolls', build_condensed_context(ANALYSIS_PAYLOAD, date(2026, 2, 10))))


def _clear_caches(test):
    cache.clear()
    caches['ai_sources'].clear()
    test.addCleanup(caches['ai_sources'].clear)


class AnalyzeQuotePriceTests(TestCase):
    def setUp(self):
        _clear_caches(self)
        self.company, self.customer, self.user = _make_company_customer_user()
        self.quote = _make_quote(self.company, self.customer)
        for patcher in (_no_win_model(), *_own_data()):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, fake=None, pages=PAGES, payload=ANALYSIS_PAYLOAD):
        from core.services.quote_ai_pricing import analyze_quote_price
        fake = fake or FakeOpenAI()
        with mock.patch('core.services.quote_ai_pricing._client', return_value=fake), \
                mock.patch('core.services.source_verification._fetch_uncached', side_effect=_fake_fetch(pages)):
            result = analyze_quote_price(payload=payload, user=self.user, company=self.company, quote=self.quote)
        return result, fake

    def test_only_tolls_and_driver_are_searched(self):
        from core.services.quote_ai_pricing import (AI_QUOTE_ANALYSIS_MAX_WEB_SEARCH_CALLS,
                                                    AI_QUOTE_ANALYSIS_RESEARCH_TIMEOUT_SECONDS)
        _, fake = self._run()
        self.assertEqual({_topic_of(c) for c in fake.research_calls}, {'tolls', 'driver_allowance'})
        self.assertEqual(len(fake.research_calls), 2)
        for call in fake.research_calls:
            self.assertEqual(call['tool_choice'], 'required')
            self.assertEqual(call['max_tool_calls'], AI_QUOTE_ANALYSIS_MAX_WEB_SEARCH_CALLS)
            self.assertEqual(call['model'], 'gpt-4o-mini')
            self.assertNotIn('reasoning', call)
            self.assertLessEqual(call['timeout'], AI_QUOTE_ANALYSIS_RESEARCH_TIMEOUT_SECONDS)
        self.assertEqual(len(fake.structuring_calls), 1)
        structuring = fake.structuring_calls[0]
        self.assertNotIn('reasoning', structuring)
        self.assertEqual(structuring['temperature'], 0)
        self.assertLessEqual(structuring['timeout'], 15)

    def test_structuring_schema_is_extraction_only_with_this_runs_ids(self):
        _, fake = self._run()
        schema = fake.structuring_calls[0]['text']['format']['schema']
        self.assertEqual(sorted(schema['properties']), ['driver_allowance', 'tolls'])
        driver = schema['properties']['driver_allowance']
        self.assertEqual(driver['properties']['sources']['items']['enum'], ['S1', 'S2'])
        self.assertIn('effective_date', driver['required'])
        flattened = json.dumps(schema)
        for banned in ('verdict', 'suggested_price', 'price_reasoning'):
            self.assertNotIn(banned, flattened)

    def test_happy_path_prices_only_verified_market_figures(self):
        result, _ = self._run()
        self.assertTrue(result['success'])
        items = result['cost_breakdown']
        self.assertEqual(items['fuel']['verdict'], 'needs_adjustment')
        self.assertEqual(items['fuel']['ai_value_zar'], 13391.0)
        self.assertEqual(items['fuel']['sources'][0]['url'],
                         'https://fuelsindustry.org.za/consumer-information/fuel-prices-current-past/')
        self.assertEqual(items['tolls']['verdict'], 'needs_adjustment')
        self.assertEqual(items['tolls']['ai_value_zar'], 1608.70)
        self.assertEqual(items['driver_allowance']['ai_value_zar'], 243.63)
        self.assertEqual(items['driver_allowance']['detail']['days'], 2)
        self.assertEqual(items['driver_allowance']['detail']['nights'], 1)
        self.assertEqual({t: items[t]['verification_kind'] for t in items},
                         {'fuel': 'official', 'tolls': 'source', 'driver_allowance': 'source',
                          'base_rate': 'benchmark'})
        self.assertIn('NBCRFLI', items['driver_allowance']['reason'])
        self.assertEqual(items['base_rate']['verdict'], 'accurate')
        self.assertEqual(result['verification_status'], 'verified')

        default = result['combinations'][result['default_choice_key']]
        self.assertAlmostEqual(default['price_zar'], EXPECTED_MARKET_PRICE, places=2)
        self.assertEqual(default['margin_zar'], 30100.0)
        self.assertEqual(sorted(result['toggleable_items']), ['driver_allowance', 'fuel', 'tolls'])
        self.assertEqual(len(result['combinations']), 8)
        self.assertEqual(result['win_model']['reason'], 'not_enough_history')
        # The empty run home adds 2 more nights (32 h round trip = 3 nights, vs 1 one way).
        self.assertAlmostEqual(result['return_leg']['total_zar'], 13391 + 1608.70 + 487.26, places=2)

        row = AIQuotePriceAnalysis.objects.get(id=result['usage_log_id'])
        self.assertEqual(row.status, 'success')
        self.assertAlmostEqual(float(row.suggested_price_zar), EXPECTED_MARKET_PRICE, places=2)
        self.assertEqual(row.research_web_search_calls, 2)
        self.assertAlmostEqual(float(row.web_search_cost_usd), 0.02, places=6)  # block already in usage

    def test_figure_missing_from_its_source_page_is_not_used(self):
        from core.services.source_verification import REASON_NOT_FOUND
        pages = dict(PAGES, **{DRIVER_URL: f'From 1 March {Y}: night-out allowance R300.00 per day.'})
        driver = self._run(pages=pages)[0]['cost_breakdown']['driver_allowance']
        self.assertEqual((driver['verdict'], driver['verification_note']), ('could_not_verify', REASON_NOT_FOUND))
        self.assertEqual(driver['ai_value_zar'], 0.0)
        self.assertFalse(driver['toggleable'])

    def test_unreadable_source_page_is_not_verified(self):
        from core.services.source_verification import REASON_UNREADABLE
        result, _ = self._run(pages={TOLL_URL: PAGES[TOLL_URL]})
        driver = result['cost_breakdown']['driver_allowance']
        self.assertEqual((driver['verdict'], driver['verification_note']), ('could_not_verify', REASON_UNREADABLE))
        self.assertEqual(result['verification_status'], 'partially_verified')

    def test_one_search_failing_leaves_the_rest_working(self):
        result, _ = self._run(fake=FakeOpenAI(fail_topics={'driver_allowance'}))
        self.assertTrue(result['success'])
        self.assertEqual(result['cost_breakdown']['driver_allowance']['verdict'], 'could_not_verify')
        self.assertEqual(result['cost_breakdown']['tolls']['verdict'], 'needs_adjustment')

    def test_every_search_failing_still_checks_fuel_and_base_rate(self):
        result, fake = self._run(fake=FakeOpenAI(fail_topics={'tolls', 'driver_allowance'}))
        self.assertTrue(result['success'])
        self.assertEqual(len(fake.structuring_calls), 0)
        items = result['cost_breakdown']
        self.assertEqual(items['fuel']['verdict'], 'needs_adjustment')
        self.assertEqual(items['tolls']['verdict'], 'could_not_verify')
        row = AIQuotePriceAnalysis.objects.get(id=result['usage_log_id'])
        self.assertIn('tolls search failed', row.error_message)

    def test_structuring_failure_keeps_fuel_and_base_and_records_the_cost(self):
        result, _ = self._run(fake=FakeOpenAI(structuring_error=RuntimeError('boom')))
        self.assertTrue(result['success'])
        self.assertEqual(result['cost_breakdown']['tolls']['verdict'], 'could_not_verify')
        self.assertEqual(result['cost_breakdown']['fuel']['verdict'], 'needs_adjustment')
        row = AIQuotePriceAnalysis.objects.get(id=result['usage_log_id'])
        self.assertIn('structuring: boom', row.error_message)
        self.assertEqual(row.research_input_tokens, 2000)
        self.assertGreater(row.total_cost_usd, 0)

    def test_structuring_refusal_still_records_its_own_tokens(self):
        result, _ = self._run(fake=FakeOpenAI(structuring_raw_text=''))
        row = AIQuotePriceAnalysis.objects.get(id=result['usage_log_id'])
        self.assertEqual((row.structuring_input_tokens, row.structuring_output_tokens), (1000, 200))

    def test_pricing_crash_writes_a_failed_row_with_the_spend(self):
        with mock.patch('core.services.quote_ai_pricing.compute_pricing', side_effect=OverflowError('boom')):
            result, _ = self._run()
        self.assertFalse(result['success'])
        row = AIQuotePriceAnalysis.objects.get(id=result['usage_log_id'])
        self.assertEqual((row.status, row.failed_at_call), ('failed', 'pricing'))
        self.assertEqual(row.structuring_input_tokens, 1000)
        self.assertGreater(row.total_cost_usd, 0)

    def test_zero_citations_skips_structuring(self):
        no_cites = {t: ('NOT FOUND', []) for t in ('tolls', 'driver_allowance')}
        result, fake = self._run(fake=FakeOpenAI(research=no_cites))
        self.assertEqual(len(fake.structuring_calls), 0)
        self.assertTrue(result['success'])
        self.assertEqual(result['verification_status'], 'partially_verified')  # fuel + base still checked
        self.assertEqual(sorted(result['toggleable_items']), ['fuel'])


class ComputePricingTests(SimpleTestCase):
    SOURCES = {'S1': {'id': 'S1', 'title': 'SANRAL', 'url': TOLL_URL},
               'S2': {'id': 'S2', 'title': 'NBCRFLI', 'url': DRIVER_URL}}
    PAGE_RESULTS = {url: {'text': text, 'error': None} for url, text in PAGES.items()}
    IDS = {TOLL_URL: 'S1', DRIVER_URL: 'S2'}

    def _price(self, extracted=None, pages=None, fuel=None, benchmark=None, **payload_overrides):
        from core.services.quote_ai_pricing import compute_pricing
        extracted = extracted if extracted is not None else _default_extracted(self.IDS)
        return compute_pricing(extracted, dict(ANALYSIS_PAYLOAD, **payload_overrides), self.SOURCES,
                               pages if pages is not None else self.PAGE_RESULTS, TODAY,
                               official_fuel=dict(fuel or OFFICIAL_FUEL), benchmark=dict(benchmark or BENCHMARK))

    def _extracted(self, **overrides):
        data = _default_extracted(self.IDS)
        for key, value in overrides.items():
            data[key] = dict(data[key], **value) if isinstance(value, dict) else value
        return data

    def _page(self, url, text):
        return dict(self.PAGE_RESULTS, **{url: {'text': text, 'error': None}})

    # ---- fuel (official FIASA price) ----
    def test_fuel_within_one_percent_is_at_market(self):
        fuel = self._price(fuel_price_used=29.0)['cost_breakdown']['fuel']  # 0.4% below 29.11
        self.assertEqual((fuel['verdict'], fuel['ai_value_zar']), ('accurate', 12000.0))
        self.assertIn('inland', fuel['reason'])
        self.assertIn('coastal R28.24/L', fuel['reason'])

    def test_no_official_price_means_fuel_is_not_verified(self):
        p = self._price(fuel={'price_per_litre': None, 'error': 'only diesel has an official monthly price (Petrol)'})
        self.assertEqual(p['cost_breakdown']['fuel']['verdict'], 'could_not_verify')
        self.assertIn('only diesel', p['cost_breakdown']['fuel']['reason'])

    # ---- combinations / rounding ----
    def test_every_combination_price_is_the_sum_of_its_chosen_lines(self):
        p = self._price(cross_border_cost=750)
        for key, combo in p['combinations'].items():
            v = combo['values']
            self.assertAlmostEqual(combo['price_zar'],
                                   v['fuel'] + v['tolls'] + v['driver_allowance'] + v['base_rate'] + 750, places=2)
            self.assertEqual(key, '|'.join(f'{t}={combo["choices"][t]}' for t in
                                           ('fuel', 'tolls', 'driver_allowance', 'base_rate')))

    def test_fuel_and_base_round_to_whole_rand_like_the_quote_builder(self):
        # 1398.6 km x R21.50 = 30,069.90 -> 30,070; 459.73 L x R29.11 = 13,382.74 -> 13,383
        p = self._price(distance_km=1398.6, fuel_usage_litres=459.73)
        self.assertEqual(p['cost_breakdown']['base_rate']['current_value_zar'], 30070.0)
        self.assertEqual(p['cost_breakdown']['fuel']['ai_value_zar'], 13383.0)

    # ---- tolls ----
    def test_tolls_with_one_unconfirmed_plaza_are_not_verified(self):
        pages = self._page(TOLL_URL, f'Tariffs from 1 March {Y}. Grasmere Class 4 R 950.00')
        tolls = self._price(pages=pages)['cost_breakdown']['tolls']
        self.assertEqual(tolls['verdict'], 'could_not_verify')
        self.assertEqual({r['plaza']: r['verified'] for r in tolls['detail']['plazas']},
                         {'Grasmere': True, 'Huguenot': False})

    def test_last_years_schedule_or_a_bare_year_is_not_used(self):
        for eff, note in (((PERIOD - timedelta(days=365)).replace(day=1).isoformat(), 'out of date'),
                          (str(Y), 'no exact effective date')):
            extracted = self._extracted()
            for plaza in extracted['tolls']['plazas']:
                plaza['effective_date'] = eff
            tolls = self._price(extracted)['cost_breakdown']['tolls']
            self.assertEqual(tolls['verdict'], 'could_not_verify')
            self.assertTrue(all(r['note'].startswith(note) for r in tolls['detail']['plazas']), eff)

    def test_toll_page_must_carry_the_current_schedule_date(self):
        from core.services.source_verification import REASON_DATE_NOT_FOUND
        # Last year's schedule page: it ends in this year and has a "(c) {Y}" footer.
        pages = self._page(TOLL_URL, f'Tariffs 1 March {Y - 1} - 28 February {Y}. Grasmere R 950.00. '
                                     f'Huguenot R 900.00. (c) {Y} SANRAL')
        tolls = self._price(pages=pages)['cost_breakdown']['tolls']
        self.assertTrue(all(r['note'] == REASON_DATE_NOT_FOUND for r in tolls['detail']['plazas']))

    def test_round_trip_tolls_are_one_way_market_times_legs(self):
        p = self._price(legs=2, toll_cost=3600, distance_km=2800)
        self.assertEqual(p['cost_breakdown']['tolls']['ai_value_zar'], 3217.4)

    def test_extra_plazas_are_listed_but_not_priced(self):
        extracted = self._extracted()
        extracted['tolls']['plazas'].append({'plaza': 'Verkeerdevlei', 'tariff_zar': 400.0,
                                             'effective_date': PERIOD.isoformat(), 'sources': ['S1']})
        tolls = self._price(extracted=extracted)['cost_breakdown']['tolls']
        self.assertEqual(tolls['detail']['other_plazas_mentioned'], ['Verkeerdevlei'])
        self.assertEqual(tolls['ai_value_zar'], 1608.7)

    def test_a_ramp_plaza_never_stands_in_for_the_mainline_plaza(self):
        extracted = self._extracted()
        extracted['tolls']['plazas'].insert(0, {'plaza': 'Grasmere Ramp', 'tariff_zar': 58.0,
                                                'effective_date': PERIOD.isoformat(), 'sources': ['S1']})
        pages = self._page(TOLL_URL, f'From 1 March {Y}. Grasmere Ramp R58.00. Grasmere R 950.00. Huguenot R 900.00.')
        tolls = self._price(extracted, pages)['cost_breakdown']['tolls']
        grasmere = next(r for r in tolls['detail']['plazas'] if r['plaza'] == 'Grasmere')
        self.assertEqual(grasmere['market_tariff_zar'], 826.09)  # R950.00 incl. VAT
        self.assertEqual(grasmere['published_tariff_incl_vat_zar'], 950.0)
        self.assertIn('Grasmere Ramp', tolls['detail']['other_plazas_mentioned'])

    def test_plaza_names_match_on_whole_words(self):
        from core.services.quote_ai_pricing import _plaza_candidates
        found = [{'plaza': 'Kroonvaal'}, {'plaza': 'Tugela East Ramp'}, {'plaza': 'Mooi River Toll Plaza'},
                 {'plaza': 'N3 Tugela Mainline'}]
        self.assertEqual(_plaza_candidates('Vaal', found), [])
        self.assertEqual(_plaza_candidates('Tugela', found), [3])
        self.assertEqual(_plaza_candidates('Mooi', found), [2])

    def test_two_different_tariffs_for_one_name_are_not_guessed(self):
        extracted = self._extracted()
        extracted['tolls']['plazas'].append({'plaza': 'Grasmere Toll Plaza', 'tariff_zar': 990.0,
                                             'effective_date': PERIOD.isoformat(), 'sources': ['S1']})
        grasmere = next(r for r in self._price(extracted)['cost_breakdown']['tolls']['detail']['plazas']
                        if r['plaza'] == 'Grasmere')
        self.assertEqual((grasmere['verified'], grasmere['note']),
                         (False, 'more than one published plaza matches this name'))

    def test_correct_excl_vat_toll_is_at_market(self):
        # Main's route calc prices tolls excl. VAT: R950 / 1.15 = 826.09 and
        # R900 / 1.15 = 782.61. The published figures are matched on the page
        # as printed (incl. VAT), then compared excl. VAT.
        route = {'toll_breakdown': [{'plaza': 'Grasmere', 'tariff': 826.09}, {'plaza': 'Huguenot', 'tariff': 782.61}]}
        tolls = self._price(toll_cost=1608.70, route=route)['cost_breakdown']['tolls']
        self.assertEqual((tolls['verdict'], tolls['ai_value_zar'], tolls['verification_kind']),
                         ('accurate', 1608.70, 'source'))
        self.assertEqual(tolls['detail']['market_one_way_zar'], 1608.70)
        self.assertEqual([r['matches_yours'] for r in tolls['detail']['plazas']], [True, True])
        self.assertEqual([r['published_tariff_incl_vat_zar'] for r in tolls['detail']['plazas']], [950.0, 900.0])

    def test_vat_inclusive_toll_is_adjusted_down_to_excl_vat(self):
        tolls = self._price(toll_cost=1850)['cost_breakdown']['tolls']
        self.assertEqual((tolls['verdict'], tolls['ai_value_zar']), ('needs_adjustment', 1608.70))
        self.assertIn('excl. VAT', tolls['reason'])

    def test_implied_base_rate_uses_excl_vat_tolls(self):
        # BENCHMARK's implied 21.50/km only comes out if the tolls in the
        # market pass-through are excl. VAT.
        base = self._price()['cost_breakdown']['base_rate']
        self.assertEqual(base['detail']['implied_rate_per_km'], 21.5)

    def test_unverified_items_say_so(self):
        p = self._price(pages={}, fuel={'price_per_litre': None}, benchmark={'rate': None, 'source': 'none'})
        self.assertEqual({t: i['verification_kind'] for t, i in p['cost_breakdown'].items()},
                         {t: 'unverified' for t in ('fuel', 'tolls', 'driver_allowance', 'base_rate')})

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

    def test_driver_days_from_driving_time_and_yours_at_or_above_market_is_kept(self):
        d = self._price(duration_minutes=1500)['cost_breakdown']['driver_allowance']  # 25 h -> 3 days, 2 nights
        self.assertEqual((d['detail']['days'], d['detail']['nights'], d['ai_value_zar']), (3, 2, round(243.63 * 2, 2)))
        kept = self._price(driver_cost=1000)['cost_breakdown']['driver_allowance']
        self.assertEqual((kept['verdict'], kept['ai_value_zar']), ('accurate', 1000.0))

    def test_driver_without_driving_time_is_not_verified(self):
        self.assertEqual(self._price(duration_minutes=None)['cost_breakdown']['driver_allowance']['verdict'],
                         'could_not_verify')

    def test_substitute_allowance_is_not_used(self):
        other = self._price(self._extracted(driver_allowance={'allowance_type': 'other'}))
        self.assertEqual(other['cost_breakdown']['driver_allowance']['verification_note'],
                         'not a recognised driver allowance')

    def test_sars_rate_must_sit_in_the_current_tax_year_row(self):
        # SARS labels each row by the year the tax year ENDS.
        page = self._page(DRIVER_URL, f'Year of assessment | meals & incidentals | incidentals\n'
                                      f'{Y + 1} R595 R184\n{Y} R570 R176')
        current = self._price(self._extracted(driver_allowance={'allowance_type': 'sars_subsistence',
                                                                'rate_per_day_zar': 595.0}), page)
        d = current['cost_breakdown']['driver_allowance']
        self.assertEqual(d['verdict'], 'needs_adjustment')
        self.assertIn('SARS daily subsistence allowance', d['reason'])
        # Last year's R570 mislabelled with this year's start date is still caught.
        stale = self._price(self._extracted(driver_allowance={'allowance_type': 'sars_subsistence',
                                                              'rate_per_day_zar': 570.0}), page)
        self.assertEqual(stale['cost_breakdown']['driver_allowance']['verdict'], 'could_not_verify')

    def test_sars_tax_year_label_is_not_a_date(self):
        d = self._price(self._extracted(driver_allowance={'allowance_type': 'sars_subsistence',
                                                          'effective_date': str(Y)}))
        self.assertEqual(d['cost_breakdown']['driver_allowance']['verification_note'], 'no exact effective date')

    def test_future_period_is_not_in_force(self):
        d = self._price(self._extracted(driver_allowance={'effective_date': (TODAY + timedelta(days=3)).isoformat()}))
        self.assertEqual(d['cost_breakdown']['driver_allowance']['verification_note'], 'not in force yet')

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
        self.assertEqual(self._price(pages={}, fuel={'price_per_litre': None},
                                     benchmark={'rate': None, 'source': 'none'})['verification_status'], 'unverified')


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


def _pricing_for_win_tests():
    from core.services.quote_ai_pricing import compute_pricing
    return compute_pricing(_default_extracted(ComputePricingTests.IDS), ANALYSIS_PAYLOAD,
                           ComputePricingTests.SOURCES, ComputePricingTests.PAGE_RESULTS, TODAY,
                           official_fuel=dict(OFFICIAL_FUEL), benchmark=dict(BENCHMARK))


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
    def setUp(self):
        self.company, self.customer, self.user = _make_company_customer_user()

    def _attach(self, ctx, market=(44000.0, 'platform'), payload=None):
        from core.services.quote_ai_pricing import _attach_win_probabilities
        p = _pricing_for_win_tests()
        with mock.patch('core.services.win_prediction.resolve_prediction_context', return_value=ctx), \
                mock.patch('core.services.lane_benchmark.resolve_market_rate', return_value=market):
            info = _attach_win_probabilities(p['combinations'], p['default_choice_key'],
                                             payload or dict(ANALYSIS_PAYLOAD, customer_id=self.customer.id),
                                             self.user, self.company)
        return info, p

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
        info, p = self._attach(PredictionContext(True, 'user', 50, predict), market=(40000.0, 'platform'),
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
        info, p = self._attach(PredictionContext(True, 'global', 62, lambda f: 0.5), market=(None, 'none'))
        self.assertEqual((info['available'], info['reason']), (False, 'no_market_rate'))

    def test_prediction_failure_is_not_reported_as_missing_history(self):
        from core.services.win_prediction import PredictionContext

        def broken(_):
            raise AttributeError('sklearn version mismatch')
        info, p = self._attach(PredictionContext(True, 'global', 62, broken))
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


class UsdCostMathTests(SimpleTestCase):
    def test_gpt_4o_mini_search_fee_and_the_already_counted_block(self):
        from core.services import quote_ai_pricing as m
        pricing = m.OPENAI_PRICING['gpt-4o-mini']
        result = m._usd_cost(_usage(input_tokens=0, output_tokens=0), model='gpt-4o-mini', web_search_calls=4)
        self.assertAlmostEqual(result['search_cost_usd'], 0.04, places=6)
        with mock.patch.object(m, 'SEARCH_BLOCK_INCLUDED_IN_USAGE', False):
            separate = m._usd_cost(_usage(input_tokens=0, output_tokens=0), model='gpt-4o-mini', web_search_calls=4)
        self.assertAlmostEqual(separate['search_cost_usd'], 0.04 + 4 * 8000 / 1e6 * pricing['input_per_1m'], places=6)

    def test_token_pricing_and_cached_discount(self):
        from core.services.quote_ai_pricing import _usd_cost, OPENAI_PRICING
        pricing = OPENAI_PRICING['gpt-4o-mini']
        result = _usd_cost(_usage(input_tokens=1_000_000, cached_tokens=0, output_tokens=1_000_000), model='gpt-4o-mini')
        self.assertAlmostEqual(result['input_cost_usd'], pricing['input_per_1m'], places=4)
        self.assertAlmostEqual(result['output_cost_usd'], pricing['output_per_1m'], places=4)
        cached = _usd_cost(_usage(input_tokens=1_000_000, cached_tokens=1_000_000, output_tokens=0), model='gpt-4o-mini')
        self.assertAlmostEqual(cached['input_cost_usd'], pricing['cached_input_per_1m'], places=4)

    def test_unknown_model_is_costed_at_the_dearest_known_rate_not_zero(self):
        from core.services.quote_ai_pricing import _usd_cost, OPENAI_PRICING
        with self.assertLogs('core.services.quote_ai_pricing', level='WARNING'):
            cost = _usd_cost(_usage(input_tokens=1_000_000, output_tokens=0), model='gpt-unknown')
        self.assertEqual(cost['input_cost_usd'], max(p['input_per_1m'] for p in OPENAI_PRICING.values()))

    def test_model_field_default_matches_the_settings_default(self):
        from core.services.quote_ai_pricing import OPENAI_PRICING
        field = AIQuotePriceAnalysis._meta.get_field('model')
        self.assertEqual(field.default, 'gpt-4o-mini')
        self.assertIn(field.default, OPENAI_PRICING)


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

    def test_reasoning_kwargs_only_for_reasoning_models(self):
        from core.services.quote_ai_pricing import _reasoning_kwargs
        self.assertEqual(_reasoning_kwargs('gpt-4o-mini'), {})
        self.assertIn('reasoning', _reasoning_kwargs('gpt-5.6-luna'))


class AIQuotePriceAnalysisViewTests(TestCase):
    def setUp(self):
        _clear_caches(self)
        self.company, self.customer, self.user = _make_company_customer_user()
        self.quote = _make_quote(self.company, self.customer)
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.user)
        for patcher in (_no_win_model(), *_own_data()):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, fake, quote_id, **overrides):
        payload = dict(ANALYSIS_PAYLOAD, quote_id=quote_id, trigger_type='auto', **overrides)
        with mock.patch('core.services.quote_ai_pricing._client', return_value=fake), \
                mock.patch('core.services.source_verification._fetch_uncached', side_effect=_fake_fetch(PAGES)):
            return self.client_api.post('/api/v1/quotes/ai-price-analysis/', payload, format='json')

    def _captured_payload(self, **overrides):
        from core.services import quote_ai_pricing
        seen = {}
        real = quote_ai_pricing.analyze_quote_price

        def capture(**kwargs):
            seen.update(kwargs['payload'])
            return real(**kwargs)
        with mock.patch.object(quote_ai_pricing, 'analyze_quote_price', side_effect=capture):
            resp = self._post(FakeOpenAI(), overrides.pop('quote_id', self.quote.id), **overrides)
        return resp, seen

    def test_cooldown_blocks_rapid_repeat_call_for_same_quote(self):
        fake = FakeOpenAI()
        resp1 = self._post(fake, self.quote.id)
        self.assertEqual(resp1.status_code, 200)
        self.assertTrue(resp1.json()['success'])
        resp2 = self._post(fake, self.quote.id)
        self.assertEqual(resp2.status_code, 429)
        self.assertEqual(resp2.json()['error'], 'cooldown')
        self.assertEqual(len(fake.calls), 3)  # 2 research + 1 structuring, from the first request only

    def test_cooldown_is_per_quote_not_global(self):
        other = _make_quote(self.company, self.customer, number='AI-Q2')
        self.assertEqual(self._post(FakeOpenAI(), self.quote.id).status_code, 200)
        self.assertEqual(self._post(FakeOpenAI(), other.id).status_code, 200)

    def test_view_forwards_trip_fuel_and_customer_keys(self):
        resp, seen = self._captured_payload(legs=2, trip_type='ROUND_TRIP', toll_cost=3600, distance_km=2800,
                                            cross_border_cost=750, customer_id=self.customer.id)
        data = resp.json()
        self.assertEqual(data['legs'], 2)
        self.assertEqual(data['cross_border_zar'], 750.0)
        self.assertEqual(seen['customer_id'], self.customer.id)
        self.assertEqual(seen['fuel_zone'], 'INLAND')

    def test_another_companys_customer_is_dropped(self):
        _, foreign_customer, _ = _make_company_customer_user(suffix='-b')
        _, seen = self._captured_payload(customer_id=foreign_customer.id)
        self.assertIsNone(seen['customer_id'])

    def test_absurd_input_never_crashes_after_paying(self):
        resp, seen = self._captured_payload(distance_km=1e200, base_rate_per_km=1e200, route=['not', 'a', 'dict'])
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['success'])
        self.assertIsNone(seen['distance_km'])
        self.assertEqual(seen['route'], {})

    def test_another_companys_quote_id_is_ignored(self):
        other_company, other_customer, _ = _make_company_customer_user(suffix='-b')
        foreign = _make_quote(other_company, other_customer, number='AI-FOREIGN')
        resp = self._post(FakeOpenAI(), foreign.id)
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

    def _make_row(self, *, status='success', cost='0.050000'):
        return AIQuotePriceAnalysis.objects.create(
            quote=self.quote, company=self.company, triggered_by=self.user,
            status=status, total_cost_usd=Decimal(cost),
            research_input_tokens=1000, research_output_tokens=200,
            structuring_input_tokens=500, structuring_output_tokens=100,
        )

    def test_totals_and_by_user_breakdown(self):
        self._make_row(cost='0.05')
        self._make_row(cost='0.03')
        self._make_row(status='failed', cost='0.01')
        client = APIClient()
        client.force_authenticate(user=self.superuser)
        data = client.get('/api/v1/admin/ai-usage/').json()
        self.assertEqual(data['all_time']['calls'], 3)
        self.assertEqual(data['all_time']['success_calls'], 2)
        self.assertEqual(data['all_time']['failed_calls'], 1)
        self.assertAlmostEqual(data['all_time']['total_cost_usd'], 0.09, places=4)
        self.assertEqual(data['all_time']['total_tokens'], 3 * (1000 + 200 + 500 + 100))
        self.assertEqual(data['by_user'][0]['calls'], 3)

    def test_non_superuser_forbidden(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        self.assertEqual(client.get('/api/v1/admin/ai-usage/').status_code, 403)

