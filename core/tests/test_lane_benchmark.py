"""Tests for core.services.lane_benchmark's market-rate resolution.

These cover the two reasons the benchmark silently returned nothing for most
production quotes: the coarse estimate table only listed one direction per
lane, and the own-company tier filtered by vehicle type with no lane-level
retry, so excluding the quote being priced took every group under its floor.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import Company, Customer, Quote
from core.services.lane_benchmark import (
    MIN_DISTINCT_OPERATORS,
    SA_MARKET_ESTIMATES,
    canon_code,
    derive_lane_code,
    lookup_sa_estimate,
    resolve_market_rate,
)
from core.tests.test_price_analysis import make_quote

User = get_user_model()


class SaEstimateLookupTests(TestCase):
    def test_listed_direction_resolves(self):
        self.assertEqual(lookup_sa_estimate('JHB', 'CPT', 'interlink')['avg'], 43800)

    def test_return_leg_resolves_from_the_mirror(self):
        # Every CPT->JHB quote in production fell through all four tiers
        # purely because only JHB->CPT was listed.
        self.assertEqual(lookup_sa_estimate('CPT', 'JHB', 'interlink')['avg'], 43800)
        self.assertEqual(lookup_sa_estimate('DBN', 'JHB', 'truck')['avg'], 15000)

    def test_unlisted_vehicle_type_degrades_to_truck(self):
        self.assertEqual(
            lookup_sa_estimate('CPT', 'JHB', 'Heavy Truck (8-16 tonnes)')['avg'], 38900
        )

    def test_city_alias_applies_before_the_mirror(self):
        # DUR is the frontend's spelling; the table keys on DBN.
        self.assertEqual(canon_code('DUR'), 'DBN')
        self.assertEqual(lookup_sa_estimate('DUR', 'CPT', 'interlink')['avg'], 52000)

    def test_explicit_direction_wins_over_the_mirror(self):
        # Mirroring is an assumption; a real directional rate must beat it.
        with self.settings():
            SA_MARKET_ESTIMATES[('CPT', 'JHB', 'interlink')] = {
                'avg': 1, 'low': 1, 'high': 1}
            try:
                self.assertEqual(lookup_sa_estimate('CPT', 'JHB', 'interlink')['avg'], 1)
            finally:
                del SA_MARKET_ESTIMATES[('CPT', 'JHB', 'interlink')]

    def test_same_origin_and_destination_has_no_rate(self):
        # PE->PE rows exist in production (free-text fields, and 'PE' matched a
        # street name) — a journey to itself has no lane rate.
        self.assertIsNone(lookup_sa_estimate('PE', 'PE', 'Rigid Truck'))

    def test_unrecognised_codes_return_none(self):
        # Street numbers reached these fields; they must not match a lane.
        self.assertIsNone(lookup_sa_estimate('21', '128', 'Interlink'))
        self.assertIsNone(lookup_sa_estimate('CPT', 'DAR', 'Rigid Truck'))


class OwnCompanyTierTests(TestCase):
    """Tier 3 — this operator's own won quotes. Never cross-tenant."""

    def setUp(self):
        self.company = Company.objects.create(company_name="Tier3 Co")
        self.user = User.objects.create_user(
            username='t3', email='t3@test.com', password='x', company=self.company)
        self.customer = Customer.objects.create(
            company=self.company, name='Cust', email='cust@tier3.test')
        self._n = 0

    def _won(self, vehicle_type, amount, origin='CPT', destination='JHB', company=None,
             created_by=None, customer=None):
        self._n += 1
        return make_quote(
            company or self.company, customer or self.customer,
            number=f'LB-{self._n:04d}', total=amount, origin=origin,
            destination=destination, status='ACCEPTED', outcome='accepted',
            vehicle_type=vehicle_type, created_by=created_by or self.user,
        )

    def test_lane_level_retry_rescues_a_vehicle_type_split(self):
        # The production shape: 6 won quotes on one lane, split 3/2/1 across
        # vehicle types. Excluding the quote being priced left the biggest
        # group at 2, under the >= 3 floor, so this tier never fired.
        for amt in (30000, 31000, 32000):
            self._won('Heavy Truck (8-16 tonnes)', amt)
        for amt in (40000, 41000):
            self._won('Interlink / B-Train (34 tonnes)', amt)
        priced = self._won('flat bed', 35000)

        rate, source = resolve_market_rate(
            'CPT', 'JHB', 'flat bed', company=self.company, exclude_quote_id=priced.id,
        )
        self.assertEqual(source, 'company')
        # Averaged across all five remaining won quotes on the lane.
        self.assertAlmostEqual(rate, (30000 + 31000 + 32000 + 40000 + 41000) / 5, places=2)

    def test_vehicle_specific_average_is_preferred_when_it_qualifies(self):
        for amt in (30000, 31000, 32000):
            self._won('Heavy Truck (8-16 tonnes)', amt)
        self._won('Interlink / B-Train (34 tonnes)', 99000)

        rate, source = resolve_market_rate(
            'CPT', 'JHB', 'Heavy Truck (8-16 tonnes)', company=self.company,
        )
        self.assertEqual(source, 'company')
        self.assertAlmostEqual(rate, 31000, places=2)  # the interlink outlier excluded

    def test_falls_through_to_estimate_below_the_floor(self):
        self._won('Heavy Truck (8-16 tonnes)', 30000)
        rate, source = resolve_market_rate(
            'CPT', 'JHB', 'Heavy Truck (8-16 tonnes)', company=self.company,
        )
        self.assertEqual(source, 'estimate')  # the mirrored JHB->CPT truck rate
        self.assertEqual(rate, 38900.0)

    def test_no_data_and_no_estimate_reports_none(self):
        rate, source = resolve_market_rate(
            'CPT', 'DAR', 'Rigid Truck', company=self.company,
        )
        self.assertIsNone(rate)
        self.assertEqual(source, 'none')

    def test_own_company_tier_never_reads_another_operator(self):
        other = Company.objects.create(company_name="Someone Else")
        other_user = User.objects.create_user(
            username='oe', email='oe@test.com', password='x', company=other)
        other_customer = Customer.objects.create(
            company=other, name='Other Cust', email='cust@other.test')
        for amt in (10000, 11000, 12000, 13000, 14000):
            self._won('Rigid Truck', amt, origin='CPT', destination='DAR',
                      company=other, created_by=other_user, customer=other_customer)
        # Five won quotes on the lane, but all from one *other* operator:
        # k-anonymity needs MIN_DISTINCT_OPERATORS, and tier 3 is scoped to
        # the caller's own company, so nothing should leak.
        self.assertEqual(MIN_DISTINCT_OPERATORS, 2)
        rate, source = resolve_market_rate(
            'CPT', 'DAR', 'Rigid Truck', company=self.company,
        )
        self.assertIsNone(rate)
        self.assertEqual(source, 'none')

    def test_window_excludes_quotes_older_than_the_fallback_period(self):
        old = timezone.now() - timedelta(days=400)
        for amt in (30000, 31000, 32000):
            q = self._won('Heavy Truck (8-16 tonnes)', amt)
            Quote.objects.filter(pk=q.pk).update(created_at=old)
        rate, source = resolve_market_rate(
            'CPT', 'JHB', 'Heavy Truck (8-16 tonnes)', company=self.company,
        )
        self.assertEqual(source, 'estimate')  # not 'company' — all too old


