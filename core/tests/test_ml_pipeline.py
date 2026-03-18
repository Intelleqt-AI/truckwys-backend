"""Tests for ML pipeline, feature engineering, and hybrid risk scoring."""

from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.contrib.auth import get_user_model

from core.models import (
    Company, Customer, Invoice, Load, Vehicle, Driver,
    Facility, PaymentOutcome
)
from core.services.feature_engineering import FeatureExtractor
from core.services.risk_engine import RiskEngine
from core.services.risk_monitor import RiskMonitor

User = get_user_model()


class FeatureEngineeringTests(TestCase):
    """Test feature extraction from invoices."""

    def setUp(self):
        """Set up test data."""
        # Create company
        self.company = Company.objects.create(
            company_name='Test Logistics',
            cipc_age_years=5,
            annual_turnover=Decimal('5000000.00'),
            fleet_size=10,
        )

        # Create user
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            company=self.company
        )

        # Create customer
        self.customer = Customer.objects.create(
            name='Test Customer',
            email='customer@example.com',
            phone='0123456789',
            address='123 Test St',
            city='Cape Town',
            state='Western Cape',
            zip_code='8001',
            company=self.company,
            payment_consistency=Decimal('0.85'),
            dispute_rate=Decimal('0.05'),
            avg_days_to_pay=25,
            total_invoices_paid=50,
            total_invoices_late=5,
            credit_score=75,
        )

        # Create invoice
        self.invoice = Invoice.objects.create(
            company=self.company,
            invoice_number='INV-TEST-001',
            customer=self.customer,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            subtotal=Decimal('10000.00'),
            vat_amount=Decimal('1500.00'),
            total_amount=Decimal('11500.00'),
            balance=Decimal('11500.00'),
            payment_terms='NET30',
        )

    def test_feature_extraction_produces_correct_count(self):
        """Test that feature extraction produces exactly 53 features."""
        extractor = FeatureExtractor()
        features = extractor.extract_features(self.invoice)

        expected_feature_names = extractor.get_feature_names()
        self.assertEqual(len(features), len(expected_feature_names))
        self.assertEqual(len(features), 54)  # 15+8+8+7+7+5+4 = 54

    def test_feature_values_are_numeric(self):
        """Test that all extracted features are numeric."""
        extractor = FeatureExtractor()
        features = extractor.extract_features(self.invoice)

        for key, value in features.items():
            self.assertIsInstance(
                value,
                (int, float),
                f"Feature {key} has non-numeric value: {value}"
            )

    def test_feature_validation(self):
        """Test feature validation."""
        extractor = FeatureExtractor()
        features = extractor.extract_features(self.invoice)

        # Should be valid
        self.assertTrue(extractor.validate_features(features))

        # Missing feature should fail
        incomplete_features = {k: v for k, v in list(features.items())[:10]}
        self.assertFalse(extractor.validate_features(incomplete_features))

    def test_client_features_extraction(self):
        """Test client-specific features are extracted correctly."""
        extractor = FeatureExtractor()
        features = extractor.extract_features(self.invoice)

        # Check key client features
        self.assertEqual(features['client_payment_consistency'], 0.85)
        self.assertEqual(features['client_dispute_rate'], 0.05)
        self.assertEqual(features['client_avg_days_to_pay'], 25.0)
        self.assertEqual(features['client_credit_score'], 75.0)
        self.assertEqual(features['client_total_invoices_paid'], 50.0)

    def test_invoice_features_extraction(self):
        """Test invoice-specific features."""
        extractor = FeatureExtractor()
        features = extractor.extract_features(self.invoice)

        self.assertEqual(features['invoice_total_amount'], 11500.0)
        self.assertEqual(features['invoice_payment_terms_days'], 30.0)
        self.assertEqual(features['invoice_is_overdue'], 0.0)


