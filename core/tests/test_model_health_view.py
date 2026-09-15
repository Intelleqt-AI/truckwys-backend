"""The admin Model Health panel.

Job Health answers "did the task run", which was misleading: retrain_win_model
ran successfully every night for weeks while declining to train, because every
outcome row was labelled 'accepted'. This view exists so that state is visible
rather than inferred, so the tests here are mostly about the blockers.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import Company, Customer, MLModelVersion, QuoteOutcome
from core.tests.test_price_analysis import make_quote

User = get_user_model()


class AdminModelHealthViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='ops', email='ops@truckwys.com', password='pw',
            is_superuser=True, is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.company = Company.objects.create(company_name='Health Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Cust', email='cust@health.test')
        self._n = 0

    def _row(self, label, *, rate=None, source='none'):
        self._n += 1
        quote = make_quote(
            self.company, self.customer, number=f'MH-{self._n:03d}',
            status='SENT', valid_until=date.today() + timedelta(days=5),
        )
        return QuoteOutcome.objects.create(
            quote=quote, company=self.company, outcome=label,
            final_price=quote.total_amount,
            market_rate_at_outcome=Decimal(str(rate)) if rate else None,
            market_rate_source=source,
        )

    def _get(self):
        resp = self.client.get(reverse('admin-model-health'))
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def test_single_class_is_reported_as_a_blocker(self):
        # The exact production state: plenty of rows, none of them negative.
        for _ in range(3):
            self._row('accepted')
        data = self._get()

        self.assertFalse(data['can_train'])
        self.assertIn('only one outcome class present — a classifier cannot train',
                       data['blockers'])
        self.assertEqual(data['training_data']['rejected'], 0)
        self.assertEqual(data['training_data']['class_balance'], 0)

    def test_sample_shortfall_is_reported_separately(self):
        self._row('accepted')
        self._row('rejected')
        data = self._get()

        self.assertEqual(data['training_data']['total'], 2)
        self.assertEqual(data['training_data']['class_balance'], 0.5)
        self.assertTrue(any('outcomes needed' in b for b in data['blockers']))
        # Both classes present, so only the count blocks it.
        self.assertFalse(any('one outcome class' in b for b in data['blockers']))

    def test_feature_coverage_counts_real_market_rates_only(self):
        self._row('accepted', rate=10000, source='platform_lane')
        self._row('rejected', rate=12000, source='company')
        self._row('rejected')
        data = self._get()['feature_coverage']

        self.assertEqual(data['market_rate_resolved'], 2)
        self.assertEqual(data['market_rate_missing'], 1)
        self.assertAlmostEqual(data['market_rate_coverage'], 0.6667, places=3)
        self.assertEqual(data['by_source']['none'], 1)
        # The panel must report whatever the live feature set is, not a
        # literal — this test is about coverage counting, not the version.
        from core.services import quote_features
        self.assertEqual(data['feature_version'], quote_features.FEATURE_VERSION)

    def test_empty_database_does_not_divide_by_zero(self):
        data = self._get()
        self.assertEqual(data['training_data']['total'], 0)
        self.assertEqual(data['feature_coverage']['market_rate_coverage'], 0)
        self.assertEqual(data['active_models'], [])

    def test_active_model_metrics_are_surfaced(self):
        MLModelVersion.objects.create(
            scope='global', status='active', algorithm='logistic_regression',
            feature_version='v3', training_sample_count=62,
            accepted_count=32, rejected_count=30,
            evaluation_metrics={'auc': 0.74, 'brier': 0.19},
        )
        MLModelVersion.objects.create(
            scope='user', user=self.admin, status='failed',
            rejection_reason='only one outcome class present',
        )
        data = self._get()

        self.assertEqual(len(data['active_models']), 1)
        model = data['active_models'][0]
        self.assertEqual(model['evaluation_metrics']['auc'], 0.74)
        self.assertEqual(model['training_sample_count'], 62)
        self.assertEqual(
            data['recent_failures'][0]['rejection_reason'],
            'only one outcome class present',
        )

    def test_non_superuser_is_refused(self):
        self.client.force_authenticate(
            User.objects.create_user(username='joe', email='joe@x.com', password='pw')
        )
        self.assertEqual(
            self.client.get(reverse('admin-model-health')).status_code, 403)
