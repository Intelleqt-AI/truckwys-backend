"""Tests for the stored verified figures behind the quote price check:
the monthly refresh job (core.services.verified_rate_refresh), the review
workflow (core.services.verified_rates), the superuser endpoints under
/api/v1/admin/verified-rates/, the seed data migration and the schedule.

Mock points: core.services.verified_rate_refresh._client (the OpenAI client)
and core.services.source_verification._fetch_uncached (the source-page
fetch). No network, no real OpenAI client."""

import importlib
import json
import threading
import types
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import AIQuotePriceAnalysis, TollPlaza, VerifiedRate
from core.services.quote_ai_pricing import _toll_schedule_start

User = get_user_model()

TODAY = timezone.localdate()
PERIOD = _toll_schedule_start(TODAY)
Y = PERIOD.year
TOLL_URL = 'https://sanral.test/tolls-2026.pdf'
DRIVER_URL = 'https://nbcrfli.test/rates'
TEST_KEY = override_settings(OPENAI_API_KEY='sk-test-not-a-real-key')

# Grasmere's Class 4 tariff went up to R990; Huguenot's is unchanged at R900.
PAGES = {
    TOLL_URL: f'Toll tariffs effective 1 March {Y}. Grasmere Class 4 R 990.00. Huguenot Class 4 R 900.00.',
    DRIVER_URL: f'Main agreement, from 1 March {Y}: night-out allowance R243.63 per night.',
}


def _usage(input_tokens=1000, output_tokens=200):
    return types.SimpleNamespace(
        input_tokens=input_tokens, input_tokens_details=types.SimpleNamespace(cached_tokens=0),
        output_tokens=output_tokens, output_tokens_details=types.SimpleNamespace(reasoning_tokens=0))


def _citation(url, title):
    return types.SimpleNamespace(type='url_citation', url=url, title=title)


def _research_response(text, citations):
    content = types.SimpleNamespace(type='output_text', text=text, annotations=citations)
    output = [types.SimpleNamespace(type='web_search_call'), types.SimpleNamespace(type='message', content=[content])]
    return types.SimpleNamespace(output_text=text, output=output, usage=_usage())


def _toll_extraction(ids, grasmere=990.0, huguenot=900.0, eff=None):
    eff = eff or PERIOD.isoformat()
    return {'tolls': {'plazas': [
        {'plaza': 'Grasmere', 'tariff_zar': grasmere, 'effective_date': eff, 'sources': ids},
        {'plaza': 'Huguenot', 'tariff_zar': huguenot, 'effective_date': eff, 'sources': ids},
    ]}, 'driver_allowance': {'rate_per_day_zar': None, 'allowance_type': 'none', 'effective_date': None, 'sources': []}}


def _allowance_extraction(ids, rate=243.63, kind='nbcrfli', eff=None):
    return {'tolls': {'plazas': []}, 'driver_allowance': {
        'rate_per_day_zar': rate, 'allowance_type': kind, 'effective_date': eff or PERIOD.isoformat(), 'sources': ids}}


class FakeOpenAI:
    """Routes responses.create by prompt: research calls carry `tools`."""

    def __init__(self, tolls=_toll_extraction, allowance=_allowance_extraction, fail=()):
        self.tolls, self.allowance, self.fail = tolls, allowance, set(fail)
        self.calls = []
        self._lock = threading.Lock()
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        if 'tools' in kwargs:
            prompt = kwargs['input'][1]['content']
            topic = 'tolls' if 'SANRAL toll tariffs' in prompt else 'driver_allowance'
            if topic in self.fail:
                raise RuntimeError(f'{topic} search failed')
            if topic == 'tolls':
                return _research_response('Grasmere R990, Huguenot R900', [_citation(TOLL_URL, 'SANRAL 2026 tariffs')])
            return _research_response('R243.63 per night', [_citation(DRIVER_URL, 'NBCRFLI main agreement')])
        notes = json.loads(kwargs['input'][1]['content'])['research_notes']
        ids = [s['id'] for s in json.loads(kwargs['input'][1]['content'])['available_sources']]
        extracted = self.tolls(ids) if notes['tolls']['source_ids'] else self.allowance(ids)
        return types.SimpleNamespace(output_text=json.dumps(extracted), output=[], usage=_usage(500, 100))

    @property
    def research_calls(self):
        return [c for c in self.calls if 'tools' in c]


