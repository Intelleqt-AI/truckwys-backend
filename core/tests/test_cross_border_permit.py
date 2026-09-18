"""The SA C-BRTA permit is a per-period cost, charged per crossing.

It used to be folded into each corridor's stored fee at a flat R376.71, which
is only right for a fleet crossing exactly 24 times a year — and Zimbabwe's row
never included it at all. It is now computed from the gazetted annual fee, the
load's weight class and the fleet's own crossing count.
"""
from decimal import Decimal

from django.test import TestCase

from core.models import BorderCrossingFee
from core.services import cross_border as cb


class AmortisedPermitTests(TestCase):
    def test_weight_picks_the_freight_class(self):
        # Class 1 up to 20 000kg (2025 gazette, superseded); Class 2 above it
        # (Gazette 54229, effective 1 Apr 2026).
        self.assertEqual(cb.cbrta_annual_permit(15_000), 6_767)
        self.assertEqual(cb.cbrta_annual_permit(20_000), 6_767)   # boundary is inclusive
        self.assertEqual(cb.cbrta_annual_permit(20_001), 9_041)
        self.assertEqual(cb.cbrta_annual_permit(34_000), 9_041)

    def test_cost_per_crossing_falls_as_the_fleet_crosses_more(self):
        self.assertEqual(cb.amortised_sa_permit(30_000, 24), 376.71)
        self.assertEqual(cb.amortised_sa_permit(30_000, 200), 45.2)
        self.assertGreater(cb.amortised_sa_permit(30_000, 6),
                           cb.amortised_sa_permit(30_000, 24))

    def test_a_year_of_crossings_pays_exactly_one_annual_permit(self):
        # The whole point of per-crossing amortisation: it has to reconcile.
        for n in (6, 24, 50, 200):
            self.assertAlmostEqual(cb.amortised_sa_permit(30_000, n) * n, 9_041, delta=n * 0.01)

    def test_nonsense_crossing_counts_fall_back_to_the_default(self):
        self.assertEqual(cb.amortised_sa_permit(30_000, 0), cb.amortised_sa_permit(30_000, 24))
        self.assertEqual(cb.amortised_sa_permit(30_000, None), cb.amortised_sa_permit(30_000, 24))


class CrossBorderCostTests(TestCase):
    def _permit_lines(self, result):
        return [b for b in result['breakdown'] if b['type'] == 'sa_permit']

    def test_zimbabwe_now_carries_a_permit(self):
        # The omission this split fixes: every other corridor had one folded in,
        # Beitbridge did not.
        r = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, weight_kg=30_000, crossings_per_year=24)
        self.assertEqual(len(self._permit_lines(r)), 1)
        self.assertEqual(self._permit_lines(r)[0]['amount'], 376.71)

    def test_one_permit_per_sa_crossing(self):
        # SA -> ZW -> ZM crosses an SA border once; the ZW->ZM hop is not ours.
        r = cb.calculate_cross_border_costs(['SA', 'ZW', 'ZM'], 1900, weight_kg=30_000, crossings_per_year=24)
        self.assertEqual(len(self._permit_lines(r)), 1)

    def test_a_return_leg_pays_its_own_share(self):
        there = cb.calculate_cross_border_costs(['SA', 'BW'], 290, weight_kg=30_000, crossings_per_year=24)
        back = cb.calculate_cross_border_costs(['BW', 'SA'], 290, weight_kg=30_000, crossings_per_year=24)
        self.assertEqual(len(self._permit_lines(there)), 1)
        self.assertEqual(len(self._permit_lines(back)), 1)

    def test_domestic_routes_pay_nothing(self):
        r = cb.calculate_cross_border_costs(['SA'], 500, weight_kg=30_000)
        self.assertEqual(r['total'], 0)
        self.assertEqual(r['breakdown'], [])

    def test_splitting_the_permit_out_did_not_change_the_total_at_24_crossings(self):
        # Botswana was R1,550 with R376.71 of permit inside it. Rebuilt from the
        # stripped row plus a freshly amortised permit it must land within a rand.
        r = cb.calculate_cross_border_costs(['SA', 'BW'], 290, weight_kg=30_000, crossings_per_year=24)
        border = sum(b['amount'] for b in r['breakdown']
                     if b['type'] in ('border_crossing', 'sa_permit'))
        self.assertAlmostEqual(border, 1550, delta=12)


