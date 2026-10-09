"""Cross-border charges: the SA C-BRTA permit, the per-component border
schedule (core/services/border_schedule.py), directions, vehicle facts,
exchange rates and route-country detection.

Expected amounts are worked out by hand from the published tariffs cited in
border_schedule.py, at the fallback exchange rates (core/services/fx.FALLBACK)
the test run uses — FX_LIVE_FETCH is off in tests.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from core.services import border_schedule as bs
from core.services import cross_border as cb
from core.services import fx

USD = float(fx.FALLBACK['USD'][0])
BWP = float(fx.FALLBACK['BWP'][0])

# An interlink whose type gives its facts (so lines can be "published").
INTERLINK = dict(gross_mass_kg=56_000, axle_config='3+2+2', sanral_class=4)
INTERLINK_8 = dict(gross_mass_kg=56_000, axle_config='3+2+3', sanral_class=4)
RIGID_2 = dict(gross_mass_kg=16_000, axle_config='2', sanral_class=2)


def lines(r, code=None):
    return [b for b in r['breakdown'] if code is None or b['code'] == code]


def line(r, code):
    found = lines(r, code)
    assert len(found) == 1, (code, [b['code'] for b in r['breakdown']])
    return found[0]


class AmortisedPermitTests(TestCase):
    def test_gross_mass_picks_the_freight_class(self):
        # GG 54229 (eff. 1 Apr 2026): Class 1 R823 + R6,160; Class 2 R823 + R8,218.
        self.assertEqual(cb.cbrta_annual_permit(15_000), 6_983)
        self.assertEqual(cb.cbrta_annual_permit(20_000), 6_983)   # boundary is inclusive
        self.assertEqual(cb.cbrta_annual_permit(20_001), 9_041)

    def test_a_year_of_crossings_pays_exactly_one_annual_permit(self):
        for n in (6, 24, 50, 200):
            self.assertAlmostEqual(cb.amortised_sa_permit(30_000, n) * n, 9_041, delta=n * 0.01)

    def test_nonsense_crossing_counts_fall_back_to_the_default(self):
        self.assertEqual(cb.amortised_sa_permit(30_000, 0), cb.amortised_sa_permit(30_000, 24))
        self.assertEqual(cb.amortised_sa_permit(30_000, None), cb.amortised_sa_permit(30_000, 24))

    def test_class_follows_gross_mass_not_payload(self):
        # 15 t of cargo on a 56 t interlink is a Class 2 vehicle.
        r = cb.calculate_cross_border_costs(['SA', 'BW'], 400, weight_kg=15_000, **INTERLINK)
        self.assertIn('Class 2', line(r, 'sa_cbrta_permit')['description'])
        r = cb.calculate_cross_border_costs(['SA', 'BW'], 400, weight_kg=15_000, **RIGID_2)
        self.assertIn('Class 1', line(r, 'sa_cbrta_permit')['description'])

    def test_one_permit_per_country_served(self):
        # Zambia via Zimbabwe needs a permit for each.
        r = cb.calculate_cross_border_costs(['SA', 'ZW', 'ZM'], 1900, crossings_per_year=24,
                                            country_km={'ZW': 900, 'ZM': 150}, **INTERLINK)
        self.assertEqual(len(lines(r, 'sa_cbrta_permit')), 2)

    def test_domestic_routes_pay_nothing(self):
        r = cb.calculate_cross_border_costs(['SA'], 500, weight_kg=30_000)
        self.assertEqual((r['total'], r['breakdown']), (0, []))


class ComponentsAreLabelledTests(TestCase):
    """Every line says where it comes from and whether it is verified; an
    unverified tariff or an assumed vehicle fact makes it an estimate."""

    def test_every_line_carries_source_and_flags(self):
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, country_km={'ZW': 583}, **INTERLINK)
        for b in r['breakdown']:
            for k in ('verified', 'label', 'source', 'as_of', 'detail', 'currency', 'amount_foreign'):
                self.assertIn(k, b, b['code'])
            self.assertEqual(b['label'], 'published' if b['verified'] else 'estimate')
            if not b['verified']:
                self.assertIn('(estimate)', b['description'])

    def test_zimbabwe_is_split_into_its_parts(self):
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, country_km={'ZW': 583}, **INTERLINK)
        toll = line(r, 'zw_border_access_toll')
        self.assertTrue(toll['verified'])
        self.assertEqual((toll['currency'], toll['amount_foreign']), ('USD', 221.0))   # legal 56 t interlink: Goods vehicle
        agent = line(r, 'zw_clearing_agent')
        self.assertFalse(agent['verified'])
        self.assertEqual(agent['amount'], 2005.0)
        self.assertIn("enter your agent's fee", agent['detail'])
        self.assertTrue(line(r, 'zw_transit_fee')['verified'])            # US$10 x 6
        self.assertEqual(line(r, 'zw_transit_fee')['amount_foreign'], 60.0)
        gates = line(r, 'zw_toll_gates')
        self.assertFalse(gates['verified'])                                # the gate count is estimated
        self.assertEqual(gates['amount_foreign'], 80.0)                    # ~4 x US$20

    def test_abnormal_only_when_the_user_marks_the_load_abnormal(self):
        legal = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, gross_mass_kg=56_000, axle_config='3+2+2')
        self.assertEqual(line(legal, 'zw_border_access_toll')['amount_foreign'], 221.0)
        abnormal = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, abnormal_load=True, **INTERLINK)
        toll = line(abnormal, 'zw_border_access_toll')
        self.assertEqual((toll['amount_foreign'], toll['verified']), (375.0, True))
        self.assertIn('Abnormal load', toll['description'])

    def test_access_toll_class_comes_from_the_axles_not_the_mass(self):
        # The class is by vehicle type, so an assumed gross mass does not make it an estimate.
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, axle_config='3+2+2', sanral_class=4)
        self.assertTrue(line(r, 'zw_border_access_toll')['verified'])
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, gross_mass_kg=16_000, axle_config='2')
        self.assertEqual(line(r, 'zw_border_access_toll')['amount_foreign'], 125.0)   # 2-axle rigid: Heavy vehicle

    def test_unverified_countries_say_so(self):
        for country, code in (('LS', 'ls_toll_gate'), ('SZ', 'sz_entry_toll'), ('MZ', 'mz_insurance_inspection')):
            with self.subTest(country=country):
                r = cb.calculate_cross_border_costs(['SA', country], 500, **INTERLINK)
                self.assertFalse(line(r, code)['verified'])
                self.assertFalse(r['verified'])
                self.assertGreater(r['estimate_zar'], 0)

    def test_assumed_vehicle_facts_make_a_verified_tariff_an_estimate(self):
        known = cb.calculate_cross_border_costs(['SA', 'NA'], 900, country_km={'NA': 900}, **INTERLINK)
        assumed = cb.calculate_cross_border_costs(['SA', 'NA'], 900, country_km={'NA': 900}, sanral_class=4)
        self.assertTrue(line(known, 'na_cross_border_charge')['verified'])
        self.assertFalse(line(assumed, 'na_cross_border_charge')['verified'])
        self.assertIn('axle configuration', line(assumed, 'na_cross_border_charge')['detail'])

    def test_zambia_is_an_estimate_not_r060_per_km(self):
        r = cb.calculate_cross_border_costs(['SA', 'ZW', 'ZM'], 1900, country_km={'ZW': 900, 'ZM': 150},
                                            **INTERLINK)
        ruc = line(r, 'zm_road_user_charge')
        self.assertFalse(ruc['verified'])
        self.assertEqual((ruc['currency'], ruc['amount_foreign']), ('USD', 20.0))   # US$10 x 2 (150 km)

    def test_corridors_with_no_figures_are_unknown_not_estimated(self):
        r = cb.calculate_cross_border_costs(['SA', 'ZW', 'TZ'], 2500, **INTERLINK)
        self.assertIn('ZW-TZ', r['unknown_crossings'])
        self.assertFalse(r['complete'])

    def test_no_weighbridge_fees_anywhere(self):
        for c in ('ZW', 'BW', 'NA', 'LS', 'SZ', 'MZ'):
            r = cb.calculate_cross_border_costs(['SA', c], 900, **INTERLINK)
            self.assertEqual(r['weighbridge_fees'], 0)
            self.assertFalse(any(b['type'] == 'weighbridge' for b in r['breakdown']))


class NamibiaTests(TestCase):
    def test_cross_border_charge_by_axle_units(self):
        # RFA (eff. 1 Aug 2026): 3-axle tractor N$1,601, 2-axle trailer N$1,261, 3-axle trailer N$1,601.
        self.assertEqual(bs.na_cbc((3, 2, 2)), Decimal('4123'))   # 7-axle interlink
        self.assertEqual(bs.na_cbc((3, 2, 3)), Decimal('4463'))   # 8 axles
        self.assertEqual(bs.na_cbc((3, 3)), Decimal('3202'))      # semi: 1601 + 1601
        self.assertEqual(bs.na_cbc((2,)), Decimal('1261'))        # 2-axle truck
        self.assertEqual(bs.na_cbc((3,)), Decimal('1601'))        # 3-axle truck
        r = cb.calculate_cross_border_costs(['SA', 'NA'], 900, country_km={'NA': 900}, **INTERLINK_8)
        self.assertEqual(line(r, 'na_cross_border_charge')['amount'], 4463.0)

    def test_mass_distance_charge_by_mass_band(self):
        # N$ per 100 km: 7-16 t 13.44, 16-34 t 24.38, 34-44 t 48.92, >44 t 73.30.
        for gross, rate in ((12_000, 13.44), (30_000, 24.38), (40_000, 48.92), (56_000, 73.30)):
            with self.subTest(gross=gross):
                r = cb.calculate_cross_border_costs(['SA', 'NA'], 900, country_km={'NA': 1000},
                                                    gross_mass_kg=gross, axle_config='3+2')
                self.assertAlmostEqual(line(r, 'na_mass_distance_charge')['amount'], rate * 10, places=2)


class DirectionTests(TestCase):
    """Entry fees are not charged on the way out."""

    def test_namibia_and_eswatini_and_lesotho_charge_on_entry_only(self):
        for c, code in (('NA', 'na_cross_border_charge'), ('SZ', 'sz_entry_toll'), ('LS', 'ls_toll_gate')):
            with self.subTest(country=c):
                back = cb.calculate_cross_border_costs([c, 'SA'], 900, **INTERLINK)
                self.assertEqual(lines(back, code), [])

    def test_botswana_return_is_the_return_permit_not_two_single_trips(self):
        out = cb.calculate_cross_border_costs(['SA', 'BW'], 400, **INTERLINK)
        back = cb.calculate_cross_border_costs(['BW', 'SA'], 400, **INTERLINK)
        one_way = line(out, 'bw_single_trip_permit')
        extra = line(back, 'bw_return_permit_supplement')
        self.assertEqual(one_way['amount_foreign'], 975.0)            # SI 48/2017, 54 501-56 500 kg
        self.assertEqual(one_way['amount_foreign'] + extra['amount_foreign'], 1833.0)   # return permit P1,833
        self.assertAlmostEqual(one_way['amount'], round(975 * BWP, 2))

    def test_zimbabwe_access_toll_not_charged_coming_back(self):
        back = cb.calculate_cross_border_costs(['ZW', 'SA'], 1120, country_km={'ZW': 583}, **INTERLINK)
        self.assertEqual(lines(back, 'zw_border_access_toll'), [])
        self.assertEqual(len(lines(back, 'zw_clearing_agent')), 1)   # clearing is needed both ways
        # ZINARA's transit fee is collected on entry; charging it leaving is unverified.
        self.assertEqual(lines(back, 'zw_transit_fee'), [])
        self.assertIn('Transit fee charged on entry', line(back, 'zw_toll_gates')['detail'])

    def test_entering_zimbabwe_from_botswana_is_unknown_not_complete(self):
        # Zimbabwe's entry charges away from Beitbridge have no source:
        # JHB -> Harare via Botswana must block, not look complete.
        r = cb.calculate_cross_border_costs(['SA', 'BW', 'ZW'], 1300, **INTERLINK)
        self.assertIn('BW-ZW', r['unknown_crossings'])
        self.assertFalse(r['complete'])
        self.assertFalse(cb.corridor_fee_known('BW', 'ZW'))
        self.assertTrue(cb.corridor_fee_known('BW', 'NA'))


class ExchangeRateTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_pegged_currencies_are_exact(self):
        for c in ('NAD', 'LSL', 'SZL'):
            self.assertEqual(fx.get_rate(c).zar_per_unit, Decimal('1'))

    def test_without_a_fetch_the_fallback_says_its_date(self):
        r = fx.get_rate('USD', date(2026, 10, 9))
        self.assertTrue(r.is_fallback)
        self.assertIn('rate as of 2026-10-08', r.label)

    @override_settings(FX_LIVE_FETCH=True)
    def test_a_fetched_rate_is_cached_for_the_day(self):
        with mock.patch.object(fx, 'fetch_live', return_value=(Decimal('17.10'), date(2026, 10, 9), 'SARB EXCX135D')) as f:
            a = fx.get_rate('USD', date(2026, 10, 9))
            b = fx.get_rate('USD', date(2026, 10, 9))
        self.assertEqual(f.call_count, 1)
        self.assertEqual((a.zar_per_unit, a.is_fallback, b.zar_per_unit), (Decimal('17.10'), False, Decimal('17.10')))

    @override_settings(FX_LIVE_FETCH=True)
    def test_a_failed_fetch_uses_the_last_good_rate_and_says_so(self):
        with mock.patch.object(fx, 'fetch_live', return_value=(Decimal('17.10'), date(2026, 10, 9), 'SARB EXCX135D')):
            fx.get_rate('USD', date(2026, 10, 9))
        with mock.patch.object(fx, 'fetch_live', side_effect=OSError('down')):
            r = fx.get_rate('USD', date(2026, 10, 10))
        self.assertEqual(r.zar_per_unit, Decimal('17.10'))
        self.assertTrue(r.is_fallback)

    @override_settings(FX_LIVE_FETCH=True)
    def test_a_down_source_is_not_asked_again_for_a_while(self):
        # While SARB is down, one failed lookup is enough: the next ones go
        # straight to the last good rate instead of waiting out a timeout.
        with mock.patch.object(fx, 'fetch_live', side_effect=OSError('down')) as f:
            rates = [fx.get_rate('USD', date(2026, 10, 9)) for _ in range(12)]
        self.assertEqual(f.call_count, 1)
        self.assertTrue(all(r.is_fallback for r in rates))
        self.assertEqual(rates[-1].zar_per_unit, fx.FALLBACK['USD'][0])

    @override_settings(FX_LIVE_FETCH=True)
    def test_sources_back_off_separately_and_recover(self):
        with mock.patch.object(fx, 'fetch_live', side_effect=OSError('down')):
            fx.get_rate('MZN', date(2026, 10, 9))                      # ExchangeRate-API down
        with mock.patch.object(fx, 'fetch_live', return_value=(Decimal('17.10'), date(2026, 10, 9), 'SARB EXCX135D')) as f:
            usd = fx.get_rate('USD', date(2026, 10, 9))               # SARB still asked
            fx.get_rate('BWP', date(2026, 10, 9))                      # same down source: skipped
        self.assertEqual(f.call_count, 1)
        self.assertFalse(usd.is_fallback)
        cache.delete(fx._DOWN_KEY.format(source='erapi'))              # the backoff ends
        with mock.patch.object(fx, 'fetch_live', return_value=(Decimal('0.26'), date(2026, 10, 9), 'ExchangeRate-API')) as f:
            mzn = fx.get_rate('MZN', date(2026, 10, 9))
        self.assertEqual((f.call_count, mzn.is_fallback), (1, False))

    @override_settings(FX_LIVE_FETCH=True)
    def test_backoff_holds_when_the_cache_is_down(self):
        fx._down_until.clear()
        broken = mock.MagicMock(get=mock.Mock(side_effect=RuntimeError('cache down')),
                                set=mock.Mock(side_effect=RuntimeError('cache down')))
        try:
            with mock.patch.object(fx, 'cache', broken), \
                    mock.patch.object(fx, 'fetch_live', side_effect=OSError('down')) as f:
                fx.get_rate('USD', date(2026, 10, 9))
                fx.get_rate('USD', date(2026, 10, 9))
            self.assertEqual(f.call_count, 1)
        finally:
            fx._down_until.clear()

    def test_lines_carry_the_rate_they_used(self):
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, **INTERLINK)
        rate = line(r, 'zw_border_access_toll')['fx']
        self.assertEqual(rate['currency'], 'USD')
        self.assertTrue(rate['is_fallback'])

    def test_rates_are_returned_at_the_precision_used(self):
        self.assertEqual(fx.get_rate('MZN').as_dict()['zar_per_unit_text'], '0.25608')
        self.assertEqual(fx.get_rate('USD').as_dict()['zar_per_unit_text'], '16.6391')


class BorderPostTests(TestCase):
    def test_mozambique_line_names_the_post_on_the_route(self):
        lebombo = cb.calculate_cross_border_costs(['SA', 'MZ'], 450, crossings=[('SA', 'MZ', -25.4430, 31.9850)],
                                                  **INTERLINK)
        kosi = cb.calculate_cross_border_costs(['SA', 'MZ'], 545, crossings=[('SA', 'MZ', -26.8641, 32.8300)],
                                               **INTERLINK)
        self.assertIn('Lebombo / Ressano Garcia', line(lebombo, 'mz_insurance_inspection')['description'])
        self.assertIn('Kosi Bay / Ponta do Ouro', line(kosi, 'mz_insurance_inspection')['description'])

    def test_no_post_named_when_the_crossing_is_not_near_one(self):
        r = cb.calculate_cross_border_costs(['SA', 'MZ'], 450, crossings=[('SA', 'MZ', -24.0, 32.0)], **INTERLINK)
        self.assertNotIn(' — ', line(r, 'mz_insurance_inspection')['description'])


class RouteCountryTests(TestCase):
    """Countries come from the route's real crossings, not map noise."""

    def _geom(self, n=101, lat0=-22.0, dlat=-0.001):
        return [{'lat': lat0 + i * dlat, 'lon': 29.99} for i in range(n)]   # ~11 m a step

    def test_a_few_hundred_metres_inside_zimbabwe_at_beitbridge_is_not_a_crossing(self):
        geom = self._geom()
        sections = [{'type': 'COUNTRY', 'start': 0, 'end': 95, 'country_code': 'ZAF'},
                    {'type': 'COUNTRY', 'start': 95, 'end': 100, 'country_code': 'ZWE'}]
        self.assertEqual(cb.route_countries(geom, sections, 'ZA', 'ZA'), ['SA'])

    def test_a_real_crossing_still_counts(self):
        geom = self._geom(n=1001)
        sections = [{'type': 'COUNTRY', 'start': 0, 'end': 400, 'country_code': 'ZAF'},
                    {'type': 'COUNTRY', 'start': 400, 'end': 1000, 'country_code': 'ZWE'}]
        self.assertEqual(cb.route_countries(geom, sections, 'ZA', 'ZW'), ['SA', 'ZW'])

    def test_short_foreign_stretch_in_the_middle_is_dropped(self):
        geom = self._geom(n=1001)
        sections = [{'type': 'COUNTRY', 'start': 0, 'end': 500, 'country_code': 'ZAF'},
                    {'type': 'COUNTRY', 'start': 500, 'end': 510, 'country_code': 'SWZ'},
                    {'type': 'COUNTRY', 'start': 510, 'end': 1000, 'country_code': 'ZAF'}]
        self.assertEqual(cb.route_countries(geom, sections, 'ZA', 'ZA'), ['SA'])


