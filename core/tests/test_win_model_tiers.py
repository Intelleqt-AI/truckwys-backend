"""Tests for the two-tier (per-user + global) win-probability training and
resolution pipeline: threshold gating, class-balance gating, per-user
isolation, and the user-then-global-then-unavailable resolution order.

Uses IsolatedModelStorageMixin (see test_price_analysis.py) so a trained
model artifact never leaks between tests, or into/from a real trained model
that happens to exist on this machine's real media/ml_models/ (e.g. from
seeding real demo data) -- a real gap this exact failure mode caught during
development: a test saw an "active" model whose backing DB row had already
been rolled away by a DIFFERENT test's transaction.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.models import Company, Customer, MLModelVersion, Quote, QuoteOutcome
from core.services import quote_training
from core.services.quote_ml import WIN_ML_AVAILABLE, WinProbabilityModel
from core.services.win_prediction import resolve_prediction_context
from core.tests.test_price_analysis import IsolatedModelStorageMixin, make_quote

User = get_user_model()


def make_outcomes(company, customer, user, n, *, accepted_ratio=0.5, prefix='Q'):
    """Directly creates n Quote+QuoteOutcome pairs (bypassing
    record_quote_outcome's Celery scheduling, which isn't the point of these
    tests) with alternating accepted/rejected labels.

    Prices vary with the label — won quotes cheaper, lost quotes dearer, with
    deliberate overlap between the two bands so the classes aren't perfectly
    separable. Every quote used to be priced at a flat 20000, which made
    price_ratio a single constant across the whole training set (verified:
    0.5141 on all 40 rows). Anything asserting "this tier trains" was
    therefore asserting it on data with no price signal at all — and once
    quote_training gained its price-sensitivity gate, such a model is
    correctly refused. Real outcomes carry price variation; these now do too.
    """
    n_accepted = round(n * accepted_ratio)
    for i in range(n):
        accepted = i < n_accepted
        outcome = 'accepted' if accepted else 'rejected'
        # 16k-22k won, 23k-29k lost, walked deterministically for repeatability.
        total = (16000 + (i % 7) * 1000) if accepted else (23000 + (i % 7) * 1000)
        q = make_quote(company, customer, number=f'{prefix}-{i}', created_by=user, total=total)
        QuoteOutcome.objects.create(
            quote=q, company=company, created_by=user, outcome=outcome,
            final_price=Decimal(str(total)),
        )


class _RequiresSklearnMixin:
    """On top of IsolatedModelStorageMixin's isolation, these tests fit real
    sklearn models -- skip cleanly if the ML stack isn't installed rather
    than erroring on import."""

    def setUp(self):
        super().setUp()
        if not WIN_ML_AVAILABLE:
            self.skipTest('sklearn/joblib not installed in this environment')


class TrainingThresholdGateTests(_RequiresSklearnMixin, IsolatedModelStorageMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(company_name='Gate Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Gate Ltd', email='gate@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(username='gate-user', password='x', company=self.company)

    def test_39_outcomes_below_threshold_refuses(self):
        make_outcomes(self.company, self.customer, self.user, 39)
        result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)
        self.assertFalse(result['trained'])
        self.assertIn('insufficient data', result['reason'])
        self.assertEqual(result['samples'], 39)

    def test_40_mixed_outcomes_trains_and_activates(self):
        make_outcomes(self.company, self.customer, self.user, 40)
        result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)
        self.assertTrue(result['trained'], result)
        self.assertEqual(result['samples'], 40)

        version = MLModelVersion.objects.filter(scope='user', user=self.user, status='active').first()
        self.assertIsNotNone(version)
        self.assertEqual(version.training_sample_count, 40)
        self.assertEqual(version.accepted_count, 20)
        self.assertEqual(version.rejected_count, 20)

        model = WinProbabilityModel(scope='user', user_id=self.user.id)
        self.assertTrue(model.is_trained())

    def test_only_accepted_outcomes_refuses_even_above_threshold(self):
        make_outcomes(self.company, self.customer, self.user, 45, accepted_ratio=1.0)
        result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)
        self.assertFalse(result['trained'])
        self.assertEqual(result['reason'], 'only one outcome class present')

    def test_only_rejected_outcomes_refuses_even_above_threshold(self):
        make_outcomes(self.company, self.customer, self.user, 45, accepted_ratio=0.0)
        result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)
        self.assertFalse(result['trained'])
        self.assertEqual(result['reason'], 'only one outcome class present')


class UserIsolationAndFallbackTests(_RequiresSklearnMixin, IsolatedModelStorageMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(company_name='Iso Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Iso Ltd', email='iso@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user_a = User.objects.create_user(username='iso-user-a', password='x', company=self.company)
        self.user_b = User.objects.create_user(username='iso-user-b', password='x', company=self.company)

    def test_user_a_model_never_trained_on_user_b_rows(self):
        make_outcomes(self.company, self.customer, self.user_a, 40, prefix='A')
        # user_b never reaches the threshold on their own.
        make_outcomes(self.company, self.customer, self.user_b, 10, prefix='B')

        result_a = quote_training.retrain_win_model_for_scope('user', user_id=self.user_a.id)
        self.assertTrue(result_a['trained'])
        self.assertEqual(result_a['samples'], 40)  # not 50 -- user_b's 10 rows never counted

        result_b = quote_training.retrain_win_model_for_scope('user', user_id=self.user_b.id)
        self.assertFalse(result_b['trained'])
        self.assertEqual(result_b['samples'], 10)

    def test_resolve_prefers_user_model_when_it_qualifies(self):
        make_outcomes(self.company, self.customer, self.user_a, 40, prefix='A')
        quote_training.retrain_win_model_for_scope('user', user_id=self.user_a.id)
        # A qualifying global model too, from a different (unrelated) user.
        other_company = Company.objects.create(company_name='Global Donor Co')
        other_customer = Customer.objects.create(
            company=other_company, name='Donor Ltd', email='donor@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        donor_user = User.objects.create_user(username='donor-user', password='x', company=other_company)
        make_outcomes(other_company, other_customer, donor_user, 40, prefix='D')
        quote_training.retrain_win_model_for_scope('global')

        ctx = resolve_prediction_context(self.user_a, self.company)
        self.assertTrue(ctx.available)
        self.assertEqual(ctx.scope, 'user')
        self.assertEqual(ctx.sample_count, 40)

    def test_resolve_falls_back_to_global_when_user_tier_insufficient(self):
        # user_b never qualifies on their own; global does (donor company).
        other_company = Company.objects.create(company_name='Global Donor Co 2')
        other_customer = Customer.objects.create(
            company=other_company, name='Donor Ltd 2', email='donor2@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        donor_user = User.objects.create_user(username='donor-user-2', password='x', company=other_company)
        make_outcomes(other_company, other_customer, donor_user, 40, prefix='D2')
        quote_training.retrain_win_model_for_scope('global')

        ctx = resolve_prediction_context(self.user_b, self.company)
        self.assertTrue(ctx.available)
        self.assertEqual(ctx.scope, 'global')

    def test_resolve_unavailable_when_neither_tier_qualifies(self):
        ctx = resolve_prediction_context(self.user_b, self.company)
        self.assertFalse(ctx.available)
        self.assertIsNone(ctx.scope)
        # predict_proba must still be callable (heuristic) even when unavailable.
        p = ctx.predict_proba({'price_ratio': 1.0})
        self.assertTrue(0.0 <= p <= 1.0)


class ModelVersionFieldWidthTests(_RequiresSklearnMixin, IsolatedModelStorageMixin, TestCase):
    """Every bookkeeping field must fit the column it is stored in.

    This is asserted in Python rather than left to the database on purpose.
    model_version was built with timezone.now().isoformat(), 41 characters
    against a varchar(40): SQLite ignores the declared width, so the whole
    local suite passed while every activation on Postgres raised
    StringDataRightTruncation — after the artifact had already been written to
    disk, leaving production serving a trained model with no version row and
    nothing to compare the next retrain's AUC against. full_clean() applies
    Django's own max_length validation, which is backend-independent.
    """

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(company_name='Width Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Width Ltd', email='width@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(
            username='width-user', password='x', company=self.company)

    def test_activated_global_version_validates_against_its_own_columns(self):
        make_outcomes(self.company, self.customer, self.user, 40, prefix='W')
        result = quote_training.retrain_win_model_for_scope('global')
        self.assertTrue(result.get('trained'), result)

        row = MLModelVersion.objects.get(scope='global', status='active')
        row.full_clean(exclude=['user'])
        self.assertLessEqual(
            len(row.model_version),
            MLModelVersion._meta.get_field('model_version').max_length,
        )

    def test_activated_user_version_validates_too(self):
        # The user scope interpolates a real id where global has a dash, so it
        # is the longer of the two and must be checked separately.
        make_outcomes(self.company, self.customer, self.user, 40, prefix='WU')
        result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)
        self.assertTrue(result.get('trained'), result)

        row = MLModelVersion.objects.get(scope='user', status='active')
        row.full_clean()
        self.assertLessEqual(
            len(row.model_version),
            MLModelVersion._meta.get_field('model_version').max_length,
        )


def make_price_blind_outcomes(company, customer, user, n, *, prefix='PB'):
    """Outcomes where price carries no information: every price level appears
    in both classes in the same proportion, so there is no price->win
    relationship to learn. This is the shape of the data that put a backwards
    model into production.

    The label is driven by i // 8 and the price by i % 8 deliberately. Driving
    the label off i % 2 instead does NOT produce price-blind data: 8 is even,
    so parity aligns with the price cycle and every even-indexed price lands
    in one class — the model then learns a real (if accidental) price signal
    and the gate correctly lets it through.
    """
    for i in range(n):
        total = 16000 + (i % 8) * 1500
        QuoteOutcome.objects.create(
            quote=make_quote(company, customer, number=f'{prefix}-{i}',
                             created_by=user, total=total),
            company=company, created_by=user,
            outcome='accepted' if (i // 8) % 2 == 0 else 'rejected',
            final_price=Decimal(str(total)),
        )


class PriceSensitivityGateTests(_RequiresSklearnMixin, IsolatedModelStorageMixin, TestCase):
    """A model must price before it may go live.

    The first two models this system ever trained both learned price
    backwards — a positive price_ratio coefficient on an 85%-accepted dataset
    — and the margin optimizer, which searches for where expected profit
    peaks, consequently recommended 62% above the operator's own quote at a
    claimed 96% win rate. Their AUCs were 0.72 and 0.63, so no metric gate
    would have stopped either. These tests cover the gate that does.
    """

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(company_name='Gate Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Gate Ltd', email='gate@x.test',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(
            username='gate-user', password='x', company=self.company)

    def test_price_blind_data_is_refused_and_leaves_no_artifact(self):
        make_price_blind_outcomes(self.company, self.customer, self.user, 40)
        result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)

        self.assertFalse(result['trained'])
        self.assertIn('price sensitivity gate', result['reason'])
        # The artifact file is what predictions resolve against, so the gate
        # is worthless unless it runs before the write.
        self.assertFalse(WinProbabilityModel(scope='user', user_id=self.user.id).is_trained())
        ctx = resolve_prediction_context(self.user, self.company)
        self.assertFalse(ctx.available)

    def test_refusal_is_recorded_with_the_measured_sensitivity(self):
        make_price_blind_outcomes(self.company, self.customer, self.user, 40)
        quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)

        row = MLModelVersion.objects.get(scope='user', user_id=self.user.id)
        self.assertEqual(row.status, 'rejected')
        self.assertIn('price sensitivity gate', row.rejection_reason)
        self.assertIn('price_sensitivity', row.evaluation_metrics)

    def test_a_previously_active_model_survives_a_refused_retrain(self):
        # Learnable data first, so there is something live to protect.
        make_outcomes(self.company, self.customer, self.user, 40, prefix='GOOD')
        self.assertTrue(
            quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)['trained'])

        # The threshold is raised to force the refusal rather than feeding in
        # price-blind rows: a retrain sees the whole history, so the original
        # learnable rows would still carry the combined set past the gate.
        with override_settings(WIN_MODEL_MIN_PRICE_SENSITIVITY=0.99):
            result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)

        self.assertFalse(result['trained'])
        # Still serving the good model rather than nothing.
        self.assertTrue(WinProbabilityModel(scope='user', user_id=self.user.id).is_trained())
        self.assertTrue(resolve_prediction_context(self.user, self.company).available)

    def test_a_model_that_passes_actually_prices_downward(self):
        # The gate must not be satisfiable by a model that merely ranks well:
        # assert the live artifact's behaviour directly, the way the quote
        # builder exercises it when an operator moves the price.
        make_outcomes(self.company, self.customer, self.user, 40, prefix='MONO')
        self.assertTrue(
            quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)['trained'])

        model = WinProbabilityModel(scope='user', user_id=self.user.id)
        base = {name: 0.0 for name in model.metadata['feature_names']}
        base.update(price_ratio_available=1.0, quoted_margin_pct=20.0, client_tier=1,
                    historical_acceptance_rate=0.6, distance_km=1000.0, route_popularity=0.4)
        probs = []
        for ratio in (0.45, 0.55, 0.65, 0.75):
            probs.append(model.predict_proba({**base, 'price_ratio': ratio}))
        self.assertEqual(probs, sorted(probs, reverse=True), f'win probability rose with price: {probs}')
        self.assertGreater(probs[0] - probs[-1], 0.05)

    def test_the_gate_threshold_is_configurable(self):
        make_outcomes(self.company, self.customer, self.user, 40, prefix='CFG')
        with override_settings(WIN_MODEL_MIN_PRICE_SENSITIVITY=0.99):
            result = quote_training.retrain_win_model_for_scope('user', user_id=self.user.id)
        self.assertFalse(result['trained'])
        self.assertIn('need >= 0.99', result['reason'])
