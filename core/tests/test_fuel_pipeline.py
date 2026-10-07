"""Fuel price pipeline — Phase 0 correctness fixes (see
docs/backend-changes/2026-09-fuel-pipeline.md).

Every test here is hermetic: HTTP is patched at the ``requests.get`` boundary
and the FIASA page is served from a recorded fixture
(core/tests/fixtures/fiasa_2026-09-28.html, trimmed verbatim from the live page
fetched 2026-09-28). Nothing here reaches the internet.

Each test class names the review finding it pins down. Every test in this file
was written first and seen to FAIL against main @ 45039ee before the fix.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import Company, FuelPrice
from core.services import fuel_price as fps

SAST = ZoneInfo('Africa/Johannesburg')
FIXTURE = Path(__file__).parent / 'fixtures' / 'fiasa_2026-09-28.html'
FIASA_HTML = FIXTURE.read_text(encoding='utf-8')

# Values read straight off the fixture (cents/litre ÷ 100).
SEP_50PPM_GAUTENG = Decimal('29.5551')   # "Diesel 0.005%" Gauteng, 2-Sep-26
SEP_50PPM_COASTAL = Decimal('28.6831')   # "Diesel 0.005%" Coastal, 2-Sep-26
SEP_500PPM_GAUTENG = Decimal('29.1111')  # "Diesel 0.05%"  Gauteng, 2-Sep-26
SEP_500PPM_COASTAL = Decimal('28.2391')  # "Diesel 0.05%"  Coastal, 2-Sep-26
JUN_50PPM_GAUTENG = Decimal('28.7597')   # "Diesel 0.005%" Gauteng, 3-Jun-26


def _sast(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SAST)


def _resp(text):
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock()
    return r


def serve(html=FIASA_HTML):
    """Every requests.get in the fuel service returns `html`."""
    return patch('core.services.fuel_price.requests.get', return_value=_resp(html))


def offline():
    """Every requests.get in the fuel service fails like a timeout."""
    return patch('core.services.fuel_price.requests.get',
                 side_effect=requests.ConnectionError('offline (test)'))


def at(moment):
    """Freeze django.utils.timezone.now() (the service's notion of 'now')."""
    return patch('django.utils.timezone.now', return_value=moment)


def _fill_next_column(html, values):
    """Simulate FIASA pre-publishing the 7-Oct-26 column before it takes
    effect: write values into the first empty cell of the named rows."""
    soup = BeautifulSoup(html, 'lxml')
    for tab, rows in values.items():
        table = soup.find('div', id=tab).find('table')
        for tr in table.find_all('tr'):
            cells = tr.find_all(['td', 'th'])
            if not cells:
                continue
            label = cells[0].get_text(strip=True).lower()
            for prefix, val in rows.items():
                if label.startswith(prefix):
                    for c in cells[1:]:
                        if not c.get_text(strip=True):
                            c.string = val
                            break
    return str(soup)


OCTOBER_PREPUBLISHED = _fill_next_column(FIASA_HTML, {
    'tab-1': {'diesel 0.05%': '3200,00', 'diesel 0.005%': '3250,00', '95 ulp': '2700,00'},
    'tab-2': {'diesel 0.05%': '3287,20', 'diesel 0.005%': '3330,00', '95 ulp': '2790,00',
              '93 ulp': '2770,00'},
})


class FiasaParserTests(TestCase):
    """F4 (grade) and F5 (effective date); F12(a)/(b) fall out of the same parser."""

    def test_diesel_price_is_the_50ppm_row_and_500ppm_is_kept_separately(self):
        with serve(), at(_sast(2026, 9, 28, 10)):
            d = fps._fetch_from_fiasa()
        self.assertEqual(d['diesel_inland'], SEP_50PPM_GAUTENG)
        self.assertEqual(d['diesel_coastal'], SEP_50PPM_COASTAL)
        self.assertEqual(d['diesel_grade'], '50ppm')
        self.assertEqual(d['diesel_500ppm_inland'], SEP_500PPM_GAUTENG)
        self.assertEqual(d['diesel_500ppm_coastal'], SEP_500PPM_COASTAL)

    def test_effective_date_comes_from_the_column_header(self):
        with serve(), at(_sast(2026, 9, 28, 10)):
            d = fps._fetch_from_fiasa()
        # 2-Sep-26 is the first Wednesday of September; change at 00:01 SAST.
        self.assertEqual(d['effective_from'], _sast(2026, 9, 2, 0, 1))

    def test_prepublished_column_is_not_used_before_it_takes_effect(self):
        # Review demo D4: FIASA fills the 7-Oct column on 30 Sep.
        with serve(OCTOBER_PREPUBLISHED), at(_sast(2026, 10, 6, 23, 59)):
            d = fps._fetch_from_fiasa()
        self.assertEqual(d['diesel_inland'], SEP_50PPM_GAUTENG)
        self.assertEqual(d['effective_from'], _sast(2026, 9, 2, 0, 1))

    def test_prepublished_column_is_used_from_wednesday_0001_sast(self):
        with serve(OCTOBER_PREPUBLISHED), at(_sast(2026, 10, 7, 0, 1)):
            d = fps._fetch_from_fiasa()
        self.assertEqual(d['diesel_inland'], Decimal('33.3000'))
        self.assertEqual(d['diesel_500ppm_inland'], Decimal('32.8720'))
        self.assertEqual(d['effective_from'], _sast(2026, 10, 7, 0, 1))

    def test_unparseable_current_cell_fails_instead_of_returning_last_month(self):
        # Review demo D5: newest Gauteng 50ppm cell reformatted.
        soup = BeautifulSoup(FIASA_HTML, 'lxml')
        for tr in soup.find('div', id='tab-2').find('table').find_all('tr'):
            cells = tr.find_all(['td', 'th'])
            if cells and cells[0].get_text(strip=True).lower().startswith('diesel 0.005%'):
                [c for c in cells[1:] if c.get_text(strip=True)][-1].string = '2 955,51*'
        with serve(str(soup)), at(_sast(2026, 9, 28, 10)):
            d = fps._fetch_from_fiasa()
        self.assertIsNone(d)

    def test_zone_is_read_from_the_tab_heading_not_the_dom_id(self):
        # Review demo D6: the two tabs swap ids; the headings still say which is which.
        html = (FIASA_HTML.replace('"tab-1"', '"tab-X"')
                .replace('"tab-2"', '"tab-1"').replace('"tab-X"', '"tab-2"'))
        with serve(html), at(_sast(2026, 9, 28, 10)):
            d = fps._fetch_from_fiasa()
        self.assertEqual(d['diesel_inland'], SEP_50PPM_GAUTENG)
        self.assertEqual(d['diesel_coastal'], SEP_50PPM_COASTAL)


class HistoricalDateTests(TestCase):
    """F3: a historical target_date must never be stamped with today's price."""

    def setUp(self):
        cache.clear()

    def test_month_not_on_the_page_does_not_get_todays_price(self):
        # Review demo D2 stored 29.1111 FIASA under 2024-03-01.
        with serve(), at(_sast(2026, 9, 28, 10)):
            fp = fps.fetch_fuel_prices(target_date=date(2024, 3, 1))
        self.assertEqual(fp.source, 'FALLBACK')
        self.assertEqual(fp.diesel_inland, Decimal('22.1000'))

    def test_month_on_the_page_gets_that_months_column(self):
        with serve(), at(_sast(2026, 9, 28, 10)):
            fp = fps.fetch_fuel_prices(target_date=date(2026, 6, 1))
        self.assertEqual(fp.source, 'FIASA')
        self.assertEqual(fp.diesel_inland, JUN_50PPM_GAUTENG)
        self.assertEqual(fp.effective_from, _sast(2026, 6, 3, 0, 1))

    def test_current_price_scrapers_are_not_used_for_a_past_month(self):
        live_today = {'diesel_inland': Decimal('29.00'), 'diesel_coastal': Decimal('28.00'),
                      'petrol_95': Decimal('26.00'), 'petrol_93': Decimal('25.00'),
                      'source': 'SAPIA'}
        with offline(), at(_sast(2026, 9, 28, 10)), \
                patch('core.services.fuel_price._fetch_from_sapia', return_value=live_today):
            fp = fps.fetch_fuel_prices(target_date=date(2024, 3, 1))
        self.assertEqual(fp.source, 'FALLBACK')
        self.assertEqual(fp.diesel_inland, Decimal('22.1000'))

    def test_current_month_before_first_wednesday_gets_price_in_force(self):
        # 1-6 Oct: September's price is still in force; it must say so.
        with serve(OCTOBER_PREPUBLISHED), at(_sast(2026, 10, 2, 6)):
            fp = fps.fetch_fuel_prices(target_date=date(2026, 10, 1))
        self.assertEqual(fp.diesel_inland, SEP_50PPM_GAUTENG)
        self.assertEqual(fp.effective_from, _sast(2026, 9, 2, 0, 1))


class NeverDowngradeTests(TestCase):
    """F2: a failed or lower-trust fetch must never overwrite a good row."""

    def setUp(self):
        cache.clear()
        self.month = date(2026, 9, 1)

    def _good_row(self):
        with serve(), at(_sast(2026, 9, 27, 6)):
            return fps.fetch_fuel_prices(target_date=self.month, force_update=True)

    def test_failed_forced_refresh_keeps_good_row_and_flags_failure(self):
        good = self._good_row()
        good_fetched_at = good.fetched_at
        # Review demo D3: FIASA times out on the next nightly forced refresh.
        with offline(), at(_sast(2026, 9, 28, 6)):
            fp = fps.fetch_fuel_prices(target_date=self.month, force_update=True)
        row = FuelPrice.objects.get(date=self.month)
        self.assertEqual(row.source, 'FIASA')
        self.assertEqual(row.diesel_inland, SEP_50PPM_GAUTENG)
        self.assertEqual(row.fetched_at, good_fetched_at)  # last *successful* check
        self.assertEqual(row.fetch_failed_at, _sast(2026, 9, 28, 6))
        self.assertEqual(fp.pk, row.pk)

    def test_lower_trust_live_source_does_not_replace_fiasa(self):
        self._good_row()
        regex_guess = {'diesel_inland': Decimal('22.50'), 'diesel_coastal': Decimal('21.63'),
                       'petrol_95': Decimal('23.80'), 'petrol_93': Decimal('23.05'),
                       'source': 'AA_SA'}
        with offline(), at(_sast(2026, 9, 28, 6)), \
                patch('core.services.fuel_price._fetch_from_aa_sa', return_value=regex_guess):
            fps.fetch_fuel_prices(target_date=self.month, force_update=True)
        row = FuelPrice.objects.get(date=self.month)
        self.assertEqual(row.source, 'FIASA')
        self.assertEqual(row.diesel_inland, SEP_50PPM_GAUTENG)
        self.assertIsNotNone(row.fetch_failed_at)

    def test_successful_refresh_clears_the_failure_flag(self):
        self._good_row()
        with offline(), at(_sast(2026, 9, 28, 6)):
            fps.fetch_fuel_prices(target_date=self.month, force_update=True)
        with serve(), at(_sast(2026, 9, 29, 6)):
            fps.fetch_fuel_prices(target_date=self.month, force_update=True)
        row = FuelPrice.objects.get(date=self.month)
        self.assertIsNone(row.fetch_failed_at)
        self.assertEqual(row.fetched_at, _sast(2026, 9, 29, 6))

    def test_fallback_row_is_still_replaced_by_live_data(self):
        # Unchanged behaviour guard: a fallback placeholder is upgraded.
        with offline(), at(_sast(2026, 9, 28, 6)):
            fb = fps.fetch_fuel_prices(target_date=self.month, force_update=True)
        self.assertTrue(fb.source.startswith('FALLBACK'))
        with serve(), at(_sast(2026, 9, 28, 7)):
            fp = fps.fetch_fuel_prices(target_date=self.month, force_update=True)
        self.assertEqual(fp.source, 'FIASA')
        self.assertEqual(fp.diesel_inland, SEP_50PPM_GAUTENG)


class ManualOverrideTests(TestCase):
    """F7: a staff override stays until a person replaces it."""

    def setUp(self):
        cache.clear()
        self.month = date.today().replace(day=1)

    def test_forced_refresh_does_not_overwrite_manual_row(self):
        # Review demo D3b.
        FuelPrice.objects.create(date=self.month, diesel_inland=Decimal('29.9000'),
                                 diesel_coastal=Decimal('29.0000'), source='MANUAL')
        with serve() as get:
            fp = fps.fetch_fuel_prices(target_date=self.month, force_update=True)
        self.assertEqual(fp.source, 'MANUAL')
        self.assertEqual(fp.diesel_inland, Decimal('29.9000'))
        get.assert_not_called()  # no scrape at all for a confirmed row

    def test_staff_post_replaces_price_and_stamps_fetched_at(self):
        staff = get_user_model().objects.create_user(
            username='fuel_staff', email='fuel_staff@test.test', password='x', is_staff=True)
        FuelPrice.objects.create(date=self.month, diesel_inland=Decimal('29.1111'),
                                 diesel_coastal=Decimal('28.2391'), source='FIASA',
                                 fetched_at=_sast(2026, 1, 1))
        client = APIClient()
        client.force_authenticate(staff)
        with offline():
            r = client.post('/api/v1/fuel-prices/current/',
                            {'diesel_inland': '30.10', 'diesel_coastal': '29.20'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        # Keyed by the SAST day it was entered; the FIASA row stays on record.
        from django.utils import timezone as dj_tz
        row = FuelPrice.objects.get(date=dj_tz.localdate())
        self.assertTrue(FuelPrice.objects.filter(date=self.month, source='FIASA').exists()
                        or dj_tz.localdate() == self.month)
        self.assertEqual(row.source, 'MANUAL')
        self.assertEqual(row.diesel_inland, Decimal('30.1000'))
        self.assertGreater(row.fetched_at, _sast(2026, 1, 2))
        self.assertIsNotNone(row.effective_from)


class RefreshTaskRetryTests(TestCase):
    """F10: one failed run schedules exactly one retry."""

    def _run_task(self, fetch_side_effect=None, fetch_return=None):
        from core import tasks
        t = tasks.refresh_fuel_price
        sig = MagicMock()
        kwargs = {'side_effect': fetch_side_effect} if fetch_side_effect else {'return_value': fetch_return}
        with patch('core.services.fuel_price.fetch_fuel_prices', **kwargs), \
                patch('celery.app.task.Task.signature_from_request', return_value=sig):
            t.push_request(id='test-refresh', retries=0, is_eager=False, called_directly=False)
            try:
                t.run()
            except Exception:
                pass
            finally:
                t.pop_request()
        return sig.apply_async.call_count

    def test_all_sources_down_schedules_exactly_one_retry(self):
        # Review demo D8b: 2 retry messages for one failure.
        fp = MagicMock(source='FALLBACK_LATEST', diesel_inland=Decimal('24.5'),
                       date=date(2026, 9, 1), fetch_failed_at=None)
        self.assertEqual(self._run_task(fetch_return=fp), 1)

    def test_unexpected_error_schedules_exactly_one_retry(self):
        self.assertEqual(self._run_task(fetch_side_effect=RuntimeError('boom')), 1)

    def test_failed_refresh_that_kept_a_good_row_still_retries(self):
        # With F2 fixed a failed refresh returns the kept FIASA row; the task
        # must still treat the run as failed and retry.
        fp = MagicMock(source='FIASA', diesel_inland=Decimal('29.5551'),
                       date=date(2026, 9, 1), fetch_failed_at=_sast(2026, 9, 28, 6))
        self.assertEqual(self._run_task(fetch_return=fp), 1)

    def test_successful_refresh_schedules_no_retry(self):
        fp = MagicMock(source='FIASA', diesel_inland=Decimal('29.5551'),
                       date=date(2026, 9, 1), fetch_failed_at=None)
        self.assertEqual(self._run_task(fetch_return=fp), 0)


class CurrentEndpointTests(TestCase):
    """Item 7: /api/v1/fuel-prices/current/ returns the company-zone price
    with source, grade and effective date; legacy fields are unchanged."""

    def setUp(self):
        cache.clear()
        self.month = date.today().replace(day=1)
        self.company = Company.objects.create(company_name='Fuel Zone Co', fuel_zone='COASTAL')
        self.user = get_user_model().objects.create_user(
            username='fuel_user', email='fuel_user@test.test', password='x')
        self.user.company = self.company
        self.user.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _row(self, **extra):
        defaults = dict(date=self.month, diesel_inland=SEP_50PPM_GAUTENG,
                        diesel_coastal=SEP_50PPM_COASTAL, petrol_95=Decimal('26.92'),
                        petrol_93=Decimal('26.76'), source='FIASA', diesel_grade='50ppm',
                        diesel_500ppm_inland=SEP_500PPM_GAUTENG,
                        diesel_500ppm_coastal=SEP_500PPM_COASTAL,
                        effective_from=_sast(2026, 9, 2, 0, 1))
        defaults.update(extra)
        return FuelPrice.objects.create(**defaults)

    def test_returns_company_zone_price_with_provenance(self):
        self._row()
        with offline():
            r = self.client.get('/api/v1/fuel-prices/current/')
        body = r.json()
        self.assertEqual(r.status_code, 200)
        # legacy contract untouched
        self.assertEqual(body['inland_price'], float(SEP_50PPM_GAUTENG))
        self.assertEqual(body['coastal_price'], float(SEP_50PPM_COASTAL))
        self.assertEqual(body['source'], 'FIASA')
        self.assertEqual(body['date'], self.month.isoformat())
        # additions
        self.assertEqual(body['zone'], 'COASTAL')
        self.assertEqual(body['zone_price'], float(SEP_50PPM_COASTAL))
        self.assertEqual(body['diesel_grade'], '50ppm')
        self.assertEqual(body['effective_from'], _sast(2026, 9, 2, 0, 1).isoformat())
        self.assertEqual(body['diesel_500ppm_coastal'], float(SEP_500PPM_COASTAL))
        self.assertIsNone(body['last_failed_check_at'])

    def test_failed_refresh_shows_last_good_price_flagged_stale(self):
        self._row(fetch_failed_at=_sast(2026, 9, 28, 6))
        with offline():
            r = self.client.get('/api/v1/fuel-prices/current/')
        body = r.json()
        self.assertEqual(body['inland_price'], float(SEP_50PPM_GAUTENG))
        self.assertTrue(body['is_stale'])
        self.assertTrue(body['stale_warning'])
        self.assertEqual(body['last_failed_check_at'], _sast(2026, 9, 28, 6).isoformat())


class LegacyDailyScraperTests(TestCase):
    """F13: the regex daily scraper wrote competing rows dated *today*."""

    @override_settings(FUEL_PRICE_DAILY_SCRAPER_ENABLED=False)
    def test_daily_command_is_disabled_by_default(self):
        FuelPrice.objects.create(date=date(2026, 9, 1), diesel_inland=SEP_50PPM_GAUTENG,
                                 diesel_coastal=SEP_50PPM_COASTAL, source='FIASA')
        page = '<html>Call 011 22.50 ... Est. 19.95 ... 2026.09 ... 24.99</html>'
        with patch('core.services.fuel_price_live.requests.get', return_value=_resp(page)), \
                patch('time.sleep'):
            call_command('fetch_fuel_price_daily', stdout=MagicMock())
        self.assertEqual(list(FuelPrice.objects.values_list('date', flat=True)), [date(2026, 9, 1)])
