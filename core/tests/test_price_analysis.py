"""Tests for the AI price-analysis stack: optimizer cost basis, outcome
capture (the ML flywheel), snapshot-based training, market-rate resolution
guards, and tenant scoping of model stats."""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Quote, QuoteOutcome

User = get_user_model()


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


class OptimizerCostBasisTests(TestCase):
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
    """B3: training rows come from the stored snapshots, not reconstruction."""

    def setUp(self):
        self.company = Company.objects.create(company_name='Train Co')
        self.customer = Customer.objects.create(
            company=self.company, name='T Ltd', email='t@x.test',
            phone='', address='', city='', state='', zip_code='',
        )

    def test_matrix_prefers_snapshot_fields(self):
        from core.services.quote_training import build_win_training_matrix

        q1 = make_quote(self.company, self.customer, number='Q-T1')
        QuoteOutcome.objects.create(
            quote=q1, company=self.company, outcome='accepted',
            final_price=Decimal('20000'), client_tier='regular',
            origin='JHB', destination='CPT',
            price_ratio=Decimal('0.9'), days_until_departure=4,
            quote_month=2, quote_dow=3,
            historical_acceptance_rate=Decimal('0.75'),
        )
        q2 = make_quote(self.company, self.customer, number='Q-T2')
        QuoteOutcome.objects.create(
            quote=q2, company=self.company, outcome='rejected',
            final_price=Decimal('30000'), client_tier='new',
            origin='JHB', destination='CPT',
            price_ratio=Decimal('1.2'), days_until_departure=12,
            quote_month=8, quote_dow=0,
            historical_acceptance_rate=Decimal('0.25'),
        )

        X, y, n = build_win_training_matrix()
        self.assertEqual(n, 2)
        rows = {tuple(row) for row in X.tolist()}
        # [price_ratio, tier, days, hist, month, dow, popularity]
        self.assertIn((0.9, 1.0, 4.0, 0.75, 2.0, 3.0, 1.0), rows)
        self.assertIn((1.2, 0.0, 12.0, 0.25, 8.0, 0.0, 1.0), rows)
        self.assertEqual(sorted(y.tolist()), [0, 1])


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
        if data.get('win_model'):
            self.assertEqual(data['win_model']['outcomes_collected'], 2)


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