class PaymentOutcomeTests(TestCase):
    """Test PaymentOutcome model."""

    def setUp(self):
        """Set up test data."""
        self.company = Company.objects.create(company_name='Test Co')
        self.user = User.objects.create_user(
            username='test', email='test@test.com', password='test', company=self.company
        )
        self.customer = Customer.objects.create(
            name='Customer', email='cust@test.com', phone='123',
            address='Addr', city='City', state='State', zip_code='123',
            company=self.company
        )
        self.invoice = Invoice.objects.create(
            company=self.company,
            invoice_number='INV-001',
            customer=self.customer,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            subtotal=Decimal('1000.00'),
            total_amount=Decimal('1150.00'),
            balance=Decimal('1150.00'),
        )

    def test_payment_outcome_creation(self):
        """Test creating a payment outcome."""
        outcome = PaymentOutcome.objects.create(
            invoice=self.invoice,
            expected_payment_date=self.invoice.due_date,
            actual_payment_date=self.invoice.due_date + timedelta(days=5),
            payment_amount=self.invoice.total_amount,
            feature_snapshot={'test_feature': 1.0},
        )

        self.assertEqual(outcome.days_late, 5)
        self.assertFalse(outcome.defaulted)
        self.assertEqual(outcome.payment_category, 'MINOR_LATE')

    def test_payment_outcome_auto_default_flag(self):
        """Test that outcome is auto-marked as default if >90 days late."""
        outcome = PaymentOutcome.objects.create(
            invoice=self.invoice,
            expected_payment_date=self.invoice.due_date,
            actual_payment_date=self.invoice.due_date + timedelta(days=100),
            payment_amount=self.invoice.total_amount,
        )

        self.assertEqual(outcome.days_late, 100)
        self.assertTrue(outcome.defaulted)
        self.assertEqual(outcome.payment_category, 'DEFAULT')

    def test_payment_outcome_high_risk_property(self):
        """Test is_high_risk property."""
        # On-time payment
        outcome1 = PaymentOutcome(
            invoice=self.invoice,
            expected_payment_date=self.invoice.due_date,
            actual_payment_date=self.invoice.due_date,
            days_late=0,
            payment_amount=self.invoice.total_amount,
        )
        self.assertFalse(outcome1.is_high_risk)

        # 35 days late
        outcome2 = PaymentOutcome(
            invoice=self.invoice,
            expected_payment_date=self.invoice.due_date,
            actual_payment_date=self.invoice.due_date + timedelta(days=35),
            days_late=35,
            payment_amount=self.invoice.total_amount,
        )
        self.assertTrue(outcome2.is_high_risk)


class HybridRiskScoringTests(TestCase):
    """Test hybrid risk scoring (rules + ML)."""

    def setUp(self):
        """Set up test data."""
        self.company = Company.objects.create(
            company_name='Test Co',
            cipc_age_years=3,
            annual_turnover=Decimal('2000000.00'),
        )
        self.user = User.objects.create_user(
            username='test', email='test@test.com', password='test', company=self.company
        )
        self.customer = Customer.objects.create(
            name='Customer', email='cust@test.com', phone='123',
            address='Addr', city='City', state='State', zip_code='123',
            company=self.company,
            payment_consistency=Decimal('0.90'),
            total_invoices_paid=20,
        )
        self.invoice = Invoice.objects.create(
            company=self.company,
            invoice_number='INV-001',
            customer=self.customer,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            subtotal=Decimal('5000.00'),
            total_amount=Decimal('5750.00'),
            balance=Decimal('5750.00'),
        )
        self.facility = Facility.objects.create(
            company=self.company,
            limit=Decimal('500000.00'),
            outstanding=Decimal('0.00'),
            status='ACTIVE',
        )

    def test_hybrid_scoring_runs_without_ml(self):
        """Test hybrid scoring works when ML model not trained."""
        engine = RiskEngine(self.invoice, self.facility)
        result = engine.score_with_ml()

        self.assertTrue(result['success'])
        self.assertIn('rules_score', result)
        self.assertIn('blend_score', result)
        self.assertEqual(result['ml_available'], False)
        self.assertEqual(result['ml_weight_used'], 0.0)

        # When ML not available, blend should equal rules
        self.assertEqual(result['blend_score'], result['rules_score'])

    def test_hybrid_scoring_returns_feature_importances(self):
        """Test that hybrid scoring returns feature importances if ML available."""
        engine = RiskEngine(self.invoice, self.facility)
        result = engine.score_with_ml()

        # Even without ML, should have empty list
        self.assertIn('feature_importances', result)
        self.assertIsInstance(result['feature_importances'], list)


