"""Tests for the AI price-analysis stack: optimizer cost basis, outcome
capture (the ML flywheel), snapshot-based training, market-rate resolution
guards, and tenant scoping of model stats."""

import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Quote, QuoteOutcome

User = get_user_model()


class IsolatedModelStorageMixin:
    """Per-test temp MEDIA_ROOT + a cleared model-resolution cache.

    WinProbabilityModel persists to the real filesystem (settings.MEDIA_ROOT),
    which Django's per-test DB transaction rollback does NOT clean up. Any
    test that exercises analyze_quote()/optimize_price()/
    resolve_prediction_context() and asserts heuristic-mode (no trained
    model) behaviour MUST use this mixin -- otherwise it silently depends on
    there being no REAL trained model file on this machine, which stops
    being true the moment anyone seeds real demo data (core.services.
    quote_training.retrain_win_model_for_scope writes to the real
    media/ml_models/ directory, same as production). Also clears the
    process-local _MODEL_CACHE so a model resolved during one test can't
    leak into another via the shared module-level cache.
    """

    def setUp(self):
        super().setUp()
        self._tmp_media = tempfile.mkdtemp(prefix='tw-win-model-test-')
        self._override = override_settings(MEDIA_ROOT=self._tmp_media)
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.addCleanup(lambda: shutil.rmtree(self._tmp_media, ignore_errors=True))
        from core.services.quote_ml import _MODEL_CACHE
        _MODEL_CACHE.clear()
        self.addCleanup(_MODEL_CACHE.clear)


def make_quote(company, customer, *, number, total=25000, origin='JHB',
               destination='CPT', status='SENT', outcome='pending',
               pickup_in_days=None, **extra):
    fields = dict(
        company=company, customer=customer, quote_number=number,
        pickup_location='Johannesburg', delivery_location='Cape Town',
        origin=origin, destination=destination,
        cargo_description='test cargo', weight=Decimal('20000'),
        base_rate=Decimal('5000'), fuel_surcharge=Decimal('9000'),
        toll_charges=Decimal('1500'), driver_allowance=Decimal('1000'),
        additional_charges=Decimal('500'),
        total_amount=Decimal(str(total)),
        valid_until=date.today() + timedelta(days=14),
        status=status, outcome=outcome,
    )
    if pickup_in_days is not None:
        fields['pickup_date'] = date.today() + timedelta(days=pickup_in_days)
    fields.update(extra)
    return Quote.objects.create(**fields)