class MeasuredCountryDistanceTests(TestCase):
    GEOM = [{'lat': -26.0 + i * 0.1, 'lon': 28.0} for i in range(11)]
    SECTIONS = [
        {'type': 'COUNTRY', 'start': 0, 'end': 5, 'country_code': 'ZA'},
        {'type': 'COUNTRY', 'start': 5, 'end': 10, 'country_code': 'ZW'},
    ]

    def test_distances_are_summed_per_country(self):
        km = cb.country_distances_km(self.GEOM, self.SECTIONS)
        self.assertEqual(set(km), {'SA', 'ZW'})
        self.assertAlmostEqual(km['SA'], km['ZW'], delta=1)

    def test_unusable_sections_fall_back_rather_than_guess(self):
        self.assertEqual(cb.country_distances_km([], self.SECTIONS), {})
        self.assertEqual(cb.country_distances_km(self.GEOM, []), {})

    def test_measured_distance_is_used(self):
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, country_km={'ZW': 600}, **INTERLINK)
        self.assertIn('600,0 km', line(r, 'zw_transit_fee')['description'])

    def test_distances_in_labels_to_a_tenth_of_a_km(self):
        r = cb.calculate_cross_border_costs(['SA', 'NA'], 1500, country_km={'NA': 805.7}, **INTERLINK)
        self.assertIn('805,7 km', line(r, 'na_mass_distance_charge')['description'])

    def test_a_rough_split_says_so(self):
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, **INTERLINK)
        self.assertIn('rough split', line(r, 'zw_transit_fee')['detail'])
