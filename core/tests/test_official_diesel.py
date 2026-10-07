"""Official diesel price in force, freshness and company resolution
(QUOTE-RULES.md §1-§2). Hermetic: FIASA is served from the recorded page."""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import Company, FuelPrice
from core.services import fuel_price as fps
from core.tests.test_fuel_pipeline import OCTOBER_PREPUBLISHED, SEP_50PPM_GAUTENG, at, offline, serve

SAST = ZoneInfo('Africa/Johannesburg')


def sast(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SAST)


def row(day, inland, coastal, source='FIASA', eff=None, grade='50ppm', **extra):
    return FuelPrice.objects.create(date=day, diesel_inland=Decimal(str(inland)), diesel_coastal=Decimal(str(coastal)),
                                    source=source, diesel_grade=grade if source == 'FIASA' else None,
                                    effective_from=eff, **extra)


class PeriodTests(TestCase):
    def test_period_starts_first_wednesday_0001_sast(self):
        self.assertEqual(fps.period_start(sast(2026, 10, 7, 0, 1)), sast(2026, 10, 7, 0, 1))
        self.assertEqual(fps.period_start(sast(2026, 10, 7, 0, 0)), sast(2026, 9, 2, 0, 1))
        self.assertEqual(fps.period_start(sast(2026, 10, 1, 12)), sast(2026, 9, 2, 0, 1))
        self.assertEqual(fps.period_start(sast(2026, 10, 31, 23)), sast(2026, 10, 7, 0, 1))
        self.assertEqual(fps.previous_period_start(sast(2026, 10, 7, 0, 1)), sast(2026, 9, 2, 0, 1))

    def test_period_uses_sast_not_server_tz(self):
        # 6 Oct 22:01 UTC == 7 Oct 00:01 SAST
        from datetime import timezone as dt_tz
        self.assertEqual(fps.period_start(datetime(2026, 10, 6, 22, 1, tzinfo=dt_tz.utc)), sast(2026, 10, 7, 0, 1))


class InForceTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_history_kept_and_price_in_force_on_any_date(self):
        row(date(2026, 9, 2), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))
        row(date(2026, 10, 7), 32.7989, 31.9269, eff=sast(2026, 10, 7, 0, 1))
        self.assertEqual(fps.price_in_force('INLAND', sast(2026, 9, 20))['price'], 29.5551)
        self.assertEqual(fps.price_in_force('COASTAL', sast(2026, 10, 7, 9))['price'], 31.9269)
        self.assertEqual(fps.price_in_force('INLAND', sast(2026, 10, 7, 0, 0))['price'], 29.5551)

    def test_fallback_rows_are_never_used(self):
        row(date(2026, 10, 1), 24.5, 23.88, source='FALLBACK_LATEST')
        self.assertIsNone(fps.price_in_force('INLAND', sast(2026, 10, 7, 9)))
        row(date(2026, 9, 2), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))
        self.assertEqual(fps.price_in_force('INLAND', sast(2026, 10, 7, 9))['price'], 29.5551)

    def test_newer_fiasa_supersedes_older_manual(self):
        row(date(2026, 9, 15), 30.10, 29.20, source='MANUAL', eff=sast(2026, 9, 15, 8))
        self.assertEqual(fps.price_in_force('INLAND', sast(2026, 10, 1))['source'], 'MANUAL')
        row(date(2026, 10, 7), 32.7989, 31.9269, eff=sast(2026, 10, 7, 0, 1))
        rec = fps.price_in_force('INLAND', sast(2026, 10, 7, 9))
        self.assertEqual((rec['source'], rec['price']), ('FIASA', 32.7989))

    def test_500ppm_legacy_fiasa_rows_not_used_for_pricing(self):
        row(date(2026, 8, 1), 28.1, 27.2, grade=None, eff=sast(2026, 8, 5, 0, 1))
        self.assertIsNone(fps.price_in_force('INLAND', sast(2026, 8, 20)))
        self.assertEqual(fps.price_in_force('INLAND', sast(2026, 8, 20), strict_grade=False)['price'], 28.1)


class RefreshTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_refresh_on_first_wednesday_change_keeps_history(self):
        with serve(), at(sast(2026, 9, 28, 10)):
            fps.refresh_official()
        sep = FuelPrice.objects.get(date=date(2026, 9, 2))
        with serve(OCTOBER_PREPUBLISHED), at(sast(2026, 10, 7, 0, 5)):
            rec = fps.refresh_official()
        self.assertEqual(rec.date, date(2026, 10, 7))
        self.assertEqual(rec.diesel_inland, Decimal('33.3000'))
        sep.refresh_from_db()
        self.assertEqual(sep.diesel_inland, SEP_50PPM_GAUTENG)   # never overwritten

    def test_no_network_when_current(self):
        row(date(2026, 10, 7), 32.7989, 31.9269, eff=sast(2026, 10, 7, 0, 1),
            petrol_95=Decimal('30.25'), petrol_95_coastal=Decimal('29.38'))
        with serve() as get, at(sast(2026, 10, 9)):
            fps.refresh_official()
        get.assert_not_called()

    def test_manual_row_does_not_lock_the_next_period(self):
        row(date(2026, 9, 1), 30.10, 29.20, source='MANUAL', eff=sast(2026, 9, 1, 9))
        with serve(OCTOBER_PREPUBLISHED), at(sast(2026, 10, 8, 6)):
            rec = fps.refresh_official()
        self.assertEqual(rec.source, 'FIASA')
        self.assertEqual(rec.date, date(2026, 10, 7))
        self.assertTrue(FuelPrice.objects.filter(source='MANUAL').exists())

    def test_failed_refresh_flags_and_keeps(self):
        old = row(date(2026, 9, 2), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))
        with offline(), at(sast(2026, 10, 8, 6)):
            rec = fps.refresh_official()
        self.assertEqual(rec.pk, old.pk)
        rec.refresh_from_db()
        self.assertIsNotNone(rec.fetch_failed_at)
        self.assertFalse(FuelPrice.objects.filter(source__startswith='FALLBACK').exists())

    @override_settings(FUEL_PRICE_READ_REFRESH=True)
    def test_read_path_queues_one_refresh_and_never_calls_the_network(self):
        row(date(2026, 9, 2), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))
        with offline() as get, at(sast(2026, 10, 8, 6)), \
                patch('core.services.fuel_price.enqueue_refresh', return_value=True) as q:
            first = fps.resolve_official('INLAND')
            second = fps.resolve_official('INLAND')
        get.assert_not_called()                                  # no network in the request path
        self.assertEqual(q.call_count, 1)                        # deduplicated by the cache lock
        self.assertTrue(first['refresh_attempted'])
        self.assertFalse(second['refresh_attempted'])
        self.assertTrue(first['stale'])
        self.assertEqual(first['price'], float(SEP_50PPM_GAUTENG))

    def test_enqueue_never_raises_when_the_broker_is_down(self):
        with patch('core.tasks.refresh_fuel_price.apply_async', side_effect=ConnectionError('no broker')):
            self.assertFalse(fps.enqueue_refresh())

    def test_task_retries_when_fiasa_still_shows_last_period(self):
        from unittest.mock import MagicMock
        from core import tasks
        stale = MagicMock(source='FIASA', effective_from=sast(2026, 9, 2, 0, 1), fetch_failed_at=None,
                          diesel_inland=Decimal('29.5551'), date=date(2026, 9, 2))
        for fp in (None, stale):
            sig = MagicMock()
            t = tasks.refresh_fuel_price
            with patch('core.services.fuel_price.fetch_fuel_prices', return_value=fp), at(sast(2026, 10, 8, 6)), \
                    patch('celery.app.task.Task.signature_from_request', return_value=sig):
                t.push_request(id='x', retries=0, is_eager=False, called_directly=False)
                try:
                    t.run()
                except Exception:
                    pass
                finally:
                    t.pop_request()
            self.assertEqual(sig.apply_async.call_count, 1, fp)

    def test_two_periods_old_is_not_usable(self):
        row(date(2026, 8, 5), 28.0, 27.1, eff=sast(2026, 8, 5, 0, 1))
        with at(sast(2026, 10, 8, 6)):
            out = fps.resolve_official('INLAND', refresh=False)
        self.assertIsNone(out['price'])


class CompanyDieselTests(TestCase):
    def setUp(self):
        cache.clear()
        row(date(2026, 10, 7), 32.7989, 31.9269, eff=sast(2026, 10, 7, 0, 1))

    def test_live_company_gets_zone_official(self):
        c = Company.objects.create(company_name='Coastal Co', fuel_zone='COASTAL')
        with at(sast(2026, 10, 7, 9)):
            out = fps.resolve_company_diesel(c)
        self.assertEqual((out['source'], out['price'], out['zone']), ('official', 31.9269, 'COASTAL'))
        self.assertEqual(out['warnings'], [])

    def test_own_company_warns_when_off(self):
        c = Company.objects.create(company_name='Own Co', fuel_price_mode='OWN', fuel_price_own=Decimal('30.00'),
                                   fuel_price_own_set_at=sast(2026, 9, 10))
        with at(sast(2026, 10, 7, 9)):
            out = fps.resolve_company_diesel(c)
        self.assertEqual((out['source'], out['price']), ('own', 30.0))
        self.assertEqual([w['code'] for w in out['warnings']], ['diesel_own_off', 'diesel_own_old'])

    def test_missing_is_null_never_a_default(self):
        FuelPrice.objects.all().delete()
        c = Company.objects.create(company_name='Live Co')
        with at(sast(2026, 10, 7, 9)):
            out = fps.resolve_company_diesel(c)
        self.assertIsNone(out['price'])
        self.assertEqual(out['source'], 'missing')
        self.assertEqual(out['warnings'][0]['code'], 'diesel_missing')


class CurrentEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(company_name='Fuel Co', fuel_zone='INLAND',
                                              fuel_price_mode='OWN', fuel_price_own=Decimal('31.00'),
                                              fuel_price_own_set_at=sast(2026, 9, 3))
        user = get_user_model().objects.create_user(username='fu', email='fu@test.test', password='x')
        user.company = self.company
        user.save()
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_fallback_only_returns_null_prices(self):
        row(date(2026, 10, 1), 24.5, 23.88, source='FALLBACK_LATEST')
        with at(sast(2026, 10, 7, 9)):
            body = self.client.get('/api/v1/fuel-prices/current/').json()
        self.assertIsNone(body['inland_price'])
        self.assertIsNone(body['zone_price'])
        self.assertTrue(body['is_stale'])
        self.assertEqual(body['company_price']['source'], 'own')

    def test_force_is_staff_only_and_ignored_for_others(self):
        row(date(2026, 10, 7), 32.7989, 31.9269, eff=sast(2026, 10, 7, 0, 1))
        with patch('core.services.fuel_price.refresh_official') as r, at(sast(2026, 10, 7, 9)):
            resp = self.client.get('/api/v1/fuel-prices/current/?force=true')
        self.assertEqual(resp.status_code, 200)
        r.assert_not_called()

    def test_zone_price_effective_from_period_and_company_price(self):
        row(date(2026, 9, 2), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))
        row(date(2026, 10, 7), 32.7989, 31.9269, eff=sast(2026, 10, 7, 0, 1))
        with at(sast(2026, 10, 7, 9)):
            body = self.client.get('/api/v1/fuel-prices/current/').json()
        self.assertEqual(body['zone_price'], 32.7989)
        self.assertEqual(body['effective_from'], sast(2026, 10, 7, 0, 1).isoformat())
        self.assertEqual(body['period_start'], sast(2026, 10, 7, 0, 1).isoformat())
        self.assertFalse(body['stale'])
        cp = body['company_price']
        self.assertEqual((cp['mode'], cp['source'], cp['price']), ('OWN', 'own', 31.0))
        self.assertEqual(cp['official']['price'], 32.7989)
        self.assertEqual([w['code'] for w in cp['warnings']], ['diesel_own_off', 'diesel_own_old'])

    def test_previous_period_flagged_stale(self):
        row(date(2026, 9, 2), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))
        with at(sast(2026, 10, 8, 9)):
            body = self.client.get('/api/v1/fuel-prices/current/').json()
        self.assertTrue(body['stale'])
        self.assertTrue(body['is_stale'])
        self.assertTrue(body['company_price']['official']['stale'])


class RepairAndCommandTests(TestCase):
    def test_repair_rekeys_mislabelled_month_rows(self):
        from io import StringIO
        from django.core.management import call_command
        row(date(2026, 10, 1), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))      # Sep price filed under Oct
        row(date(2026, 9, 1), 29.5551, 28.6831, eff=sast(2026, 9, 2, 0, 1))       # duplicate under Sep
        row(date(2026, 8, 1), 28.0, 27.1, grade=None)                              # no effective date
        out = StringIO()
        call_command('repair_fuel_history', stdout=out)
        self.assertIn('Dry run', out.getvalue())
        self.assertTrue(FuelPrice.objects.filter(date=date(2026, 10, 1)).exists())   # dry run changes nothing
        call_command('repair_fuel_history', '--apply', stdout=StringIO())
        self.assertEqual(FuelPrice.objects.filter(date=date(2026, 9, 2)).count(), 1)
        self.assertFalse(FuelPrice.objects.filter(date__in=[date(2026, 10, 1), date(2026, 9, 1)]).exists())
        self.assertTrue(FuelPrice.objects.filter(date=date(2026, 8, 1)).exists())

    def test_history_lookup_needs_an_effective_date(self):
        row(date(2026, 8, 1), 28.0, 27.1, grade=None)
        self.assertIsNone(fps.price_in_force('INLAND', sast(2026, 8, 20), strict_grade=False))

    def test_fetch_command_keys_by_effective_date(self):
        from io import StringIO
        from django.core.management import call_command
        with serve(), at(sast(2026, 9, 28, 10)):
            call_command('fetch_fuel_prices', '--date', '2026-06-01', stdout=StringIO())
        self.assertTrue(FuelPrice.objects.filter(date=date(2026, 6, 3), source='FIASA').exists())
        self.assertFalse(FuelPrice.objects.filter(date=date(2026, 6, 1)).exists())
