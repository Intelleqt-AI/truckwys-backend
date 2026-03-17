"""Tests for the True Margin Calculator service."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from core.models.fuel_price import FuelPrice
from core.services.margin_calculator import (
    DEFAULT_DEADHEAD_FACTOR,
    DEFAULT_DRIVER_RATE_PER_KM,
    DEFAULT_KM_PER_LITRE,
    DEFAULT_MAINTENANCE_PER_KM,
    DEFAULT_TYRE_WEAR_PER_KM,
    MarginResult,
    _FALLBACK_DIESEL_PRICE,
    calculate_true_margin,
)


class MarginCalculatorTestCase(TestCase):
    """Tests for calculate_true_margin."""

    def setUp(self):
        self.diesel_price = Decimal('21.18')
        self.fuel_price = FuelPrice.objects.create(
            date=date(2025, 3, 1),
            diesel_inland=self.diesel_price,
            diesel_coastal=Decimal('20.56'),
            petrol_95=Decimal('22.44'),
            petrol_93=Decimal('21.67'),
        )

    # ------------------------------------------------------------------
    # Return type & structure
    # ------------------------------------------------------------------

    def test_returns_margin_result(self):
        result = calculate_true_margin(
            route={'distance_km': 1400, 'tolls_zar': 500},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('25000'),
        )
        self.assertIsInstance(result, MarginResult)

    def test_cost_breakdown_has_all_keys(self):
        result = calculate_true_margin(
            route={'distance_km': 1400},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('25000'),
        )
        for key in ('fuel', 'driver', 'tolls', 'tyre_wear', 'maintenance'):
            self.assertIn(key, result.cost_breakdown)

    # ------------------------------------------------------------------
    # Fuel cost
    # ------------------------------------------------------------------

    def test_fuel_cost_uses_db_diesel_price(self):
        """fuel = effective_km / km_per_litre * diesel_inland"""
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        effective_km = Decimal('1000') * DEFAULT_DEADHEAD_FACTOR
        expected = (effective_km / DEFAULT_KM_PER_LITRE * self.diesel_price).quantize(
            Decimal('0.01')
        )
        self.assertEqual(result.cost_breakdown['fuel'], expected)
        self.assertEqual(result.fuel_price_used, self.diesel_price)

    def test_rigid_truck_uses_higher_km_per_litre(self):
        """Rigid trucks (4.5 km/L) consume less fuel than articulated (2.8 km/L)."""
        artic = calculate_true_margin(
            route={'distance_km': 500},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('10000'),
        )
        rigid = calculate_true_margin(
            route={'distance_km': 500},
            truck_type='rigid',
            load_type='general',
            quote_price=Decimal('10000'),
        )
        self.assertLess(rigid.cost_breakdown['fuel'], artic.cost_breakdown['fuel'])

    def test_unknown_truck_type_uses_default(self):
        """Unknown truck type falls back to articulated fuel consumption."""
        default = calculate_true_margin(
            route={'distance_km': 500},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('10000'),
        )
        unknown = calculate_true_margin(
            route={'distance_km': 500},
            truck_type='mystery_truck',
            load_type='general',
            quote_price=Decimal('10000'),
        )
        self.assertEqual(unknown.cost_breakdown['fuel'], default.cost_breakdown['fuel'])

    # ------------------------------------------------------------------
    # Per-km costs
    # ------------------------------------------------------------------

    def test_driver_cost_uses_effective_km(self):
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        effective_km = Decimal('1000') * DEFAULT_DEADHEAD_FACTOR
        expected = (effective_km * DEFAULT_DRIVER_RATE_PER_KM).quantize(Decimal('0.01'))
        self.assertEqual(result.cost_breakdown['driver'], expected)

    def test_tyre_wear_cost_uses_effective_km(self):
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        effective_km = Decimal('1000') * DEFAULT_DEADHEAD_FACTOR
        expected = (effective_km * DEFAULT_TYRE_WEAR_PER_KM).quantize(Decimal('0.01'))
        self.assertEqual(result.cost_breakdown['tyre_wear'], expected)

    def test_maintenance_cost_uses_effective_km(self):
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        effective_km = Decimal('1000') * DEFAULT_DEADHEAD_FACTOR
        expected = (effective_km * DEFAULT_MAINTENANCE_PER_KM).quantize(Decimal('0.01'))
        self.assertEqual(result.cost_breakdown['maintenance'], expected)

    # ------------------------------------------------------------------
    # Tolls
    # ------------------------------------------------------------------

    def test_tolls_added_to_true_cost(self):
        no_tolls = calculate_true_margin(
            route={'distance_km': 1000, 'tolls_zar': 0},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        with_tolls = calculate_true_margin(
            route={'distance_km': 1000, 'tolls_zar': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        self.assertEqual(with_tolls.true_cost - no_tolls.true_cost, Decimal('1000.00'))

    def test_zero_tolls_when_not_supplied(self):
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        self.assertEqual(result.cost_breakdown['tolls'], Decimal('0.00'))

    # ------------------------------------------------------------------
    # Deadhead factor
    # ------------------------------------------------------------------

    def test_effective_distance_applies_default_deadhead(self):
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        self.assertEqual(result.effective_distance_km, Decimal('1300.00'))

    def test_custom_deadhead_factor_accepted(self):
        base = calculate_true_margin(
            route={'distance_km': 1000, 'deadhead_factor': '1.0'},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        with_deadhead = calculate_true_margin(
            route={'distance_km': 1000, 'deadhead_factor': '1.3'},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        self.assertGreater(with_deadhead.true_cost, base.true_cost)

    # ------------------------------------------------------------------
    # Margin maths
    # ------------------------------------------------------------------

    def test_margin_zar_equals_price_minus_true_cost(self):
        result = calculate_true_margin(
            route={'distance_km': 1400, 'tolls_zar': 500},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('25000'),
        )
        self.assertEqual(
            result.margin_zar,
            (Decimal('25000') - result.true_cost).quantize(Decimal('0.01')),
        )

    def test_margin_pct_calculated_from_quote_price(self):
        result = calculate_true_margin(
            route={'distance_km': 1400},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('25000'),
        )
        expected_pct = (
            result.margin_zar / Decimal('25000') * Decimal('100')
        ).quantize(Decimal('0.01'))
        self.assertEqual(result.margin_pct, expected_pct)

    def test_negative_margin_when_price_too_low(self):
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('100'),
        )
        self.assertLess(result.margin_zar, Decimal('0'))
        self.assertLess(result.margin_pct, Decimal('0'))

    def test_distance_field_preserved(self):
        result = calculate_true_margin(
            route={'distance_km': 750},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('15000'),
        )
        self.assertEqual(result.distance_km, Decimal('750'))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_zero_distance_raises_value_error(self):
        with self.assertRaises(ValueError):
            calculate_true_margin(
                route={'distance_km': 0},
                truck_type='articulated',
                load_type='general',
                quote_price=Decimal('20000'),
            )

    def test_negative_distance_raises_value_error(self):
        with self.assertRaises(ValueError):
            calculate_true_margin(
                route={'distance_km': -500},
                truck_type='articulated',
                load_type='general',
                quote_price=Decimal('20000'),
            )

    # ------------------------------------------------------------------
    # Fallback diesel price
    # ------------------------------------------------------------------

    def test_fallback_diesel_price_when_no_fuel_records(self):
        FuelPrice.objects.all().delete()
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
        )
        self.assertEqual(result.fuel_price_used, _FALLBACK_DIESEL_PRICE)

    # ------------------------------------------------------------------
    # Interface compatibility
    # ------------------------------------------------------------------

    def test_client_id_accepted_without_error(self):
        """client_id is reserved for future use; must be accepted silently."""
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='general',
            quote_price=Decimal('20000'),
            client_id=42,
        )
        self.assertIsInstance(result, MarginResult)

    def test_load_type_accepted_without_error(self):
        """load_type is reserved for future adjustments; must be accepted."""
        result = calculate_true_margin(
            route={'distance_km': 1000},
            truck_type='articulated',
            load_type='hazmat',
            quote_price=Decimal('20000'),
        )
        self.assertIsInstance(result, MarginResult)