class LaneCodeDerivationTests(TestCase):
    """origin/destination drive every market-rate tier, and they used to be
    computed in the browser by two functions that disagreed. One took the first
    three characters of the address, so "21 Smith Street" became the lane code
    `21`; the other matched city names as bare substrings, so a street
    containing "PE" became Port Elizabeth."""

    def test_explicit_code_is_kept(self):
        self.assertEqual(derive_lane_code('JHB', 'Johannesburg, GP'), 'JHB')

    def test_alias_is_canonicalised(self):
        self.assertEqual(derive_lane_code('DUR', 'Durban'), 'DBN')

    def test_street_number_is_repaired_from_the_address(self):
        self.assertEqual(derive_lane_code('21', '21 Smith Street, Johannesburg'), 'JHB')
        self.assertEqual(derive_lane_code('128', '128 Main Rd, Cape Town'), 'CPT')

    def test_derived_from_address_when_no_code_given(self):
        self.assertEqual(derive_lane_code('', 'Pretoria CBD'), 'PTA')
        self.assertEqual(derive_lane_code('', 'Gqeberha Harbour'), 'PE')

    def test_city_name_inside_a_word_is_not_a_match(self):
        # The 'PE' -> Port Elizabeth substring bug.
        self.assertEqual(derive_lane_code('', '14 PEPPER STREET, Nelspruit'), '')
        self.assertEqual(derive_lane_code('', 'Speedway Industrial Park'), '')

    def test_unknown_city_yields_blank_not_junk(self):
        # Blank makes resolve_market_rate bail immediately; a wrong code would
        # silently pollute the lane statistics every tier is computed from.
        self.assertEqual(derive_lane_code('889', 'Plot 889, Rustenburg'), '')
        self.assertEqual(derive_lane_code('', ''), '')

    def test_saving_a_quote_normalises_its_lane_codes(self):
        company = Company.objects.create(company_name='Lane Co')
        customer = Customer.objects.create(
            company=company, name='C', email='c@lane.test')
        q = make_quote(
            company, customer, number='LANE-1', origin='21', destination='128',
            pickup_location='21 Smith Street, Johannesburg',
            delivery_location='128 Main Rd, Cape Town',
        )
        q.refresh_from_db()
        self.assertEqual(q.origin, 'JHB')
        self.assertEqual(q.destination, 'CPT')