class OptimizerCostBasisTests(IsolatedModelStorageMixin, TestCase):
    """A1: expected profit must be computed against direct cost, with the
    candidate band anchored on the market rate."""

    def test_optimum_anchored_on_market_not_forced_above_cost_plus_5pct(self):
        from core.services.margin_optimizer import optimize_price

        result = optimize_price(total_cost=10000, market_rate=25000)
        self.assertTrue(result['curve'])
        prices = [p['price'] for p in result['curve']]
        # Band spans 0.75x..1.35x market, floored at cost * 1.05.
        self.assertGreaterEqual(min(prices), 10000 * 1.05)
        self.assertGreaterEqual(min(prices), 25000 * 0.75 - 1)
        self.assertLessEqual(max(prices), 25000 * 1.35 + 1)
        # Optimum sits near/below market — far above the old cost*1.45 ceiling.
        self.assertGreater(result['optimal_price'], 14500)
        # margin_pct is markup over the supplied cost.
        expected_margin = (result['optimal_price'] - 10000) / 10000 * 100
        self.assertAlmostEqual(result['optimal_margin_pct'], expected_margin, delta=0.2)

    def test_cost_above_market_band_never_inverts(self):
        from core.services.margin_optimizer import optimize_price

        result = optimize_price(total_cost=30000, market_rate=25000)
        prices = [p['price'] for p in result['curve']]
        self.assertGreaterEqual(min(prices), 30000 * 1.05 - 1)
        self.assertLess(min(prices), max(prices))

    @mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=Exception('offline'))
    def test_analyze_quote_optimizes_over_direct_cost(self, _fp):
        """With a real market rate, the suggestion is no longer forced to be
        >= quote_total * 1.05 (the old wrong-cost-basis behaviour)."""
        from core.services.quote_analysis import analyze_quote

        result = analyze_quote({
            'quote_total': 25000,
            'direct_cost': 12000,
            'market_rate': 25000,  # client-supplied; no origin => used as-is
        })
        self.assertTrue(result['success'])
        self.assertLess(result['suggested_price'], 25000 * 1.05)
        opt = result['price_optimization']
        # Margin is relative to direct cost, not the quoted total.
        expected_margin = (opt['optimal_price'] - 12000) / 12000 * 100
        self.assertAlmostEqual(opt['optimal_margin_pct'], expected_margin, delta=0.2)

    @mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=Exception('offline'))
    def test_no_market_data_is_reported_honestly_not_fabricated(self, _fp):
        """With NO market data at all, the analysis must NOT invent a market rate
        from the user's own price (the old quote_total*1.25 anchor). It reports
        market_rate=None / source='none', recommends the company margin-target
        price (default 10% on price = cost/0.9) rather than the optimizer's
        synthetic ~30% markup, and the suggestion still doesn't track whatever
        the user typed."""
        from core.services.quote_analysis import analyze_quote

        base = {'direct_cost': 8000, 'market_rate': 0}
        low = analyze_quote({**base, 'quote_total': 10000})
        high = analyze_quote({**base, 'quote_total': 20000})
        # No fabricated market rate.
        self.assertIsNone(low['market_analysis']['market_rate'])
        self.assertEqual(low['market_analysis']['source'], 'none')
        self.assertIsNone(low['market_analysis']['your_vs_market_pct'])
        # Margin-target recommendation: cost / (1 - 0.10) with the default 10%.
        self.assertEqual(low['suggested_price'], round(8000 / 0.9, 2))
        self.assertEqual(low['price_optimization']['optimal_price'], round(8000 / 0.9, 2))
        self.assertIn('target margin', low['suggested_price_rationale'])
        # Same costs => same suggestion, regardless of what the user typed.
        self.assertEqual(low['suggested_price'], high['suggested_price'])


class AiPredictionContractTests(IsolatedModelStorageMixin, TestCase):
    """The 'ai_prediction' block is the ONLY place a caller should trust as a
    real trained-model result — never let heuristic-driven price_optimization
    masquerade as it, and never let a resolver failure break the manual flow."""

    @mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=Exception('offline'))
    def test_unavailable_by_default_but_manual_flow_still_works(self, _fp):
        from core.services.quote_analysis import analyze_quote

        result = analyze_quote({'quote_total': 25000, 'direct_cost': 12000, 'market_rate': 25000})
        self.assertTrue(result['success'])
        self.assertEqual(result['ai_prediction'], {'available': False, 'reason': 'insufficient_training_data'})
        # Manual/heuristic flow is completely unaffected.
        self.assertIsNotNone(result['price_optimization']['optimal_price'])
        self.assertIsNotNone(result['suggested_price'])

    @mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=Exception('offline'))
    def test_available_shape_when_a_model_resolves(self, _fp):
        from core.services.quote_analysis import analyze_quote
        from core.services.win_prediction import PredictionContext

        fake_ctx = PredictionContext(True, 'user', 72, lambda features: 0.6)
        with mock.patch('core.services.win_prediction.resolve_prediction_context', return_value=fake_ctx):
            result = analyze_quote({'quote_total': 25000, 'direct_cost': 12000, 'market_rate': 25000})

        ai = result['ai_prediction']
        self.assertTrue(ai['available'])
        self.assertEqual(ai['model_scope'], 'user')
        self.assertEqual(ai['training_samples'], 72)
        self.assertIn('recommended_price', ai)
        self.assertIn('win_probability', ai)
        self.assertIn('price_vs_market_pct', ai)

    @mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=Exception('offline'))
    def test_resolver_exception_degrades_gracefully(self, _fp):
        from core.services.quote_analysis import analyze_quote

        with mock.patch('core.services.win_prediction.resolve_prediction_context', side_effect=RuntimeError('boom')):
            result = analyze_quote({'quote_total': 25000, 'direct_cost': 12000, 'market_rate': 25000})
        # analyze_quote's own try/except around base_features/opt construction
        # must never let a resolver failure propagate into a 500.
        self.assertTrue(result['success'])
        self.assertFalse(result['ai_prediction']['available'])


