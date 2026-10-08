"""The authoritative quote costing (core.services.quote_costing) and its
golden vectors (QUOTE-RULES.md §12)."""
import json
import os
from pathlib import Path

from django.test import SimpleTestCase

from core.services import quote_costing as qc
from core.tests.quote_golden_cases import CASES, REOPEN_CASES, EFFECTIVE, INLAND, base, long_trip, official

GOLDEN_PATH = Path(__file__).parent / 'fixtures' / 'quote_golden.json'

RULES = {
    'spec': 'QUOTE-RULES.md (7 Oct 2026), sections 1, 3-7, 10, 12',
    'arithmetic': 'IEEE doubles, operations in the order below; cents(x) = floor(x * 100 + 0.5) / 100, '
                  'applied only to each line amount, the floor, margin and the target price.',
    'capacity_t': 'capacity / 1000 if capacity > 100 else capacity',
    'load_ratio': 'min((load_kg / 1000) / capacity_t, 1); 1 when capacity or load is unknown',
    'burn_loaded': 'rated * (0.70 + 0.30 * load_ratio)',
    'burn_empty': 'rated * 0.70',
    'empty_return': 'one-way only: include_empty_return if not null, else '
                    'settings.include_empty_return_default (true) and distance_km >= settings.empty_return_min_km (300)',
    'km_loaded': 'distance_km * (2 if ROUND_TRIP else 1); km_empty = distance_km when the empty return is included',
    'litres': 'litres_loaded = km_loaded * burn_loaded / 100; litres_empty = km_empty * burn_empty / 100',
    'fuel': 'cents(litres_loaded * price); fuel_return = cents(litres_empty * price)',
    'operating': 'cents(km_loaded * operating_cost_per_km); operating_return = cents(km_empty * operating_cost_per_km)',
    'tolls': 'cents(tolls.one_way * legs_loaded); tolls_return = cents(tolls.empty_return ?? tolls.one_way); '
             'unknown (null / lookup_failed) -> null + tolls_unknown unless confirmed_none (then 0); '
             'one_way 0 from a working lookup = R 0 known, basis "No toll plazas on this route", no warning',
    'nights': 'nights(h) = max(ceil(h / hours_per_day) - 1, 0), h = duration_minutes / 60; '
              'loaded = nights(h) one-way, nights(2h) round trip; return extra = nights(2h) - nights(h)',
    'driver': 'driver.amount if given, else cents(nights * allowance_per_night) (0 when nights = 0)',
    'floor': 'cents(sum of line amounts); null when any line is unknown',
    'target_price': 'max(cents(floor / (1 - target_margin_pct / 100)), minimum_charge)',
    'margin': 'cents(price - floor); margin_pct = (price - floor) / price * 100 (unrounded)',
    'default_price': 'round_up(max(rate_price ?? 0, target_price)), round_up(p) = ceil(p / u) * u with u = 50 '
                     'below 20 000 else 100 (the choices\' rounding); null without a floor',
    'diesel': 'override_price -> override; OWN (own_price set, not use_official) -> own; '
              'else official_price -> official; else missing',
    'diesel_own_off': '|own - official| / official > 0.03; impact_zar = cents((own - official) * litres_total)',
    'fuel_type': 'diesel.fuel_type Diesel (default) | Petrol | Electric. Petrol (petrol and hybrid trucks) uses the '
                 'same rule as diesel with the official ULP price for the zone and diesel.grade (95; 93 inland '
                 'only); warning codes stay diesel_* with fuel_type on the warning. Electric has no official '
                 'price: own price or diesel_missing.',
    'compare': 'lines[].amount, floor, floor_known, target_price, margin: exact to the cent. '
               'warnings: code, severity, impact_zar exact; title/detail are server copy. '
               'litres / burn: within 1e-9.',
}


def build_golden():
    return {
        'version': qc.VERSION,
        'generated_by': 'core.services.quote_costing.compute (pricing-backend)',
        'rules': RULES,
        'cases': [{'name': name, 'description': desc, 'inputs': inputs, 'expected': qc.compute(inputs)}
                  for name, desc, inputs in CASES],
        # Reopen notice (§11): quote_costing.changes_since_priced(price,
        # floor_then, floor_now, priced_at). Added after the cases; the cases
        # above are unchanged.
        'reopen_rules': qc.changes_since_priced.__doc__.strip(),
        'reopen_cases': [{'name': name, 'inputs': inputs, 'expected': qc.changes_since_priced(**inputs)}
                         for name, inputs in REOPEN_CASES],
    }


def _codes(out):
    return [(w['code'], w['severity']) for w in out['warnings']]