def _fake_fetch(pages):
    def fetch(url):
        text = pages.get(url)
        return {'text': text, 'error': None} if text else {'text': None, 'error': 'http 403'}
    return fetch


def _pin_plazas():
    """Only Grasmere and Huguenot active, Class 4 = R950 / R900 incl. VAT,
    verified last year."""
    TollPlaza.objects.update(is_active=False)
    old = date(Y - 1, 3, 1)
    out = {}
    for name, km, class4 in (('Grasmere', '1290.0', '950.00'), ('Huguenot', '105.0', '900.00')):
        out[name] = TollPlaza.objects.update_or_create(name=name, route='N1', defaults=dict(
            direction='Cape Town → Johannesburg', location_km=Decimal(km), is_active=True,
            tariff_class_2=Decimal('50.00'), tariff_class_3=Decimal('150.00'), tariff_class_4=Decimal('236.00'),
            tariff_class_5=Decimal(class4), tariff_year=old.year, tariff_effective_from=old,
            tariff_source_url='https://old.test/', tariff_source_name='old', tariff_verified_at=old))[0]
    return out


def _approved_allowance(value='243.63', effective_from=None, key='nbcrfli'):
    return VerifiedRate.objects.create(
        kind='driver_allowance', key=key, label='NBCRFLI driver allowance', value=Decimal(value),
        published_value=Decimal(value), unit='per_night', effective_from=effective_from or PERIOD,
        source_url=DRIVER_URL, source_name='NBCRFLI', verified_at=PERIOD, status='approved',
        approved_at=timezone.now())