class OptimizerConstraintTests(IsolatedModelStorageMixin, TestCase):
    """New business-constraint knobs on optimize_price(): clamp-then-relax,
    never a hard refusal, and byte-identical behaviour for legacy callers
    that don't pass them at all."""

    def test_legacy_callers_without_new_kwargs_are_unaffected(self):
        from core.services.margin_optimizer import optimize_price

        result = optimize_price(total_cost=10000, market_rate=25000)
        self.assertIn('constraints_applied', result)
        self.assertFalse(result['constraints_relaxed'])
        self.assertEqual(result['constraint_notes'], [])
        # Default min_win_probability=0 means every candidate is "feasible",
        # so the constrained and unconstrained argmax coincide.
        unconstrained = optimize_price(total_cost=10000, market_rate=25000)
        self.assertEqual(result['optimal_price'], unconstrained['optimal_price'])

    def test_min_win_probability_clamps_the_search(self):
        from core.services.margin_optimizer import optimize_price

        # The heuristic's max achievable win-prob within this band (at the
        # cheapest swept price, ratio=0.75) is ~0.85 -- 0.7 is comfortably
        # reachable without hitting the "infeasible floor" relax path (that's
        # covered separately below).
        loose = optimize_price(total_cost=10000, market_rate=25000, min_win_probability=0.0)
        tight = optimize_price(total_cost=10000, market_rate=25000, min_win_probability=0.7)
        self.assertGreaterEqual(tight['win_probability_at_optimal'], 0.7 - 1e-6)
        self.assertLessEqual(tight['optimal_price'], loose['optimal_price'])
        self.assertFalse(tight['constraints_relaxed'])

    def test_infeasible_win_probability_floor_relaxes_but_still_returns_a_price(self):
        from core.services.margin_optimizer import optimize_price

        result = optimize_price(total_cost=10000, market_rate=25000, min_win_probability=0.999)
        self.assertTrue(result['constraints_relaxed'])
        self.assertTrue(result['constraint_notes'])
        self.assertIsNotNone(result['optimal_price'])
        self.assertGreater(result['optimal_price'], 0)

    def test_max_market_deviation_caps_the_band(self):
        from core.services.margin_optimizer import optimize_price

        result = optimize_price(total_cost=10000, market_rate=25000, max_market_deviation=0.10)
        prices = [p['price'] for p in result['curve']]
        self.assertLessEqual(max(prices), 25000 * 1.10 + 1)
        self.assertEqual(result['constraints_applied']['max_market_deviation_pct'], 10.0)

    def test_base_features_price_ratio_is_overwritten_per_candidate(self):
        """base_features is used as-is except price_ratio, which must vary
        across the sweep regardless of what the caller seeded it with."""
        from core.services.margin_optimizer import optimize_price

        seen = []

        def spy(features):
            seen.append(features['price_ratio'])
            return 0.5

        optimize_price(
            total_cost=10000, market_rate=25000, predict_proba_fn=spy,
            base_features={'price_ratio': 999.0, 'client_tier': 2},
        )
        self.assertGreater(len(seen), 1)
        self.assertNotIn(999.0, seen)
        self.assertEqual(len(set(seen)), len(seen))  # every candidate got a distinct ratio

    @mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=Exception('offline'))
    def test_no_market_data_uses_company_margin_target(self, _fp):
        """The no-data recommendation follows the COMPANY's configured target."""
        from core.models import Company
        from core.services.quote_analysis import analyze_quote

        company = Company.objects.create(company_name='Target Co', margin_target_pct=15)
        out = analyze_quote({'direct_cost': 8500, 'quote_total': 8500, 'market_rate': 0},
                            company=company)
        self.assertEqual(out['suggested_price'], round(8500 / 0.85, 2))
        self.assertIn('15%', out['suggested_price_rationale'])

    @mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=Exception('offline'))
    def test_real_market_rate_keeps_profit_max_path(self, _fp):
        """With a real market rate the profit-max optimizer stays in charge —
        the margin-target override applies ONLY when there's no data."""
        from core.services.quote_analysis import analyze_quote

        out = analyze_quote({'direct_cost': 8000, 'quote_total': 10000, 'market_rate': 12000})
        self.assertNotIn('target margin', out['suggested_price_rationale'] or '')
        # The optimizer's own optimum, not cost/0.9.
        self.assertNotEqual(out['suggested_price'], round(8000 / 0.9, 2))

    def test_at_risk_price_increase_suggestion_math(self):
        """Target price for a revenue margin t is cost/(1-t): cost=10000,
        price=10200, t=10% -> increase ≈ R911 (old formula returned negative)."""
        from core.services.quote_analysis import assess_revenue_guard

        result = assess_revenue_guard(total_cost=10000, quote_price=10200)
        self.assertEqual(result['risk_level'], 'AT_RISK')
        matches = [s for s in result['suggestions'] if 'Increase price by' in s]
        self.assertTrue(matches, 'increase suggestion missing')
        self.assertIn('R911', matches[0])