class GoldenVectorTests(SimpleTestCase):
    def test_golden_file_matches_the_function(self):
        golden = build_golden()
        if os.environ.get('QUOTE_GOLDEN_WRITE'):
            GOLDEN_PATH.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + '\n')
        stored = json.loads(GOLDEN_PATH.read_text())
        # Round-trip through JSON so floats compare as the clients read them.
        self.assertEqual(stored, json.loads(json.dumps(golden, ensure_ascii=False)))

    def test_at_least_twelve_cases_covering_the_spec(self):
        stored = json.loads(GOLDEN_PATH.read_text())
        self.assertGreaterEqual(len(stored['cases']), 12)
        codes = {w['code'] for c in stored['cases'] for w in c['expected']['warnings']}
        for code in ('diesel_own_off', 'diesel_own_old', 'diesel_missing', 'diesel_stale', 'tolls_unknown',
                     'distance_estimated', 'below_minimum_charge', 'truck_burn_suspect', 'no_vehicle'):
            self.assertIn(code, codes)


class FormulaTests(SimpleTestCase):
    def test_burn_formula_and_litres(self):
        out = qc.compute(base())
        v = out['vehicle']
        self.assertAlmostEqual(v['load_ratio'], 20 / 34)
        self.assertAlmostEqual(v['burn_loaded_l_per_100km'], 42 * (0.70 + 0.30 * 20 / 34))
        self.assertAlmostEqual(v['burn_empty_l_per_100km'], 42 * 0.70)
        fuel = out['lines'][0]
        self.assertEqual(fuel['key'], 'fuel')
        litres = 250 * (42 * (0.70 + 0.30 * (20 / 34))) / 100
        self.assertAlmostEqual(fuel['litres'], litres)
        self.assertEqual(fuel['amount'], qc.cents(litres * INLAND))

    def test_load_over_capacity_caps_ratio_and_blocks(self):
        out = qc.compute(base(load_kg=40000))
        self.assertEqual(out['vehicle']['load_ratio'], 1)
        self.assertIn(('overload', 'block'), _codes(out))

    def test_kg_capacity_is_normalised(self):
        self.assertEqual(qc.capacity_tonnes(8000), 8.0)
        self.assertEqual(qc.capacity_tonnes(100), 100.0)
        self.assertEqual(qc.capacity_tonnes(34), 34.0)
        self.assertIsNone(qc.capacity_tonnes(0))
        self.assertIsNone(qc.capacity_tonnes(None))

    def test_cents_is_half_up_like_js(self):
        self.assertEqual(qc.cents(2.345), 2.35 if 2.345 * 100 + 0.5 >= 235 else 2.34)
        self.assertEqual(qc.cents(-1.5), -1.5)
        self.assertEqual(qc.cents(0.125), 0.13)

    def test_no_weight_effect_setting(self):
        # The old per-tonne sensitivity (and `|| 2`) is gone: an empty truck
        # burns exactly 70% of rated.
        out = qc.compute(base(load_kg=0))
        self.assertAlmostEqual(out['vehicle']['burn_loaded_l_per_100km'], 42 * 0.70)


class TripShapeTests(SimpleTestCase):
    def test_empty_return_default_from_300_km(self):
        self.assertFalse(qc.compute(base(distance_km=299.9))['trip']['empty_return_included'])
        self.assertTrue(qc.compute(base(distance_km=300.0))['trip']['empty_return_included'])

    def test_return_load_booked_removes_empty_return(self):
        out = qc.compute(long_trip(include_empty_return=False))
        self.assertFalse(out['trip']['empty_return_included'])
        self.assertFalse([ln for ln in out['lines'] if ln['leg'] == 'empty_return'])

    def test_empty_return_lines(self):
        out = qc.compute(long_trip())
        keys = [ln['key'] for ln in out['lines']]
        self.assertEqual(keys, ['fuel', 'operating', 'tolls', 'driver', 'fuel_return', 'operating_return',
                                'tolls_return', 'driver_return'])
        by = {ln['key']: ln for ln in out['lines']}
        self.assertAlmostEqual(by['fuel_return']['litres'], 568.4 * 42 * 0.70 / 100)
        self.assertEqual(by['tolls_return']['amount'], 812.61)   # empty class
        self.assertEqual(by['operating_return']['amount'], qc.cents(568.4 * 16.0))
        # 7h20 one way: 0 nights; 14h40 both ways: 1 night -> 1 extra night home
        self.assertEqual(by['driver']['nights'], 0)
        self.assertEqual(by['driver_return']['nights'], 1)
        self.assertEqual(by['driver_return']['amount'], 450.0)
        self.assertEqual(out['floor'], qc.cents(sum(ln['amount'] for ln in out['lines'])))

    def test_round_trip_both_legs_loaded(self):
        out = qc.compute(long_trip(trip_type='ROUND_TRIP'))
        by = {ln['key']: ln for ln in out['lines']}
        self.assertEqual(out['trip']['km_loaded'], 568.4 * 2)
        self.assertEqual(by['tolls']['amount'], qc.cents(1043.48 * 2))
        self.assertEqual(by['driver']['nights'], 1)
        self.assertFalse(out['trip']['empty_return_included'])