@TEST_KEY
class RefreshJobTests(TestCase):
    def setUp(self):
        VerifiedRate.objects.filter(proposed_by='migration_0158').delete()   # tests own their allowance rows
        caches['ai_sources'].clear()
        self.addCleanup(caches['ai_sources'].clear)
        self.plazas = _pin_plazas()

    def _run(self, fake=None, pages=PAGES, **kwargs):
        from core.services.verified_rate_refresh import run_refresh
        fake = fake or FakeOpenAI()
        kwargs.setdefault('sanral_classes', [4])
        caches['ai_sources'].clear()  # each run fetches its pages afresh
        with mock.patch('core.services.verified_rate_refresh._client', return_value=fake), \
                mock.patch('core.services.source_verification._fetch_uncached', side_effect=_fake_fetch(pages)):
            return run_refresh(**kwargs), fake

    def test_changed_and_verified_figures_become_pending_proposals_only(self):
        summary, fake = self._run()
        self.assertEqual(summary['status'], 'ok')
        self.assertEqual(len(fake.research_calls), 2)  # one SANRAL class + the allowance
        for call in fake.research_calls:
            self.assertEqual((call['tool_choice'], call['max_tool_calls']), ('required', 2))
        toll = VerifiedRate.objects.get(kind='toll_tariff')
        grasmere = self.plazas['Grasmere']
        self.assertEqual((toll.status, toll.key, toll.sanral_class, toll.toll_plaza_id),
                         ('pending', f'toll:{grasmere.id}:class4', 4, grasmere.id))
        # Stored excl. VAT; the figure as printed and the old one are kept.
        self.assertEqual((toll.value, toll.published_value, toll.previous_value),
                         (Decimal('860.87'), Decimal('990.00'), Decimal('826.09')))
        self.assertEqual((toll.effective_from, toll.source_url, toll.source_name, toll.verified_at),
                         (PERIOD, TOLL_URL, 'SANRAL 2026 tariffs', TODAY))
        self.assertEqual(toll.proposed_by, 'refresh_verified_rates')
        # Never auto-applied.
        grasmere.refresh_from_db()
        self.assertEqual(grasmere.tariff_class_5, Decimal('950.00'))
        self.assertEqual(grasmere.tariff_verified_at, date(Y - 1, 3, 1))  # it changed: not re-verified
        # Huguenot matched on the current page: re-verified, still no tariff change.
        huguenot = self.plazas['Huguenot']
        huguenot.refresh_from_db()
        self.assertEqual((huguenot.tariff_class_5, huguenot.tariff_verified_at, huguenot.tariff_effective_from,
                          huguenot.tariff_source_url), (Decimal('900.00'), TODAY, PERIOD, TOLL_URL))
        # No allowance on record yet: the verified one is proposed, not applied.
        allowance = VerifiedRate.objects.get(kind='driver_allowance')
        self.assertEqual((allowance.status, allowance.key, allowance.value, allowance.previous_value),
                         ('pending', 'nbcrfli', Decimal('243.63'), None))
        self.assertFalse(VerifiedRate.objects.filter(status='approved').exists())

    def test_usage_rows_record_the_refresh_spend(self):
        summary, _ = self._run()
        rows = AIQuotePriceAnalysis.objects.filter(trigger_type='refresh')
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(r.company_id is None and r.status == 'success' for r in rows))
        self.assertTrue(all(r.research_web_search_calls == 1 and r.total_cost_usd > 0 for r in rows))
        self.assertAlmostEqual(summary['cost_usd'], float(sum(r.total_cost_usd for r in rows)), places=6)
        self.assertEqual(VerifiedRate.objects.get(kind='toll_tariff').refresh_run.trigger_type, 'refresh')

    def test_figure_not_on_its_source_page_is_never_proposed(self):
        pages = {TOLL_URL: f'Toll tariffs effective 1 March {Y}. Huguenot Class 4 R 900.00.',
                 DRIVER_URL: f'From 1 March {Y}: night-out allowance R300.00.'}
        summary, _ = self._run(pages=pages)
        self.assertFalse(VerifiedRate.objects.exists())
        self.assertIn('not found on the cited page', {u['note'] for u in summary['unverified']})

    def test_undated_last_years_or_unreadable_figures_are_never_proposed(self):
        for kwargs in ({'tolls': lambda ids: _toll_extraction(ids, eff=str(Y))},
                       {'tolls': lambda ids: _toll_extraction(ids, eff=date(Y - 1, 3, 1).isoformat())}):
            self._run(fake=FakeOpenAI(**kwargs), kinds=('toll_tariff',))
        self._run(pages={}, kinds=('toll_tariff',))
        self.assertFalse(VerifiedRate.objects.exists())

    def test_matching_figures_propose_nothing_and_refresh_verified_at(self):
        row = _approved_allowance()
        VerifiedRate.objects.filter(id=row.id).update(verified_at=PERIOD)
        TollPlaza.objects.filter(name='Grasmere').update(tariff_class_5=Decimal('990.00'))
        summary, _ = self._run()
        self.assertEqual(summary['proposals'], [])
        self.assertFalse(VerifiedRate.objects.filter(status='pending').exists())
        row.refresh_from_db()
        self.assertEqual(row.verified_at, TODAY)
        self.assertEqual(set(TollPlaza.objects.filter(is_active=True).values_list('tariff_verified_at', flat=True)),
                         {TODAY})

    def test_same_figure_is_not_proposed_twice_and_a_rejected_one_not_again(self):
        self._run()
        self._run()
        self.assertEqual(VerifiedRate.objects.filter(status='pending').count(), 2)
        VerifiedRate.objects.filter(kind='toll_tariff').update(status='rejected')
        summary, _ = self._run()
        self.assertEqual(VerifiedRate.objects.filter(kind='toll_tariff').count(), 1)
        self.assertIn('previously_rejected', {p['outcome'] for p in summary['proposals']})

    def test_a_newer_figure_supersedes_an_unreviewed_older_proposal(self):
        self._run(kinds=('toll_tariff',))
        pages = {TOLL_URL: f'Toll tariffs effective 1 March {Y}. Grasmere Class 4 R 995.00. Huguenot R 900.00.'}
        self._run(fake=FakeOpenAI(tolls=lambda ids: _toll_extraction(ids, grasmere=995.0)), pages=pages,
                  kinds=('toll_tariff',))
        statuses = dict(VerifiedRate.objects.values_list('published_value', 'status'))
        self.assertEqual(statuses, {Decimal('990.00'): 'superseded', Decimal('995.00'): 'pending'})

    def test_changed_allowance_is_proposed_against_the_approved_one(self):
        _approved_allowance('230.00', effective_from=PERIOD)
        self._run(kinds=('driver_allowance',))
        proposal = VerifiedRate.objects.get(status='pending')
        self.assertEqual((proposal.value, proposal.previous_value), (Decimal('243.63'), Decimal('230.00')))

    def test_a_failed_search_records_its_row_and_proposes_nothing(self):
        summary, _ = self._run(fake=FakeOpenAI(fail={'tolls'}))
        self.assertTrue(any('class 4' in e for e in summary['errors']))
        failed = AIQuotePriceAnalysis.objects.get(trigger_type='refresh', status='failed')
        self.assertEqual(failed.failed_at_call, 'research')
        self.assertFalse(VerifiedRate.objects.filter(kind='toll_tariff').exists())
        self.assertTrue(VerifiedRate.objects.filter(kind='driver_allowance').exists())

    @override_settings(AI_PRICE_ANALYSIS_ENABLED=False)
    def test_kill_switch_skips_without_a_client(self):
        with mock.patch('core.services.verified_rate_refresh._client') as client:
            from core.services.verified_rate_refresh import run_refresh
            summary = run_refresh()
        self.assertEqual((summary['status'], summary['reason']), ('skipped', 'disabled'))
        self.assertFalse(client.called)
        self.assertFalse(AIQuotePriceAnalysis.objects.exists())

    @override_settings(OPENAI_API_KEY='')
    def test_missing_key_skips_cleanly(self):
        from core.services.verified_rate_refresh import run_refresh
        with mock.patch.dict('os.environ', {'OPENAI_API_KEY': ''}), \
                mock.patch('openai.OpenAI') as real_client:
            summary = run_refresh()
        self.assertEqual((summary['status'], summary['reason']), ('skipped', 'no_api_key'))
        self.assertFalse(real_client.called)

    @override_settings(AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD=0.01)
    def test_budget_spent_skips_before_any_call(self):
        AIQuotePriceAnalysis.objects.create(status='success', trigger_type='refresh', total_cost_usd=Decimal('0.02'))
        summary, fake = self._run()
        self.assertEqual((summary['status'], summary['reason']), ('skipped', 'budget'))
        self.assertEqual(fake.calls, [])

    @override_settings(AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD=0.012)
    def test_budget_is_checked_before_every_lookup(self):
        # Each lookup costs about $0.0104 here: after two the cap is spent, so
        # the third (the allowance) never starts.
        summary, fake = self._run(sanral_classes=[3, 4])
        self.assertEqual((summary['status'], summary['reason'], summary['runs']), ('stopped', 'budget', 2))
        self.assertEqual(len(fake.research_calls), 2)

    def test_client_that_cannot_start_skips(self):
        from core.services.verified_rate_refresh import run_refresh
        with mock.patch('core.services.verified_rate_refresh._client', side_effect=RuntimeError('bad')):
            self.assertEqual(run_refresh()['reason'], 'client_error')