class PlatformBenchmarkOutlierTests(TestCase):
    """compute_lane_benchmark's cross-platform tier — the sanity cap and the
    median-over-mean switch in resolve_market_rate.

    Provoked by a real incident: a handful of quotes had base_rate keyed in
    at ~1000x the intended R/km (a Rigid Truck at R8,000/km instead of
    ~R15/km). A plain average of total_amount turned CPT<->DBN's benchmark
    into R857,223 for every OTHER company quoting that lane -- nothing in
    compute_lane_benchmark had ever needed to defend against a single
    fat-fingered entry three orders of magnitude off the rest of the sample.
    """

    def setUp(self):
        self.company_a = Company.objects.create(company_name='Platform Co A')
        self.company_b = Company.objects.create(company_name='Platform Co B')
        self.user_a = User.objects.create_user(
            username='pa', email='pa@test.com', password='x', company=self.company_a)
        self.user_b = User.objects.create_user(
            username='pb', email='pb@test.com', password='x', company=self.company_b)
        self.cust_a = Customer.objects.create(
            company=self.company_a, name='A', email='a@platform.test')
        self.cust_b = Customer.objects.create(
            company=self.company_b, name='B', email='b@platform.test')
        self._n = 0

    def _won(self, company, customer, user, amount, origin='CPT', destination='DBN'):
        self._n += 1
        return make_quote(
            company, customer, number=f'PB-{self._n:04d}', total=amount,
            origin=origin, destination=destination, status='ACCEPTED',
            outcome='accepted', created_by=user,
        )

    def test_extreme_outlier_is_excluded_from_every_statistic(self):
        from core.services.lane_benchmark import compute_lane_benchmark

        for amt in (30000, 31000, 32000):
            self._won(self.company_a, self.cust_a, self.user_a, amt)
        for amt in (33000, 34000):
            self._won(self.company_b, self.cust_b, self.user_b, amt)
        # The data-entry-error shape: three orders of magnitude off the rest.
        self._won(self.company_b, self.cust_b, self.user_b, 30_000_000)

        result = compute_lane_benchmark('CPT', 'DBN')
        self.assertTrue(result['available'])
        self.assertEqual(result['sample_size'], 5)  # the outlier never counted
        self.assertLess(result['market_avg_rate'], 40000)
        self.assertLess(result['market_median_rate'], 40000)

    def test_normal_price_variance_is_kept(self):
        from core.services.lane_benchmark import compute_lane_benchmark

        amounts = [25000, 30000, 35000, 40000, 45000]  # 1.8x spread, well under 10x
        for i, amt in enumerate(amounts):
            company, cust, user = (
                (self.company_a, self.cust_a, self.user_a) if i % 2 == 0
                else (self.company_b, self.cust_b, self.user_b)
            )
            self._won(company, cust, user, amt)

        result = compute_lane_benchmark('CPT', 'DBN')
        self.assertTrue(result['available'])
        self.assertEqual(result['sample_size'], 5)
        self.assertAlmostEqual(result['market_avg_rate'], sum(amounts) / 5, places=2)

    def test_outlier_exclusion_can_legitimately_drop_below_k_anonymity(self):
        from core.services.lane_benchmark import compute_lane_benchmark

        for amt in (30000, 31000):
            self._won(self.company_a, self.cust_a, self.user_a, amt)
        for amt in (50_000_000, 60_000_000, 70_000_000):
            self._won(self.company_b, self.cust_b, self.user_b, amt)

        result = compute_lane_benchmark('CPT', 'DBN')
        # 2 real rows survive the cap -- below k_anonymity=5. Honest
        # unavailability, not a benchmark built from three bad rows.
        self.assertFalse(result['available'])

    def test_resolve_market_rate_uses_the_median_not_the_mean(self):
        for amt in (20000, 21000, 22000, 23000):
            self._won(self.company_a, self.cust_a, self.user_a, amt)
        # A legitimately pricier quote, well inside the sanity cap (< 10x),
        # that would still drag a mean noticeably off-centre.
        self._won(self.company_b, self.cust_b, self.user_b, 60000)

        rate, source = resolve_market_rate('CPT', 'DBN')
        self.assertEqual(source, 'platform')
        amounts = sorted([20000, 21000, 22000, 23000, 60000])
        mean = sum(amounts) / len(amounts)
        median = amounts[2]
        self.assertNotAlmostEqual(rate, mean, delta=1)
        self.assertAlmostEqual(rate, median, places=2)