class DieselTests(SimpleTestCase):
    def test_own_within_three_percent_no_warning(self):
        out = qc.compute(base(diesel=official(mode='OWN', own_price=INLAND * 1.03,
                                              own_set_at='2026-10-08T00:00:00Z')))
        self.assertEqual(out['diesel']['source'], 'own')
        self.assertNotIn('diesel_own_off', [c for c, _ in _codes(out)])

    def test_own_off_has_impact_on_this_quote(self):
        out = qc.compute(base(diesel=official(mode='OWN', own_price=30.0, own_set_at=EFFECTIVE)))
        w = next(w for w in out['warnings'] if w['code'] == 'diesel_own_off')
        self.assertEqual(w['impact_zar'], qc.cents((30.0 - INLAND) * out['litres']['total']))
        self.assertEqual([a['id'] for a in w['actions']], ['use_official', 'update_own'])
        self.assertIn('R 30,00', w['detail'])
        self.assertIn('R 32,80', w['detail'])
        self.assertNotIn('diesel_own_old', [c for c, _ in _codes(out)])   # set exactly at the change

    def test_empty_own_price_means_live(self):
        out = qc.compute(base(diesel=official(mode='OWN', own_price=None)))
        self.assertEqual(out['diesel']['mode'], 'LIVE')
        self.assertEqual(out['diesel']['source'], 'official')

    def test_missing_never_defaults(self):
        out = qc.compute(base(diesel=official(price=None)))
        self.assertIsNone(out['diesel']['price'])
        self.assertIsNone(out['floor'])
        self.assertIn(('diesel_missing', 'block'), _codes(out))
        self.assertFalse(out['can_send'])

    def test_warning_copy_limits(self):
        for _name, _desc, inputs in CASES:
            for w in qc.compute(inputs)['warnings']:
                self.assertLessEqual(len(w['title'].split()), 8, w['title'])
                self.assertEqual(w['detail'].count('. '), 0, w['detail'])
                self.assertIn(w['severity'], ('block', 'warn'))


class PriceTests(SimpleTestCase):
    def test_target_price_and_margin(self):
        out = qc.compute(base())
        self.assertEqual(out['target_price'], qc.cents(out['floor'] / 0.9))
        self.assertEqual(out['margin'], qc.cents(12500 - out['floor']))

    def test_minimum_charge_lifts_target_and_blocks_below(self):
        out = qc.compute(base(minimum_charge=15000.0))
        self.assertEqual(out['target_price'], 15000.0)
        self.assertIn(('below_minimum_charge', 'block'), _codes(out))
        self.assertNotIn('below_minimum_charge', [c for c, _ in _codes(qc.compute(base(minimum_charge=15000.0,
                                                                                         price=15000.0)))])


class ReopenTests(SimpleTestCase):
    def test_costs_up_keeps_margin(self):
        out = qc.changes_since_priced(20000, 17200, 18250, '2026-09-02T10:00:00Z')
        self.assertEqual(out['delta_zar'], 1050)
        self.assertAlmostEqual(out['margin_then'], 14.0)
        self.assertAlmostEqual(out['margin_now'], 8.75)
        self.assertEqual(out['repriced_price_keep_margin'], qc.cents(18250 / 0.86))
        self.assertEqual(out['notice'], 'Costs up R 1 050 since 2 Sep. Margin 14% → 9%.')

    def test_unchanged_and_unknown(self):
        self.assertFalse(qc.changes_since_priced(20000, 17200, 17200.4)['changed'])
        out = qc.changes_since_priced(20000, None, 18000)
        self.assertIsNone(out['delta_zar'])
        self.assertIsNone(out['repriced_price_keep_margin'])


class DisplayRoundingTests(SimpleTestCase):
    def test_half_up_on_the_decimal_form(self):
        self.assertEqual(qc.fmt_num(1.005, 2), '1,01')      # float 1.00499.. but shown half-up
        self.assertEqual(qc.fmt_num(2.5), '3')
        self.assertEqual(qc.fmt_num(1049.5), '1 050')
        self.assertEqual(qc.fmt_rand(-0.4), 'R 0')
        self.assertEqual(qc.fmt_rand(32.795, 2), 'R 32,80')

    def test_own_off_impact_is_the_difference_of_fuel_lines(self):
        out = qc.compute(long_trip(diesel=official(mode='OWN', own_price=30.0, own_set_at=EFFECTIVE)))
        w = next(w for w in out['warnings'] if w['code'] == 'diesel_own_off')
        own_fuel = sum(ln['amount'] for ln in out['lines'] if ln['key'] in ('fuel', 'fuel_return'))
        off = qc.compute(long_trip())
        off_fuel = sum(ln['amount'] for ln in off['lines'] if ln['key'] in ('fuel', 'fuel_return'))
        self.assertEqual(w['impact_zar'], qc.cents(own_fuel - off_fuel))
        self.assertNotIn('R', w['detail'].split('(inland)')[1])     # amount not repeated in the detail
