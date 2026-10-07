"""Tests for T1.1 — Fuel Price service and model."""

import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests
from django.core.cache import cache
from django.test import TestCase

from core.models import FuelPrice
from core.services.fuel_price import (
    _check_price_alert,
    _fetch_from_dmre,
    _fetch_from_sapia,
    _to_decimal,
    fetch_fuel_prices,
)


class ToDecimalTests(TestCase):
    def test_valid_string(self):
        self.assertEqual(_to_decimal('21.7400'), Decimal('21.7400'))

    def test_rounds_to_four_places(self):
        result = _to_decimal('21.74')
        self.assertEqual(result, Decimal('21.7400'))

    def test_invalid_string_raises(self):
        with self.assertRaises(ValueError):
            _to_decimal('not-a-number')


class FetchFuelPricesTests(TestCase):
    """fetch_fuel_prices(): hermetic (recorded FIASA page / offline), clock at
    2026-09-28 10:00 SAST. Only FIASA columns are stored, under their
    effective date; nothing is ever taken from a fallback table."""

    NOW = datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo('Africa/Johannesburg'))
    FIASA_HTML = (Path(__file__).parent / 'fixtures' / 'fiasa_2026-09-28.html').read_text(encoding='utf-8')

    def setUp(self):
        cache.clear()
        resp = MagicMock(text=self.FIASA_HTML)
        self.http = patch('core.services.fuel_price.requests.get', return_value=resp)
        self.http_mock = self.http.start()
        self.addCleanup(self.http.stop)
        clock = patch('django.utils.timezone.now', return_value=self.NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def _go_offline(self):
        self.http_mock.return_value = None
        self.http_mock.side_effect = requests.ConnectionError('offline (test)')

    def test_old_month_not_on_fiasa_stores_nothing(self):
        self.assertIsNone(fetch_fuel_prices(target_date=date(2024, 3, 1)))
        self.assertFalse(FuelPrice.objects.exists())

    def test_month_on_fiasa_is_stored_once_under_its_effective_date(self):
        fp1 = fetch_fuel_prices(target_date=date(2026, 7, 1))
        fp2 = fetch_fuel_prices(target_date=date(2026, 7, 1), force_update=True)
        self.assertEqual(fp1.pk, fp2.pk)
        self.assertEqual(fp1.source, 'FIASA')
        self.assertEqual(FuelPrice.objects.count(), 1)

    def test_defaults_to_the_official_price_in_force(self):
        fp = fetch_fuel_prices()
        self.assertEqual(fp.date, date(2026, 9, 2))
        self.assertEqual(fp.source, 'FIASA')
        FuelPrice.objects.all().delete()
        self._go_offline()
        self.assertIsNone(fetch_fuel_prices())
        self.assertFalse(FuelPrice.objects.exists())

    def test_fiasa_is_used_for_the_current_month(self):
        fp = fetch_fuel_prices(target_date=date(2026, 9, 1))
        self.assertEqual(fp.source, 'FIASA')
        self.assertEqual(fp.diesel_inland, Decimal('29.5551'))   # Diesel 0.005% Gauteng
        self.assertEqual(fp.diesel_coastal, Decimal('28.6831'))  # Diesel 0.005% Coastal
        self.assertEqual(fp.diesel_grade, '50ppm')


class PriceAlertTests(TestCase):
    """Tests for _check_price_alert — 5% MoM change detection."""

    def _make_fuel_price(self, target_date, diesel_inland, diesel_coastal):
        return FuelPrice.objects.create(
            date=target_date,
            diesel_inland=Decimal(diesel_inland),
            diesel_coastal=Decimal(diesel_coastal),
            petrol_95=Decimal('22.00'),
            petrol_93=Decimal('21.00'),
        )

    def test_no_alert_when_no_prior_record(self):
        new_data = {
            'diesel_inland': Decimal('21.00'),
            'diesel_coastal': Decimal('20.50'),
        }
        with self.assertLogs('core.services.fuel_price', level='WARNING') as cm:
            # Inject a benign warning so assertLogs doesn't fail on empty
            logging.getLogger('core.services.fuel_price').warning('sentinel')
            _check_price_alert(date(2024, 1, 1), new_data)

        self.assertEqual(len(cm.output), 1)  # only sentinel
        self.assertIn('sentinel', cm.output[0])

    def test_no_alert_when_change_within_5_percent(self):
        prior_date = date(2024, 1, 1)
        self._make_fuel_price(prior_date, '21.00', '20.50')

        new_data = {
            'diesel_inland': Decimal('21.80'),   # +3.8% — under threshold
            'diesel_coastal': Decimal('21.00'),
        }
        with self.assertLogs('core.services.fuel_price', level='WARNING') as cm:
            logging.getLogger('core.services.fuel_price').warning('sentinel')
            _check_price_alert(date(2024, 2, 1), new_data)

        self.assertEqual(len(cm.output), 1)

    def test_alert_logged_when_change_exceeds_5_percent(self):
        prior_date = date(2024, 1, 1)
        self._make_fuel_price(prior_date, '20.00', '19.50')

        # +10% — exceeds 5% threshold
        new_data = {
            'diesel_inland': Decimal('22.00'),
            'diesel_coastal': Decimal('21.45'),
        }
        with self.assertLogs('core.services.fuel_price', level='WARNING') as cm:
            _check_price_alert(date(2024, 2, 1), new_data)

        warning_msgs = [m for m in cm.output if 'FUEL PRICE ALERT' in m]
        self.assertGreaterEqual(len(warning_msgs), 1)
        self.assertIn('diesel_inland', warning_msgs[0])

    def test_alert_on_large_decrease(self):
        prior_date = date(2024, 3, 1)
        self._make_fuel_price(prior_date, '22.00', '21.50')

        # -10% decrease
        new_data = {
            'diesel_inland': Decimal('19.80'),
            'diesel_coastal': Decimal('19.35'),
        }
        with self.assertLogs('core.services.fuel_price', level='WARNING') as cm:
            _check_price_alert(date(2024, 4, 1), new_data)

        warning_msgs = [m for m in cm.output if 'FUEL PRICE ALERT' in m]
        self.assertGreaterEqual(len(warning_msgs), 1)

    def test_alert_checks_both_diesel_fields(self):
        prior_date = date(2024, 5, 1)
        self._make_fuel_price(prior_date, '20.00', '10.00')

        # inland within threshold, coastal exceeds
        new_data = {
            'diesel_inland': Decimal('20.50'),   # +2.5%
            'diesel_coastal': Decimal('11.20'),  # +12%
        }
        with self.assertLogs('core.services.fuel_price', level='WARNING') as cm:
            _check_price_alert(date(2024, 6, 1), new_data)

        coastal_alerts = [m for m in cm.output if 'diesel_coastal' in m]
        self.assertEqual(len(coastal_alerts), 1)