class RiskMonitorTests(TestCase):
    """Test risk monitoring service."""

    def setUp(self):
        """Set up test data."""
        self.company = Company.objects.create(company_name='Test Co')
        self.user = User.objects.create_user(
            username='test', email='test@test.com', password='test', company=self.company
        )
        self.customer = Customer.objects.create(
            name='Customer', email='cust@test.com', phone='123',
            address='Addr', city='City', state='State', zip_code='123',
            company=self.company,
            total_invoices_paid=10,
            total_invoices_late=1,
        )
        self.invoice = Invoice.objects.create(
            company=self.company,
            invoice_number='INV-001',
            customer=self.customer,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            subtotal=Decimal('10000.00'),
            total_amount=Decimal('11500.00'),
            balance=Decimal('11500.00'),
        )

    def test_anomaly_detection_first_invoice(self):
        """Test detection of first invoice anomaly."""
        monitor = RiskMonitor(company=self.company)
        anomalies = monitor.detect_anomalies(self.invoice)

        # Should detect first invoice
        first_invoice_anomaly = next(
            (a for a in anomalies if a['type'] == 'first_invoice'),
            None
        )
        self.assertIsNotNone(first_invoice_anomaly)
        self.assertEqual(first_invoice_anomaly['severity'], 'medium')

    def test_anomaly_detection_overdue_invoice(self):
        """Test detection of overdue invoice."""
        # Create overdue invoice
        overdue_invoice = Invoice.objects.create(
            company=self.company,
            invoice_number='INV-OVERDUE',
            customer=self.customer,
            issue_date=date.today() - timedelta(days=60),
            due_date=date.today() - timedelta(days=30),
            subtotal=Decimal('5000.00'),
            total_amount=Decimal('5750.00'),
            balance=Decimal('5750.00'),
            status='OVERDUE',
        )

        monitor = RiskMonitor(company=self.company)
        anomalies = monitor.detect_anomalies(overdue_invoice)

        # Should detect overdue
        overdue_anomaly = next(
            (a for a in anomalies if a['type'] == 'already_overdue'),
            None
        )
        self.assertIsNotNone(overdue_anomaly)
        self.assertEqual(overdue_anomaly['severity'], 'critical')

    def test_portfolio_health_check(self):
        """Test portfolio health metrics calculation."""
        monitor = RiskMonitor(company=self.company)
        health = monitor.check_portfolio_health()

        self.assertTrue(health['success'])
        self.assertIn('total_invoices', health)
        self.assertIn('risk_scores', health)
        self.assertIn('tier_distribution', health)
        self.assertIn('payment_outcomes', health)


class MLIntegrationTests(TestCase):
    """Integration tests for ML pipeline (requires ML libraries)."""

    def setUp(self):
        """Set up test data."""
        self.company = Company.objects.create(company_name='Test Co')
        self.user = User.objects.create_user(
            username='test', email='test@test.com', password='test', company=self.company
        )
        self.customer = Customer.objects.create(
            name='Customer', email='cust@test.com', phone='123',
            address='Addr', city='City', state='State', zip_code='123',
            company=self.company,
        )

    def test_ml_pipeline_cold_start(self):
        """Test ML pipeline handles cold start gracefully."""
        try:
            from core.services.ml_pipeline import RiskMLPipeline

            pipeline = RiskMLPipeline()

            # Should not crash on initialization
            self.assertIsNotNone(pipeline)

            # Without training data, model_info should return None
            info = pipeline.get_model_info()
            # Could be None or a dict with trained: False
            if info is not None:
                self.assertIn('trained', info)

        except ImportError:
            # ML libraries not installed - skip test
            self.skipTest("ML libraries not installed")