class FallbackDriftTests(TestCase):
    """The hardcoded table only runs when a DB row is missing. A stale one
    returns a plausible wrong number with no signal, so it must track the rows."""

    def test_border_fee_fallbacks_match_db(self):
        mismatches = []
        for key, fallback in cb._FALLBACK_BORDER_FEES.items():
            fc, tc = key.split('-')
            # The fallback table holds one value per corridor, and that value is
            # the heavy band — the only one every corridor has. Compare like
            # with like, not against Namibia's new 2-axle row.
            row = BorderCrossingFee.objects.filter(
                from_country=fc, to_country=tc, is_active=True,
                max_weight_kg__isnull=True).order_by('-min_weight_kg').first()
            if row and abs(float(row.fee_zar) - float(fallback)) > 0.01:
                mismatches.append(f'{key}: DB R{row.fee_zar} vs fallback R{fallback}')
        self.assertEqual(mismatches, [], 'fallbacks have drifted from the seeded rows')

    def test_no_fallback_still_carries_the_sa_permit(self):
        # Splitting it out means no corridor constant may include it again.
        for key, fee in cb._FALLBACK_BORDER_FEES.items():
            if key.startswith('SA-') or key.endswith('-SA'):
                self.assertNotAlmostEqual(
                    float(fee) % 376.71, 0.0, places=2,
                    msg=f'{key} looks like it still embeds the old flat permit')


class FlatCountryTollTests(TestCase):
    """Mozambique's TRAC plazas charge one fixed amount however far the route
    runs past them. Charging it per km was right only at the 95km the rate was
    derived from, and over-charged every longer route."""

    def _mz_toll(self, distance_km):
        r = cb.calculate_cross_border_costs(['SA', 'MZ'], distance_km, weight_kg=30_000)
        return next(b for b in r['breakdown'] if b['type'] == 'non_sa_toll')

    def test_mozambique_toll_does_not_grow_with_distance(self):
        near, far = self._mz_toll(560), self._mz_toll(900)
        self.assertEqual(near['amount'], far['amount'])
        self.assertEqual(near['amount'], 598.78)

    def test_the_label_says_it_is_a_gate_charge(self):
        self.assertIn('fixed gate charge', self._mz_toll(560)['description'])

    def test_the_old_per_km_figure_is_gone(self):
        # 197km x R6.30 = R1,242.99 was the number this replaces.
        self.assertLess(self._mz_toll(650)['amount'], 700)

    def test_per_km_countries_still_scale(self):
        def zw(d):
            r = cb.calculate_cross_border_costs(['SA', 'ZW'], d, weight_kg=30_000)
            return next(b for b in r['breakdown'] if b['type'] == 'non_sa_toll')['amount']
        self.assertGreater(zw(1600), zw(1120))


class WeightBandedBorderFeeTests(TestCase):
    """The destination country's charge is banded in its own schedule, so it has
    to follow the load's weight — otherwise a 15t rigid pays a 7-axle
    interlink's rate while its SA permit is priced Class 1."""

    def _fee_line(self, to, weight_kg, distance=1750):
        r = cb.calculate_cross_border_costs(['SA', to], distance, weight_kg=weight_kg)
        return next(b for b in r['breakdown'] if b['type'] == 'border_crossing')

    def test_namibia_scales_with_weight(self):
        light = self._fee_line('NA', 6_000)['amount']
        mid = self._fee_line('NA', 15_000)['amount']
        heavy = self._fee_line('NA', 30_000)['amount']
        self.assertLess(light, mid)
        self.assertLess(mid, heavy)
        self.assertEqual(heavy, 4463.29)   # the sourced 7-axle figure is unchanged

    def test_namibia_light_band_follows_the_published_per_axle_rule(self):
        # N$4,463 over 7 axles, a 2-axle rigid pays 2 of them.
        self.assertAlmostEqual(self._fee_line('NA', 6_000)['amount'],
                               round(4463.29 / 7 * 2, 2), delta=0.02)

    def test_a_corridor_with_no_light_band_says_so(self):
        # Botswana's schedule has lower bands; we do not hold the figures, so
        # the interlink rate is used and the quote admits it.
        line = self._fee_line('BW', 6_000, distance=400)
        self.assertIn('no lighter band on file', line['description'])

    def test_a_heavy_load_gets_no_such_warning(self):
        self.assertNotIn('no lighter band', self._fee_line('BW', 30_000, distance=400)['description'])

    def test_fee_and_permit_no_longer_assume_different_trucks(self):
        # The contradiction this fixes: a 7-axle border charge beside a
        # Class 1 (<=20t) permit on the same quote.
        r = cb.calculate_cross_border_costs(['SA', 'NA'], 1750, weight_kg=15_000)
        fee = next(b for b in r['breakdown'] if b['type'] == 'border_crossing')['amount']
        permit = next(b for b in r['breakdown'] if b['type'] == 'sa_permit')
        self.assertIn('Class 1', permit['description'])
        self.assertLess(fee, 4463.29)   # not the interlink rate