class OutcomeCaptureTests(TestCase):
    """B1/B2: every accept/decline path records exactly one QuoteOutcome with
    point-in-time feature snapshots."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Cap Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Shipper Ltd', email='s@x.test',
            phone='', address='', city='', state='', zip_code='',
        )

    def test_record_quote_outcome_snapshots_features(self):
        from core.services.quote_outcome_capture import record_quote_outcome

        # Two decided historical quotes -> acceptance rate 0.5 BEFORE this one.
        make_quote(self.company, self.customer, number='Q-H1', outcome='accepted')
        make_quote(self.company, self.customer, number='Q-H2', outcome='rejected')
        quote = make_quote(self.company, self.customer, number='Q-NEW',
                           pickup_in_days=5)

        record = record_quote_outcome(quote, 'accepted')
        self.assertIsNotNone(record)
        quote.refresh_from_db()
        self.assertEqual(quote.outcome, 'accepted')
        self.assertIsNotNone(quote.accepted_at)
        self.assertEqual(record.company_id, self.company.id)
        self.assertEqual(record.days_until_departure, 5)
        self.assertEqual(record.quote_month, quote.created_at.month)
        self.assertEqual(record.quote_dow, quote.created_at.weekday())
        self.assertEqual(record.historical_acceptance_rate, Decimal('0.5'))

    def test_repeat_outcomes_update_not_duplicate(self):
        from core.services.quote_outcome_capture import record_quote_outcome

        quote = make_quote(self.company, self.customer, number='Q-DUP')
        record_quote_outcome(quote, 'accepted')
        # Deliberate operator correction (allow_flip) updates the single row.
        record_quote_outcome(quote, 'rejected', rejection_reason='changed mind',
                             allow_flip=True)

        rows = QuoteOutcome.objects.filter(quote=quote)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().outcome, 'rejected')
        quote.refresh_from_db()
        self.assertEqual(quote.outcome, 'rejected')

    def test_same_outcome_rerecord_keeps_original_snapshot(self):
        """ACCEPTED -> IT re-records must not drift the point-in-time snapshot."""
        from core.services.quote_outcome_capture import record_quote_outcome

        quote = make_quote(self.company, self.customer, number='Q-IDEM',
                           pickup_in_days=5)
        first = record_quote_outcome(quote, 'accepted')
        quote.refresh_from_db()
        original_accepted_at = quote.accepted_at

        again = record_quote_outcome(quote, 'accepted')
        self.assertEqual(again.id, first.id)
        self.assertEqual(again.updated_at, first.updated_at)  # untouched row
        quote.refresh_from_db()
        self.assertEqual(quote.accepted_at, original_accepted_at)

    def test_label_flip_refused_without_allow_flip(self):
        """A stale decline path can never overwrite a genuine accepted label."""
        from core.services.quote_outcome_capture import record_quote_outcome

        quote = make_quote(self.company, self.customer, number='Q-FLIP')
        record_quote_outcome(quote, 'accepted')
        result = record_quote_outcome(quote, 'rejected')

        self.assertEqual(result.outcome, 'accepted')  # existing row returned
        self.assertEqual(QuoteOutcome.objects.get(quote=quote).outcome, 'accepted')

    def test_public_accept_link_records_outcome(self):
        quote = make_quote(self.company, self.customer, number='Q-PUB')
        client = APIClient()
        resp = client.post(
            f'/api/v1/quotes/public/{quote.id}/{quote.token}/respond/',
            {'action': 'accept'}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'ACCEPTED')
        self.assertEqual(quote.outcome, 'accepted')
        self.assertEqual(QuoteOutcome.objects.filter(quote=quote).count(), 1)

    def test_public_decline_link_records_outcome(self):
        quote = make_quote(self.company, self.customer, number='Q-PUBD')
        client = APIClient()
        resp = client.post(
            f'/api/v1/quotes/public/{quote.id}/{quote.token}/respond/',
            {'action': 'decline'}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        row = QuoteOutcome.objects.get(quote=quote)
        self.assertEqual(row.outcome, 'rejected')


class TrainingMatrixSnapshotTests(TestCase):
    """B3: training rows prefer the stored feature_snapshot (v2) over live
    reconstruction, and per-user scope never mixes users. Leakage-cutoff
    correctness lives in test_quote_features.py — this class is about the
    matrix builder's own row-selection/scoping behaviour."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Train Co')
        self.customer = Customer.objects.create(
            company=self.company, name='T Ltd', email='t@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(username='train-user', password='x', company=self.company)

    def test_matrix_prefers_snapshot_fields(self):
        from core.services.quote_training import build_win_training_matrix_for_scope
        from core.services import quote_features

        q1 = make_quote(self.company, self.customer, number='Q-T1', created_by=self.user)
        snap1 = {
            'feature_version': quote_features.FEATURE_VERSION,
            'features': {name: 0.11 for name in quote_features.CORE_FEATURES},
        }
        QuoteOutcome.objects.create(
            quote=q1, company=self.company, created_by=self.user, outcome='accepted',
            final_price=Decimal('20000'), feature_snapshot=snap1,
        )
        q2 = make_quote(self.company, self.customer, number='Q-T2', created_by=self.user)
        snap2 = {
            'feature_version': quote_features.FEATURE_VERSION,
            'features': {name: 0.22 for name in quote_features.CORE_FEATURES},
        }
        QuoteOutcome.objects.create(
            quote=q2, company=self.company, created_by=self.user, outcome='rejected',
            final_price=Decimal('30000'), feature_snapshot=snap2,
        )

        X, y, n, names = build_win_training_matrix_for_scope('global')
        self.assertEqual(n, 2)
        self.assertEqual(names, quote_features.CORE_FEATURES)
        rows = {tuple(row) for row in X.tolist()}
        self.assertIn(tuple(0.11 for _ in names), rows)
        self.assertIn(tuple(0.22 for _ in names), rows)
        self.assertEqual(sorted(y.tolist()), [0, 1])

    def test_legacy_row_without_snapshot_reconstructs_live(self):
        from core.services.quote_training import build_win_training_matrix_for_scope

        q = make_quote(self.company, self.customer, number='Q-LEGACY', created_by=self.user)
        # No feature_snapshot -- simulates a pre-v2 row (empty dict default).
        QuoteOutcome.objects.create(
            quote=q, company=self.company, created_by=self.user, outcome='accepted',
            final_price=Decimal('20000'),
        )
        X, y, n, names = build_win_training_matrix_for_scope('global')
        self.assertEqual(n, 1)
        self.assertEqual(len(X[0]), len(names))

    def test_user_scope_never_mixes_users(self):
        from core.services.quote_training import build_win_training_matrix_for_scope

        other_user = User.objects.create_user(username='other-user', password='x', company=self.company)
        q_mine = make_quote(self.company, self.customer, number='Q-MINE', created_by=self.user)
        QuoteOutcome.objects.create(quote=q_mine, company=self.company, created_by=self.user,
                                    outcome='accepted', final_price=Decimal('20000'))
        q_other = make_quote(self.company, self.customer, number='Q-OTHER', created_by=other_user)
        QuoteOutcome.objects.create(quote=q_other, company=self.company, created_by=other_user,
                                    outcome='rejected', final_price=Decimal('20000'))

        _, _, n_mine, _ = build_win_training_matrix_for_scope('user', user_id=self.user.id)
        self.assertEqual(n_mine, 1)
        _, _, n_other, _ = build_win_training_matrix_for_scope('user', user_id=other_user.id)
        self.assertEqual(n_other, 1)
        _, _, n_global, _ = build_win_training_matrix_for_scope('global')
        self.assertEqual(n_global, 2)

    def test_user_scope_without_user_id_returns_empty(self):
        from core.services.quote_training import build_win_training_matrix_for_scope

        X, y, n, names = build_win_training_matrix_for_scope('user', user_id=None)
        self.assertEqual(n, 0)
        self.assertEqual(names, [])


class MarketRateResolutionTests(TestCase):
    """A4/A5: company fallback guards + city-code canonicalization."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Lane Co')
        self.customer = Customer.objects.create(
            company=self.company, name='L Ltd', email='l@x.test',
            phone='', address='', city='', state='', zip_code='',
        )

    def _make_won_lane_quotes(self, n=3, total=20000, origin='PTA', destination='BFN'):
        quotes = []
        for i in range(n):
            quotes.append(make_quote(
                self.company, self.customer, number=f'Q-L{origin}{i}',
                total=total, origin=origin, destination=destination,
                status='ACCEPTED',
            ))
        return quotes

    def test_company_none_skips_own_quotes_layer(self):
        from core.services.lane_benchmark import resolve_market_rate

        # 3 won quotes from ONE company: platform layer fails k-anonymity, and
        # with company=None the own-quotes layer must be skipped entirely
        # (PTA->BFN has no hardcoded estimate) -> no rate at all.
        self._make_won_lane_quotes()
        rate, source = resolve_market_rate('PTA', 'BFN', company=None)
        self.assertIsNone(rate)
        self.assertEqual(source, 'none')

        # The owning company still gets its own average.
        rate, source = resolve_market_rate('PTA', 'BFN', company=self.company)
        self.assertEqual(source, 'company')
        self.assertAlmostEqual(rate, 20000.0, places=2)

    def test_company_fallback_ignores_stale_quotes(self):
        from core.services.lane_benchmark import resolve_market_rate

        quotes = self._make_won_lane_quotes()
        Quote.objects.filter(id__in=[q.id for q in quotes]).update(
            created_at=timezone.now() - timedelta(days=400)
        )
        rate, source = resolve_market_rate('PTA', 'BFN', company=self.company)
        self.assertIsNone(rate)
        self.assertEqual(source, 'none')

    def test_durban_codes_canonicalize_to_estimate(self):
        from core.services.lane_benchmark import resolve_market_rate

        # 'DUR' (the code the frontend historically stored) must hit the
        # JHB->DBN estimate.
        rate, source = resolve_market_rate('JHB', 'DUR', 'interlink', company=None)
        self.assertEqual(source, 'estimate')
        self.assertEqual(rate, 17000.0)

    def test_company_fallback_matches_stored_dur_quotes(self):
        from core.services.lane_benchmark import resolve_market_rate

        self._make_won_lane_quotes(origin='JHB', destination='DUR', total=16000)
        rate, source = resolve_market_rate('JHB', 'DBN', company=self.company)
        self.assertEqual(source, 'company')
        self.assertAlmostEqual(rate, 16000.0, places=2)


class ModelStatsScopingTests(TestCase):
    """A4: model-stats counts are tenant-scoped."""

    def setUp(self):
        self.company_a = Company.objects.create(company_name='A Co')
        self.company_b = Company.objects.create(company_name='B Co')
        self.cust_a = Customer.objects.create(
            company=self.company_a, name='A Ltd', email='a@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.cust_b = Customer.objects.create(
            company=self.company_b, name='B Ltd', email='b@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        for i in range(2):
            q = make_quote(self.company_a, self.cust_a, number=f'Q-A{i}')
            QuoteOutcome.objects.create(quote=q, company=self.company_a,
                                        outcome='accepted', final_price=Decimal('20000'))
        for i in range(5):
            q = make_quote(self.company_b, self.cust_b, number=f'Q-B{i}')
            QuoteOutcome.objects.create(quote=q, company=self.company_b,
                                        outcome='rejected', final_price=Decimal('20000'))

    def test_counts_scoped_to_requesting_company(self):
        user = User.objects.create_user(
            username='a-user', password='x', company=self.company_a,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get('/api/v1/quotes/model-stats/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['real_quotes_count'], 2)
        # win_model is now two-tier: {'user': {...}, 'global': {...}}.
        # 'global' is genuinely platform-wide (2 + 5 across both companies);
        # 'user' is 0 here since setUp's outcomes have no created_by matching
        # this user (record_quote_outcome sets it, direct .create() doesn't).
        win = data.get('win_model')
        if win:
            self.assertEqual(win['global']['outcomes_collected'], 7)
            self.assertEqual(win['user']['outcomes_collected'], 0)


class ModelProgressBlockerTests(IsolatedModelStorageMixin, TestCase):
    """model_progress must name the gate that's actually in the way.

    Reporting only outcomes_collected/outcomes_needed let the UI contradict
    itself: production showed "73/40 platform" with the bar full next to "AI
    pricing isn't ready yet", because all 73 outcomes were 'accepted' and
    training needs both classes. The count had passed; nothing said what
    hadn't. The mixin matters here — `ready` reads the real filesystem.
    """

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(company_name='Blocker Co')
        self.customer = Customer.objects.create(
            company=self.company, name='B Ltd', email='blocker@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(
            username='blocker-user', password='x', company=self.company)
        self._n = 0

    def _outcomes(self, label, count):
        for _ in range(count):
            self._n += 1
            q = make_quote(self.company, self.customer, number=f'BLK-{self._n:03d}')
            QuoteOutcome.objects.create(
                quote=q, company=self.company, created_by=self.user,
                outcome=label, final_price=Decimal('20000'),
            )

    def _progress(self):
        from core.services.win_prediction import model_progress
        return model_progress(self.user, self.company)

    def test_all_accepted_past_the_floor_reports_needs_lost_quotes(self):
        # Exactly the production state, and the one the old payload could not
        # express: qualifies is True and the model still cannot train.
        self._outcomes('accepted', 41)
        tier = self._progress()['global']

        self.assertTrue(tier['qualifies'])
        self.assertEqual(tier['blocker'], 'needs_lost_quotes')
        self.assertEqual((tier['accepted'], tier['rejected']), (41, 0))
        self.assertFalse(tier['ready'])

    def test_all_rejected_past_the_floor_reports_needs_won_quotes(self):
        self._outcomes('rejected', 41)
        self.assertEqual(self._progress()['global']['blocker'], 'needs_won_quotes')

    def test_below_the_floor_reports_insufficient_data(self):
        self._outcomes('accepted', 3)
        self._outcomes('rejected', 2)
        tier = self._progress()['global']

        self.assertFalse(tier['qualifies'])
        self.assertEqual(tier['blocker'], 'insufficient_data')
        self.assertEqual(tier['outcomes_collected'], 5)

    def test_both_classes_past_the_floor_but_no_artifact_awaits_retrain(self):
        self._outcomes('accepted', 25)
        self._outcomes('rejected', 20)
        tier = self._progress()['global']

        self.assertEqual(tier['blocker'], 'awaiting_retrain')
        self.assertFalse(tier['ready'])

    def test_awaiting_retrain_surfaces_the_last_rejection_reason(self):
        from core.models import MLModelVersion
        self._outcomes('accepted', 25)
        self._outcomes('rejected', 20)
        MLModelVersion.objects.create(
            scope='global', status='rejected',
            rejection_reason='roc_auc regressed 0.780 -> 0.700 vs active model',
        )
        tier = self._progress()['global']

        self.assertEqual(tier['blocker'], 'awaiting_retrain')
        self.assertIn('roc_auc regressed', tier['blocker_detail'])

    def test_user_tier_is_scoped_and_never_touches_a_none_model_dir(self):
        self._outcomes('accepted', 41)
        progress = self._progress()

        self.assertEqual(progress['user']['accepted'], 41)
        self.assertEqual(progress['user']['blocker'], 'needs_lost_quotes')
        # Anonymous callers get an empty user tier, not a users/None lookup.
        from core.services.win_prediction import model_progress
        anon = model_progress(None, self.company)
        self.assertEqual(anon['user']['outcomes_collected'], 0)
        self.assertFalse(anon['user']['ready'])
        self.assertEqual(anon['user']['blocker'], 'insufficient_data')


class LegacyEndpointPredictProbaRegressionTests(IsolatedModelStorageMixin, TestCase):
    """AIQuoteSuggestionView and QuoteWinProbabilityView both call
    predict_proba(features: dict) internally now (the interface changed from
    positional kwargs) -- these are unreferenced from the live quote-creation
    UI, but must not 500 for anyone still hitting them directly."""

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(company_name='Legacy Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Legacy Ltd', email='legacy@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(username='legacy-user', password='x', company=self.company)
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=self.user)

    def test_suggest_endpoint_does_not_500(self):
        resp = self.client_api.post('/api/v1/quotes/suggest/', {
            'distance_km': 1400, 'fuel_cost': 5000, 'toll_cost': 1200,
            'driver_cost': 800, 'actual_cost': 10000,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data.get('success'))
        self.assertIn('suggested_price', data)

    def test_win_probability_endpoint_does_not_500(self):
        resp = self.client_api.post('/api/v1/quotes/win-probability/', {
            'price': 20000, 'distance': 1400, 'client_id': self.customer.id,
            'days_until_departure': 5,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data.get('success'))
        self.assertIn('win_probability', data)
        self.assertTrue(0.0 <= data['win_probability'] <= 1.0)


class AnalyzeClientFeatureDerivationTests(TestCase):
    """A2: tier / acceptance rate / urgency derived server-side."""

    def test_derive_client_features(self):
        from core.views_ai_quote import AIQuoteAnalyzeView

        company = Company.objects.create(company_name='D Co')
        customer = Customer.objects.create(
            company=company, name='D Ltd', email='d@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        for i in range(3):
            make_quote(company, customer, number=f'Q-D{i}', outcome='accepted')
        make_quote(company, customer, number='Q-D-R', outcome='rejected')

        tier, rate = AIQuoteAnalyzeView._derive_client_features(company, customer.id)
        self.assertEqual(tier, 'regular')  # 3 accepted
        self.assertAlmostEqual(rate, 0.75)  # 3 of 4 decided


class DegenerateWinCurveGuardTests(TestCase):
    """The optimizer must not price against a win curve that doesn't fall.

    optimize_price maximises (price - cost) * P(win). If P(win) is flat or
    rising in price there is no interior peak, so the search returns the top
    of the market band — which is how a model with a positive price_ratio
    coefficient produced a recommendation 62% above the operator's own quote,
    labelled 96% likely to win. The guard discards such a curve and prices on
    the heuristic instead.
    """

    COST = 23000.0
    MARKET = 34700.0

    def _optimize(self, win_fn):
        from core.services.margin_optimizer import optimize_price
        return optimize_price(
            total_cost=self.COST, market_rate=self.MARKET, predict_proba_fn=win_fn,
        )

    def test_flat_curve_falls_back_to_the_heuristic(self):
        result = self._optimize(lambda features: 0.96)

        self.assertLess(result['optimal_price'], self.MARKET * 1.35)
        # The heuristic's curve genuinely declines, so a real optimum exists.
        wins = [p['win_probability'] for p in result['curve']]
        self.assertGreater(wins[0] - wins[-1], 0.02)
        self.assertNotEqual(result['win_probability_at_optimal'], 0.96)

    def test_inverted_curve_falls_back_too(self):
        # Win probability RISING with price — the exact production failure.
        result = self._optimize(lambda f: min(0.99, 0.5 + f.get('price_ratio', 1.0) * 0.3))

        wins = [p['win_probability'] for p in result['curve']]
        self.assertGreater(wins[0] - wins[-1], 0.02, 'curve should have been replaced')

    def test_the_substitution_is_explained_not_silent(self):
        result = self._optimize(lambda features: 0.96)
        notes = ' '.join(result.get('constraints_applied', {}).get('notes', [])
                         or result.get('constraint_notes', []) or [])
        self.assertIn('same win probability at every price', notes)

    def test_a_healthy_declining_curve_is_left_alone(self):
        # Must not "guard" against a model that is working: the optimum here
        # is the caller's curve, untouched.
        def declining(features):
            return max(0.02, min(0.98, 1.6 - features.get('price_ratio', 1.0)))

        result = self._optimize(declining)
        notes = ' '.join(result.get('constraints_applied', {}).get('notes', [])
                         or result.get('constraint_notes', []) or [])
        self.assertNotIn('same win probability', notes)
        for point in result['curve']:
            expected = max(0.02, min(0.98, 1.6 - point['price'] / self.MARKET))
            self.assertAlmostEqual(point['win_probability'], round(expected, 4), places=3)
