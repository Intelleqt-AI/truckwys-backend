"""Tests for core.services.quote_features — the leakage-safe feature
engineering module shared by training, outcome-snapshot, and live serving."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import Company, Customer, Quote, QuoteOutcome
from core.services import quote_features
from core.tests.test_price_analysis import make_quote

User = get_user_model()


class ComputeFeaturesShapeTests(TestCase):
    def test_returns_every_core_and_full_feature(self):
        f = quote_features.compute_features(
            company=None, total_amount=10000, base_rate=6000, fuel_surcharge=1000,
            toll_charges=500, driver_allowance=300, distance_km=250,
        )
        for name in quote_features.CORE_FEATURES:
            self.assertIn(name, f)
        for name in quote_features.FULL_FEATURES:
            self.assertIn(name, f)

    def test_quoted_margin_pct_includes_base_rate_as_cost(self):
        # 10000 total, cost = 6000(base) + 1000 + 500 + 300 = 7800 -> margin 22%.
        # Deliberately NOT the narrower quote_outcome_capture direct_cost
        # definition (which excludes base_rate) -- see the comment in
        # compute_features() for why.
        f = quote_features.compute_features(
            company=None, total_amount=10000, base_rate=6000, fuel_surcharge=1000,
            toll_charges=500, driver_allowance=300,
        )
        self.assertAlmostEqual(f['quoted_margin_pct'], 22.0, places=3)

    def test_vectorize_orders_and_defaults_missing_to_zero(self):
        f = {'a': 1.0, 'b': 2.0}
        self.assertEqual(quote_features.vectorize(f, ['b', 'a', 'c']), [2.0, 1.0, 0.0])

    def test_feature_tier_switches_on_cv_threshold(self):
        from django.test import override_settings
        with override_settings(WIN_MODEL_CV_THRESHOLD=100):
            self.assertEqual(quote_features.feature_tier_for(50), quote_features.CORE_FEATURES)
            self.assertEqual(quote_features.feature_tier_for(150), quote_features.FULL_FEATURES)

    def test_vehicle_type_bucket_known_and_unknown(self):
        self.assertEqual(quote_features._vehicle_type_bucket('34T Interlink'), 'interlink')
        self.assertEqual(quote_features._vehicle_type_bucket('Reefer Truck'), 'refrigerated')
        self.assertEqual(quote_features._vehicle_type_bucket('Something Bespoke'), 'other')

    def test_cyclical_month_wraps_december_to_january(self):
        dec_sin, dec_cos = quote_features._cyclical(12, 12)
        jan_sin, jan_cos = quote_features._cyclical(1, 12)
        # December and January should sit close together in cyclical space --
        # unlike raw month numbers 12 vs 1, which a linear model sees as 11 apart.
        dist = ((dec_sin - jan_sin) ** 2 + (dec_cos - jan_cos) ** 2) ** 0.5
        self.assertLess(dist, 0.6)


class LeakageSafetyTests(TestCase):
    """The core correctness rule: every historical aggregate must use
    created_at__lt=as_of, not just leave-one-out self-exclusion — a LATER
    quote's outcome must never leak into an EARLIER quote's reconstructed
    training features."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Leak Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Leak Ltd', email='leak@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(username='leak-user', password='x', company=self.company)

    def test_customer_signals_ignore_outcomes_after_as_of(self):
        early = make_quote(self.company, self.customer, number='Q-EARLY', created_by=self.user)
        Quote.objects.filter(id=early.id).update(created_at=timezone.now() - timedelta(days=30))
        early.refresh_from_db()

        # A LATER quote for the same customer gets rejected -- must not
        # affect the earlier quote's reconstructed historical signal.
        later = make_quote(self.company, self.customer, number='Q-LATER', created_by=self.user, outcome='rejected')

        tier, rate, volume, rel_days = quote_features.customer_signals(
            self.company, self.customer.id, as_of=early.created_at, exclude_quote_id=early.id,
        )
        self.assertEqual(volume, 0)
        self.assertAlmostEqual(rate, 0.5)  # cold start, not poisoned by the future rejection
        self.assertEqual(tier, 0)

    def test_user_signals_ignore_outcomes_after_as_of(self):
        early = make_quote(self.company, self.customer, number='Q-EARLY-U', created_by=self.user)
        Quote.objects.filter(id=early.id).update(created_at=timezone.now() - timedelta(days=30))
        early.refresh_from_db()

        make_quote(self.company, self.customer, number='Q-LATER-U', created_by=self.user, outcome='accepted')

        volume, rate, avg_ratio = quote_features.user_signals(
            self.user.id, as_of=early.created_at, exclude_quote_id=early.id,
        )
        self.assertEqual(volume, 0)
        self.assertAlmostEqual(rate, 0.5)

    def test_compute_features_for_quote_uses_quote_own_created_at_as_cutoff(self):
        """End-to-end: reconstructing an old quote's features must not see a
        newer quote's outcome, even though both exist in the DB right now."""
        old = make_quote(self.company, self.customer, number='Q-OLD', created_by=self.user,
                         total=20000, outcome='accepted')
        QuoteOutcome.objects.create(quote=old, company=self.company, created_by=self.user,
                                    outcome='accepted', final_price=Decimal('20000'))
        Quote.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(days=60))
        old.refresh_from_db()

        newer = make_quote(self.company, self.customer, number='Q-NEWER', created_by=self.user,
                           total=20000, outcome='rejected')
        QuoteOutcome.objects.create(quote=newer, company=self.company, created_by=self.user,
                                    outcome='rejected', final_price=Decimal('20000'))

        feats = quote_features.compute_features_for_quote(old, as_of=old.created_at)
        self.assertAlmostEqual(feats['historical_acceptance_rate'], 0.5)
        self.assertEqual(feats['user_quote_volume_prior'], 0)

    def test_lane_historical_acceptance_rate_respects_cutoff(self):
        early = make_quote(self.company, self.customer, number='Q-LANE-EARLY', created_by=self.user)
        Quote.objects.filter(id=early.id).update(created_at=timezone.now() - timedelta(days=30))
        early.refresh_from_db()

        make_quote(self.company, self.customer, number='Q-LANE-LATER', created_by=self.user, outcome='rejected')

        rate = quote_features.lane_historical_acceptance_rate(
            self.company, 'JHB', 'CPT', as_of=early.created_at, exclude_quote_id=early.id,
        )
        self.assertAlmostEqual(rate, 0.5)