class BandOnTheTruckTests(TestCase):
    """Both schedules that scale are written about the vehicle, not the cargo,
    so a known truck should decide the band and the payload is only the
    fallback for a quote priced before any truck is picked."""

    def _permit(self, weight_kg, capacity_kg=0):
        r = cb.calculate_cross_border_costs(
            ['SA', 'ZW'], 1120, weight_kg=weight_kg, vehicle_capacity_kg=capacity_kg)
        return next(b for b in r['breakdown'] if b['type'] == 'sa_permit')

    def test_the_truck_decides_when_one_is_known(self):
        # 18t of cargo on a 34t interlink is still an interlink.
        self.assertIn('Class 2', self._permit(18_000, capacity_kg=34_000)['description'])
        self.assertIn('Class 1', self._permit(18_000, capacity_kg=8_000)['description'])

    def test_payload_decides_when_no_truck_is_picked(self):
        self.assertIn('Class 1', self._permit(18_000)['description'])
        self.assertIn('Class 2', self._permit(21_000)['description'])

    def test_one_truck_gives_one_band_whatever_the_load(self):
        # The old cliff: 20 000kg vs 20 001kg jumped R1,370 on the same truck.
        light = self._permit(19_000, capacity_kg=34_000)['amount']
        heavy = self._permit(21_000, capacity_kg=34_000)['amount']
        self.assertEqual(light, heavy)


class MeasuredCountryDistanceTests(TestCase):
    """Per-country kilometres come off the route's own COUNTRY sections. The
    old estimate leaned on a constant measured from one assumed origin city,
    and on short routes a 10% floor decided the answer outright."""

    GEOM = [{'lat': -26.0 + i * 0.1, 'lon': 28.0} for i in range(11)]
    SECTIONS = [
        {'type': 'COUNTRY', 'start': 0, 'end': 5, 'country_code': 'ZA'},
        {'type': 'COUNTRY', 'start': 5, 'end': 10, 'country_code': 'ZW'},
    ]

    def test_distances_are_summed_per_country(self):
        km = cb.country_distances_km(self.GEOM, self.SECTIONS)
        self.assertEqual(set(km), {'SA', 'ZW'})
        self.assertAlmostEqual(km['SA'], km['ZW'], delta=1)   # equal halves here

    def test_iso_codes_are_normalised(self):
        self.assertIn('SA', cb.country_distances_km(self.GEOM, self.SECTIONS))

    def test_unusable_sections_fall_back_rather_than_guess(self):
        self.assertEqual(cb.country_distances_km([], self.SECTIONS), {})
        self.assertEqual(cb.country_distances_km(self.GEOM, []), {})
        self.assertEqual(cb.country_distances_km(self.GEOM, [{'start': 3, 'end': 1, 'country_code': 'ZW'}]), {})

    def test_measured_distance_is_used_for_tolls(self):
        measured = cb.calculate_cross_border_costs(
            ['SA', 'ZW'], 1120, weight_kg=30_000, country_km={'ZW': 600})
        line = next(b for b in measured['breakdown'] if b['type'] == 'non_sa_toll')
        self.assertIn('600 km', line['description'])
        self.assertNotIn('~', line['description'])   # no tilde: it was measured

    def test_estimate_still_marks_itself_as_one(self):
        est = cb.calculate_cross_border_costs(['SA', 'ZW'], 1120, weight_kg=30_000)
        line = next(b for b in est['breakdown'] if b['type'] == 'non_sa_toll')
        self.assertIn('~', line['description'])