class RefreshPromptAndCostTests(SimpleTestCase):
    def test_prompts_ask_for_the_period_in_force_today(self):
        from core.services.verified_rate_refresh import topic_prompt
        tolls = topic_prompt('tolls', date(2026, 9, 24), sanral_class_label='Class 4', plazas=['Vaal', 'Mooi'])
        self.assertIn('1 March 2026', tolls)
        self.assertIn('Vaal, Mooi', tolls)
        driver = topic_prompt('driver_allowance', date(2026, 9, 24))
        self.assertIn('1 March 2026 to 28 February 2027', driver)
        self.assertIn('"2027" year of assessment', driver)
        self.assertIn('1 March 2025', topic_prompt('tolls', date(2026, 2, 10)))

    def test_structuring_schema_is_extraction_only(self):
        from core.services.verified_rate_refresh import build_structuring_schema
        schema = build_structuring_schema(['S1', 'S2'])['schema']
        self.assertEqual(sorted(schema['properties']), ['driver_allowance', 'tolls'])
        self.assertEqual(schema['properties']['driver_allowance']['properties']['sources']['items']['enum'], ['S1', 'S2'])
        for banned in ('verdict', 'suggested_price', 'price_reasoning'):
            self.assertNotIn(banned, json.dumps(schema))

    def test_plaza_names_match_on_whole_words(self):
        from core.services.verified_rate_refresh import _plaza_candidates
        found = [{'plaza': 'Kroonvaal'}, {'plaza': 'Tugela East Ramp'}, {'plaza': 'Mooi River Toll Plaza'},
                 {'plaza': 'N3 Tugela Mainline'}]
        self.assertEqual(_plaza_candidates('Vaal', found), [])
        self.assertEqual(_plaza_candidates('Tugela', found), [3])
        self.assertEqual(_plaza_candidates('Mooi', found), [2])

    def test_gpt_4o_mini_search_fee_and_the_already_counted_block(self):
        from core.services import verified_rate_refresh as m
        pricing = m.OPENAI_PRICING['gpt-4o-mini']
        result = m._usd_cost(_usage(input_tokens=0, output_tokens=0), model='gpt-4o-mini', web_search_calls=4)
        self.assertAlmostEqual(result['search_cost_usd'], 0.04, places=6)
        with mock.patch.object(m, 'SEARCH_BLOCK_INCLUDED_IN_USAGE', False):
            separate = m._usd_cost(_usage(input_tokens=0, output_tokens=0), model='gpt-4o-mini', web_search_calls=4)
        self.assertAlmostEqual(separate['search_cost_usd'], 0.04 + 4 * 8000 / 1e6 * pricing['input_per_1m'], places=6)

    def test_unknown_model_is_costed_at_the_dearest_known_rate_not_zero(self):
        from core.services.verified_rate_refresh import _usd_cost, OPENAI_PRICING
        with self.assertLogs('core.services.verified_rate_refresh', level='WARNING'):
            cost = _usd_cost(_usage(input_tokens=1_000_000, output_tokens=0), model='gpt-unknown')
        self.assertEqual(cost['input_cost_usd'], max(p['input_per_1m'] for p in OPENAI_PRICING.values()))

    def test_model_field_default_is_a_priced_model(self):
        from core.services.verified_rate_refresh import OPENAI_PRICING
        self.assertIn(AIQuotePriceAnalysis._meta.get_field('model').default, OPENAI_PRICING)

    def test_reasoning_kwargs_only_for_reasoning_models(self):
        from core.services.verified_rate_refresh import _reasoning_kwargs
        self.assertEqual(_reasoning_kwargs('gpt-4o-mini'), {})
        self.assertIn('reasoning', _reasoning_kwargs('gpt-5.6-luna'))

    def test_monthly_beat_schedule(self):
        from django.conf import settings
        from core.services.task_run import TRACKED_TASKS
        from core.tasks import refresh_verified_rates
        entry = settings.CELERY_BEAT_SCHEDULE['refresh-verified-rates']
        self.assertEqual(entry['task'], refresh_verified_rates.name)
        self.assertEqual(entry['schedule']._orig_day_of_month, '2')
        self.assertIn('refresh_verified_rates', TRACKED_TASKS)


