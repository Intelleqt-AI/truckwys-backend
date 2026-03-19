"""Tests for AI Quote services and API endpoints (Phase 2 Sprint 2)."""

from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from core.models import Company
from core.services.quote_ml_lgbm import QuoteMarginModel
from core.services.quote_acceptance_model import QuoteAcceptanceModel
from core.services.revenue_guard import RevenueGuardEngine
from core.services.quote_explainer import QuoteExplainer

User = get_user_model()


class QuoteMarginModelTests(TestCase):
    """Tests for LightGBM margin prediction model."""

    def test_margin_model_trained_and_predicts(self):
        """Test that model can load and make predictions."""
        if not QuoteMarginModel.is_trained():
            self.skipTest('Model not trained')

        model = QuoteMarginModel()
        features = {feat: 100.0 for feat in model.FEATURE_NAMES}
        result = model.predict(features)

        self.assertIn('predicted_margin_pct', result)
        self.assertIn('confidence', result)
        self.assertIsInstance(result['predicted_margin_pct'], float)


class QuoteAcceptanceModelTests(TestCase):
    """Tests for XGBoost acceptance prediction model."""

    def test_acceptance_model_trained_and_predicts(self):
        """Test that model can load and predict probability."""
        if not QuoteAcceptanceModel.is_trained():
            self.skipTest('Model not trained')

        model = QuoteAcceptanceModel()
        features = {feat: 0.5 for feat in model.FEATURE_NAMES}
        prob = model.predict_probability(features)

        self.assertIsInstance(prob, float)
        self.assertGreaterEqual(prob, 0.0)
        self.assertLessEqual(prob, 1.0)


class QuoteExplainerTests(TestCase):
    """Tests for SHAP explainability."""

    def test_explainer_returns_top_5_factors(self):
        """Test that explainer returns top 5 factors."""
        if not QuoteMarginModel.is_trained():
            self.skipTest('Model not trained')

        model = QuoteMarginModel()
        explainer = QuoteExplainer()
        features = {feat: 100.0 for feat in model.FEATURE_NAMES}

        factors = explainer.explain_margin_prediction(features, model)

        self.assertIsInstance(factors, list)
        self.assertLessEqual(len(factors), 5)
        if len(factors) > 0:
            self.assertIn('feature', factors[0])
            self.assertIn('impact', factors[0])


class RevenueGuardEngineTests(TestCase):
    """Tests for Revenue Guard safety checks."""

    def test_guard_flags_low_margin(self):
        """Test that guard flags quotes with <12% margin."""
        guard = RevenueGuardEngine()

        result = guard.check(
            quote_price=10000,
            distance_km=500,
            load_type='general',
            truck_type='semi',
        )

        self.assertIsInstance(result, dict)
        self.assertIn('safe', result)
        self.assertIn('risk_score', result)
        self.assertIn('rating', result)
        self.assertIn('warnings', result)

        # Low price should trigger warnings
        self.assertFalse(result['safe'])
        self.assertGreater(result['risk_score'], 0)

    def test_guard_flags_slow_payer(self):
        """Test that guard flags clients with >60 day payment avg."""
        # Create mock client with slow payment
        class MockClient:
            avg_days_to_pay = 75

        guard = RevenueGuardEngine()
        result = guard.check(
            quote_price=50000,
            distance_km=500,
            load_type='general',
            truck_type='semi',
            client=MockClient(),
        )

        # Should have warning about slow payer
        warning_codes = [w['code'] for w in result['warnings']]
        self.assertIn('SLOW_PAYER', warning_codes)


class QuoteSuggestAPITests(TestCase):
    """Tests for POST /api/v1/quotes/suggest/ endpoint."""

    def setUp(self):
        """Set up test client and auth."""
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Test Co')
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            password='testpass',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def test_suggest_endpoint_returns_200(self):
        """Test that suggest endpoint returns 200 with valid data."""
        response = self.client.post('/api/v1/quotes/suggest/', {
            'distance_km': 570,
            'load_type': 'general',
            'truck_type': 'semi',
            'load_weight_tons': 25,
            'urgency': 3,
            'return_load_available': False,
            'origin': 'Johannesburg',
            'destination': 'Durban',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('suggested_price', response.data)
        self.assertIn('confidence', response.data)
        self.assertIn('margin_pct', response.data)


class QuoteGuardAPITests(TestCase):
    """Tests for POST /api/v1/quotes/guard/ endpoint."""

    def setUp(self):
        """Set up test client and auth."""
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Test Co')
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            password='testpass',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def test_guard_endpoint_returns_200(self):
        """Test that guard endpoint returns 200."""
        response = self.client.post('/api/v1/quotes/guard/', {
            'quote_price': 42000,
            'distance_km': 570,
            'load_type': 'general',
            'truck_type': 'semi',
            'origin': 'Johannesburg',
            'destination': 'Durban',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('safe', response.data)
        self.assertIn('risk_score', response.data)
        self.assertIn('rating', response.data)


class ChatQuoteAPITests(TestCase):
    """Tests for POST /api/v1/ai/chat-quote/ endpoint."""

    def setUp(self):
        """Set up test client and auth."""
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Test Co')
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            password='testpass',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def test_chat_quote_endpoint_returns_200(self):
        """Test that chat-quote endpoint returns 200 with messages."""
        response = self.client.post('/api/v1/ai/chat-quote/', {
            'messages': [
                {'role': 'user', 'content': 'Quote JHB to CPT'}
            ]
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('extracted_fields', response.data)
        self.assertIn('response_text', response.data)
        self.assertIn('ready_to_quote', response.data)


class VoiceQuoteAPITests(TestCase):
    """Tests for POST /api/v1/ai/voice-quote/ endpoint."""

    def setUp(self):
        """Set up test client and auth."""
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Test Co')
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            password='testpass',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def test_voice_quote_missing_audio(self):
        """Test that voice-quote endpoint returns 400 when no audio file."""
        response = self.client.post('/api/v1/ai/voice-quote/', {}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)
