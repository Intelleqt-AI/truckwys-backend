"""Tests for T1.2 — TollPlaza model and toll_calculator service."""

from decimal import Decimal

from django.test import TestCase

from core.models import TollPlaza
from core.services.toll_calculator import (
    TRUCK_TYPE_TO_CLASS,
    TollBreakdownItem,
    TollResult,
    _detect_routes,
    calculate_tolls,
)


def _make_plaza(name, route, location_km, c2, c3, c4, c5, direction='Test Route'):
    return TollPlaza.objects.create(
        name=name,
        route=route,
        direction=direction,
        location_km=Decimal(str(location_km)),
        tariff_class_2=Decimal(str(c2)),
        tariff_class_3=Decimal(str(c3)),
        tariff_class_4=Decimal(str(c4)),
        tariff_class_5=Decimal(str(c5)),
        tariff_year=2024,
    )


class TollPlazaModelTests(TestCase):
    def setUp(self):
        self.plaza = _make_plaza('Van Reenen', 'N3', 226, 27, 57, 85, 113)

    def test_str_representation(self):
        self.assertIn('Van Reenen', str(self.plaza))
        self.assertIn('N3', str(self.plaza))

    def test_get_tariff_class_2(self):
        self.assertEqual(self.plaza.get_tariff(2), Decimal('27'))

    def test_get_tariff_class_3(self):
        self.assertEqual(self.plaza.get_tariff(3), Decimal('57'))

    def test_get_tariff_class_4(self):
        self.assertEqual(self.plaza.get_tariff(4), Decimal('85'))

    def test_get_tariff_class_5(self):
        self.assertEqual(self.plaza.get_tariff(5), Decimal('113'))

    def test_get_tariff_invalid_class(self):
        with self.assertRaises(ValueError):
            self.plaza.get_tariff(1)

    def test_get_tariff_out_of_range(self):
        with self.assertRaises(ValueError):
            self.plaza.get_tariff(6)

    def test_ordering_by_route_then_km(self):
        _make_plaza('Mariannhill', 'N3', 545, 38, 79, 119, 159)
        _make_plaza('Tugela', 'N3', 291, 25, 52, 78, 104)

        plazas = list(TollPlaza.objects.filter(route='N3'))
        self.assertEqual(plazas[0].name, 'Van Reenen')   # km 226
        self.assertEqual(plazas[1].name, 'Tugela')        # km 291
        self.assertEqual(plazas[2].name, 'Mariannhill')   # km 545


class DetectRoutesTests(TestCase):
    def test_n3_johannesburg_to_durban(self):
        routes = _detect_routes('Johannesburg', 'Durban')
        self.assertIn('N3', routes)

    def test_n3_case_insensitive(self):
        routes = _detect_routes('JOHANNESBURG', 'durban')
        self.assertIn('N3', routes)

    def test_n1_cape_town_to_johannesburg(self):
        routes = _detect_routes('Cape Town', 'Johannesburg')
        self.assertIn('N1', routes)

    def test_n2_cape_town_to_port_elizabeth(self):
        routes = _detect_routes('Cape Town', 'Port Elizabeth')
        self.assertIn('N2', routes)

    def test_n4_pretoria_to_maputo(self):
        routes = _detect_routes('Pretoria', 'Maputo')
        self.assertIn('N4', routes)

    def test_n14_johannesburg_to_springbok(self):
        routes = _detect_routes('Johannesburg', 'Springbok')
        self.assertIn('N14', routes)

    def test_unknown_route_returns_empty(self):
        routes = _detect_routes('Polokwane', 'Bloemfontein')
        self.assertEqual(routes, [])

    def test_alias_joburg(self):
        routes = _detect_routes('Joburg', 'Durban')
        self.assertIn('N3', routes)


class CalculateTollsTests(TestCase):
    def setUp(self):
        # Seed representative N3 plazas
        _make_plaza('Van Reenen', 'N3', 226, 27, 57, 85, 113, 'Johannesburg → Durban')
        _make_plaza('Mooi River',  'N3', 330, 26, 54, 81, 108, 'Johannesburg → Durban')
        _make_plaza('Mariannhill', 'N3', 545, 38, 79, 119, 159, 'Johannesburg → Durban')

    def test_combination_truck_n3_total(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'combination')

        self.assertIsInstance(result, TollResult)
        self.assertIn('N3', result.routes_used)
        self.assertEqual(result.vehicle_class, 5)
        expected_total = Decimal('113') + Decimal('108') + Decimal('159')
        self.assertEqual(result.total_zar, expected_total)

    def test_heavy_truck_n3_total(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'heavy')

        self.assertEqual(result.vehicle_class, 4)
        expected_total = Decimal('85') + Decimal('81') + Decimal('119')
        self.assertEqual(result.total_zar, expected_total)

    def test_medium_truck_n3_total(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'medium')

        self.assertEqual(result.vehicle_class, 3)
        expected_total = Decimal('57') + Decimal('54') + Decimal('79')
        self.assertEqual(result.total_zar, expected_total)

    def test_breakdown_has_correct_count(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'heavy')

        self.assertEqual(len(result.breakdown), 3)
        self.assertIsInstance(result.breakdown[0], TollBreakdownItem)

    def test_breakdown_items_ordered_by_km(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'combination')

        kms = [item.location_km for item in result.breakdown]
        self.assertEqual(kms, sorted(kms))

    def test_alias_semi_maps_to_class_5(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'semi')
        self.assertEqual(result.vehicle_class, 5)

    def test_alias_interlink_maps_to_class_5(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'interlink')
        self.assertEqual(result.vehicle_class, 5)

    def test_alias_rigid_maps_to_class_3(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'rigid')
        self.assertEqual(result.vehicle_class, 3)

    def test_unknown_truck_type_raises_value_error(self):
        with self.assertRaises(ValueError):
            calculate_tolls('Johannesburg', 'Durban', 'spaceship')

    def test_unknown_route_returns_zero_with_warning(self):
        result = calculate_tolls('Polokwane', 'Bloemfontein', 'heavy')

        self.assertEqual(result.total_zar, Decimal('0.00'))
        self.assertEqual(result.breakdown, [])
        self.assertEqual(result.routes_used, [])
        self.assertIsNotNone(result.warning)

    def test_inactive_plaza_excluded(self):
        TollPlaza.objects.filter(name='Mooi River').update(is_active=False)

        result = calculate_tolls('Johannesburg', 'Durban', 'combination')
        plaza_names = [item.plaza_name for item in result.breakdown]
        self.assertNotIn('Mooi River', plaza_names)
        self.assertEqual(len(result.breakdown), 2)

    def test_result_fields(self):
        result = calculate_tolls('Johannesburg', 'Durban', 'heavy')

        self.assertEqual(result.origin, 'Johannesburg')
        self.assertEqual(result.destination, 'Durban')
        self.assertEqual(result.truck_type, 'heavy')
        self.assertIsNone(result.warning)


class TruckTypeToClassTests(TestCase):
    def test_all_expected_keys_present(self):
        for key in ('light', 'medium', 'heavy', 'combination', 'rigid', 'semi', 'interlink'):
            self.assertIn(key, TRUCK_TYPE_TO_CLASS)

    def test_class_values_in_range(self):
        for truck_type, vehicle_class in TRUCK_TYPE_TO_CLASS.items():
            self.assertIn(vehicle_class, (2, 3, 4, 5), msg=f'{truck_type} maps to {vehicle_class}')