class ManagementCommandTests(TestCase):
    @override_settings(OPENAI_API_KEY='')
    def test_without_a_key_it_says_skipped_and_logs_the_run(self):
        from core.models import TaskRunLog
        out = StringIO()
        with mock.patch.dict('os.environ', {'OPENAI_API_KEY': ''}):
            call_command('refresh_verified_rates', stdout=out)
        self.assertIn('Skipped: no_api_key', out.getvalue())
        self.assertTrue(TaskRunLog.objects.filter(task_name='refresh_verified_rates', success=True).exists())


class SeedVerificationMigrationTests(TestCase):
    def test_seeded_plazas_are_verified_from_their_source(self):
        from core.management.commands.seed_toll_data import SANRAL_TARIFF_SOURCE_URL
        # 0134 ran on the test DB after 0070 seeded the 2026 plazas.
        vaal = TollPlaza.objects.get(name='Vaal', route='N1')
        self.assertEqual((vaal.tariff_verified_at, vaal.tariff_effective_from, vaal.tariff_source_url),
                         (date(2026, 7, 1), date(2026, 3, 1), SANRAL_TARIFF_SOURCE_URL))

    def test_edited_or_unknown_plazas_are_left_unverified_and_rerun_is_harmless(self):
        mig = importlib.import_module('core.migrations.0134_seed_toll_tariff_verification')
        TollPlaza.objects.update(tariff_verified_at=None, tariff_effective_from=None, tariff_source_url='')
        TollPlaza.objects.filter(name='Vaal').update(tariff_class_5=Decimal('1.00'))
        extra = TollPlaza.objects.create(name='Test Plaza', route='N3', direction='x', location_km=Decimal('1'),
                                         tariff_class_2=1, tariff_class_3=1, tariff_class_4=1, tariff_class_5=1)
        mig.mark_seeded_tariffs_verified(django_apps, None)
        mig.mark_seeded_tariffs_verified(django_apps, None)
        self.assertIsNone(TollPlaza.objects.get(name='Vaal').tariff_verified_at)
        self.assertIsNone(TollPlaza.objects.get(id=extra.id).tariff_verified_at)
        self.assertEqual(TollPlaza.objects.get(name='Grasmere').tariff_verified_at, date(2026, 7, 1))


class AdminVerifiedRatesEndpointTests(TestCase):
    def setUp(self):
        VerifiedRate.objects.filter(proposed_by='migration_0158').delete()   # tests own their allowance rows
        self.plazas = _pin_plazas()
        self.superuser = User.objects.create_user(username='super-vr', password='x', is_superuser=True,
                                                  is_staff=True)
        self.user = User.objects.create_user(username='ops-vr', password='x')
        self.api = APIClient()
        self.api.force_authenticate(user=self.superuser)
        g = self.plazas['Grasmere']
        self.toll = VerifiedRate.objects.create(
            kind='toll_tariff', key=f'toll:{g.id}:class4', label='Grasmere (N1) Class 4', toll_plaza=g, sanral_class=4,
            value=Decimal('860.87'), published_value=Decimal('990.00'), previous_value=Decimal('826.09'),
            unit='per_passage', effective_from=PERIOD, source_url=TOLL_URL, source_name='SANRAL 2026 tariffs',
            verified_at=TODAY, proposed_by='refresh_verified_rates')
        self.allowance = VerifiedRate.objects.create(
            kind='driver_allowance', key='nbcrfli', label='NBCRFLI driver allowance', value=Decimal('243.63'),
            published_value=Decimal('243.63'), unit='per_night', effective_from=PERIOD, source_url=DRIVER_URL,
            source_name='NBCRFLI', verified_at=TODAY, proposed_by='refresh_verified_rates')

    def test_everything_is_superuser_only(self):
        other = APIClient()
        other.force_authenticate(user=self.user)
        anon = APIClient()
        for client, code in ((other, 403), (anon, (401, 403))):
            for method, url in (('get', '/api/v1/admin/verified-rates/'),
                                ('post', '/api/v1/admin/verified-rates/'),
                                ('post', f'/api/v1/admin/verified-rates/{self.toll.id}/approve/'),
                                ('post', f'/api/v1/admin/verified-rates/{self.toll.id}/reject/'),
                                ('post', '/api/v1/admin/verified-rates/refresh/')):
                status_code = getattr(client, method)(url, {}, format='json').status_code
                self.assertIn(status_code, code if isinstance(code, tuple) else (code,), (method, url))
        self.assertEqual(VerifiedRate.objects.filter(status='pending').count(), 2)

    def test_list_pending_with_current_and_proposed_values(self):
        data = self.api.get('/api/v1/admin/verified-rates/').json()
        self.assertEqual(data['count'], 2)
        toll = next(r for r in data['results'] if r['kind'] == 'toll_tariff')
        self.assertEqual((toll['label'], toll['current_value'], toll['proposed_value'], toll['published_value']),
                         ('Grasmere (N1) Class 4', 826.09, 860.87, 990.0))
        self.assertEqual((toll['source_url'], toll['source_name'], toll['verified_at'], toll['vat_basis']),
                         (TOLL_URL, 'SANRAL 2026 tariffs', TODAY.isoformat(), 'excl_vat'))
        self.assertIsNotNone(toll['found_at'])
        allowance = next(r for r in data['results'] if r['kind'] == 'driver_allowance')
        self.assertIsNone(allowance['current_value'])
        self.assertEqual(data['toll_table']['active_plazas'], 2)
        self.assertEqual(self.api.get('/api/v1/admin/verified-rates/?status=approved').json()['count'], 0)
        self.assertEqual(self.api.get('/api/v1/admin/verified-rates/?status=bogus').status_code, 400)
        self.assertEqual(self.api.get('/api/v1/admin/verified-rates/?status=all&kind=toll_tariff').json()['count'], 1)

    def test_approve_toll_writes_the_tariff_onto_the_plaza(self):
        resp = self.api.post(f'/api/v1/admin/verified-rates/{self.toll.id}/approve/', {'note': 'checked'},
                             format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['rate']['status'], 'approved')
        self.assertEqual(resp.json()['rate']['approved_by'], 'super-vr')
        g = TollPlaza.objects.get(id=self.plazas['Grasmere'].id)
        self.assertEqual((g.tariff_class_5, g.tariff_effective_from, g.tariff_verified_at, g.tariff_source_url,
                          g.tariff_year), (Decimal('990.00'), PERIOD, TODAY, TOLL_URL, PERIOD.year))
        self.assertEqual(g.tariff_class_4, Decimal('236.00'))  # other classes untouched
        # The approved row stays as history; it can't be approved twice.
        self.assertEqual(self.api.post(f'/api/v1/admin/verified-rates/{self.toll.id}/approve/').status_code, 400)
        self.assertEqual(self.api.get('/api/v1/admin/verified-rates/?status=approved').json()['count'], 1)

    def test_approve_supersedes_other_pending_proposals_for_the_same_figure(self):
        other = VerifiedRate.objects.create(
            kind='toll_tariff', key=self.toll.key, toll_plaza=self.toll.toll_plaza, sanral_class=4,
            value=Decimal('1.00'), published_value=Decimal('1.15'), unit='per_passage', effective_from=PERIOD)
        self.api.post(f'/api/v1/admin/verified-rates/{self.toll.id}/approve/')
        other.refresh_from_db()
        self.assertEqual(other.status, 'superseded')

    def test_future_toll_tariff_cannot_be_applied_early(self):
        resp = self.api.post(f'/api/v1/admin/verified-rates/{self.toll.id}/approve/',
                             {'effective_from': (TODAY + timedelta(days=5)).isoformat()}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(TollPlaza.objects.get(id=self.plazas['Grasmere'].id).tariff_class_5, Decimal('950.00'))
        self.assertEqual(self.api.post(f'/api/v1/admin/verified-rates/{self.toll.id}/approve/',
                                       {'effective_from': 'not-a-date'}, format='json').status_code, 400)

    def test_approved_allowance_is_what_the_price_check_uses_and_history_is_kept(self):
        from core.services.verified_rates import current_allowance
        old = _approved_allowance('200.00', effective_from=date(Y - 1, 3, 1))
        self.assertEqual(current_allowance(TODAY)['rate_per_night'], 200.0)
        resp = self.api.post(f'/api/v1/admin/verified-rates/{self.allowance.id}/approve/',
                             {'effective_from': PERIOD.isoformat()}, format='json')
        self.assertEqual(resp.status_code, 200)
        now = current_allowance(TODAY)
        self.assertEqual((now['rate_per_night'], now['source_url'], now['id']), (243.63, DRIVER_URL, self.allowance.id))
        old.refresh_from_db()
        self.assertEqual(old.status, 'approved')  # history
        self.assertEqual(self.api.get('/api/v1/admin/verified-rates/?status=approved').json()['count'], 2)

    def test_reject(self):
        resp = self.api.post(f'/api/v1/admin/verified-rates/{self.toll.id}/reject/', {'note': 'wrong plaza'},
                             format='json')
        self.assertEqual(resp.status_code, 200)
        self.toll.refresh_from_db()
        self.assertEqual((self.toll.status, self.toll.review_note, self.toll.rejected_by_id),
                         ('rejected', 'wrong plaza', self.superuser.id))
        self.assertEqual(TollPlaza.objects.get(id=self.plazas['Grasmere'].id).tariff_class_5, Decimal('950.00'))
        self.assertEqual(self.api.post(f'/api/v1/admin/verified-rates/{self.toll.id}/approve/').status_code, 400)
        self.assertEqual(self.api.post('/api/v1/admin/verified-rates/999999/reject/').status_code, 404)

    def test_manual_allowance_proposal_is_pending_until_approved(self):
        payload = {'allowance_type': 'nbcrfli', 'value': '250.00', 'effective_from': PERIOD.isoformat(),
                   'source_url': 'https://nbcrfli.test/agreement.pdf', 'source_name': 'NBCRFLI main agreement'}
        resp = self.api.post('/api/v1/admin/verified-rates/', payload, format='json')
        self.assertEqual(resp.status_code, 201)
        row = VerifiedRate.objects.get(id=resp.json()['rate']['id'])
        self.assertEqual((row.status, row.proposed_by, row.value), ('pending', 'admin:super-vr', Decimal('250.00')))
        self.allowance.refresh_from_db()
        self.assertEqual(self.allowance.status, 'superseded')
        for bad in ({'allowance_type': 'other'}, {'value': '-1'}, {'effective_from': 'x'}, {'source_url': 'ftp://x'}):
            self.assertEqual(self.api.post('/api/v1/admin/verified-rates/', dict(payload, **bad),
                                           format='json').status_code, 400, bad)

    @TEST_KEY
    def test_refresh_endpoint_queues_the_job(self):
        with mock.patch('core.tasks.refresh_verified_rates.delay', return_value=types.SimpleNamespace(id='t-1')) as d:
            resp = self.api.post('/api/v1/admin/verified-rates/refresh/', {'kinds': ['driver_allowance']},
                                 format='json')
        self.assertEqual((resp.status_code, resp.json()), (202, {'queued': True, 'task_id': 't-1'}))
        self.assertEqual(d.call_args.kwargs['kinds'], ['driver_allowance'])
        self.assertEqual(d.call_args.kwargs['triggered_by_id'], self.superuser.id)

    @override_settings(OPENAI_API_KEY='')
    def test_refresh_endpoint_without_a_key_says_so(self):
        with mock.patch.dict('os.environ', {'OPENAI_API_KEY': ''}), \
                mock.patch('core.tasks.refresh_verified_rates.delay') as d:
            resp = self.api.post('/api/v1/admin/verified-rates/refresh/')
        self.assertEqual((resp.status_code, resp.json()['reason']), (503, 'no_api_key'))
        self.assertFalse(d.called)
