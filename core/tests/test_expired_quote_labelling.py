"""Expiring a quote records a 'rejected' ML training label.

This is the only negative label the product generates in volume. Without it
every QuoteOutcome row is 'accepted', and core.services.quote_training refuses
to train at all ("only one outcome class present") no matter how much data
accumulates — which is exactly the state production was in.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from core.models import Company, Customer, Quote, QuoteOutcome
from core.services.notification_sweeps import sweep_expired_quotes
from core.tests.test_price_analysis import make_quote

User = get_user_model()


class ExpirySweepLabellingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='Expiry Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Cust', email='cust@expiry.test')
        self.user = User.objects.create_user(
            username='exp', email='exp@test.com', password='x', company=self.company)

    def _sent(self, number, *, valid_days_ago=3, **extra):
        return make_quote(
            self.company, self.customer, number=number, status='SENT',
            valid_until=date.today() - timedelta(days=valid_days_ago),
            created_by=self.user, **extra
        )

    def test_expiring_records_a_rejected_outcome(self):
        q = self._sent('EXP-001')
        result = sweep_expired_quotes()

        self.assertEqual(result['expired'], 1)
        self.assertEqual(result['labelled_rejected'], 1)
        q.refresh_from_db()
        self.assertEqual(q.status, 'EXPIRED')
        outcome = QuoteOutcome.objects.get(quote=q)
        self.assertEqual(outcome.outcome, 'rejected')
        self.assertEqual(outcome.rejection_reason, 'Expired without response')

    def test_an_already_accepted_quote_keeps_its_label(self):
        # Accepted, then drifted past valid_until. The label records the
        # decision, not the final status — flipping it would corrupt training.
        q = self._sent('EXP-002', outcome='accepted')
        QuoteOutcome.objects.create(
            quote=q, company=self.company, outcome='accepted',
            final_price=q.total_amount,
        )
        result = sweep_expired_quotes()

        self.assertEqual(result['expired'], 1)
        self.assertEqual(result['labelled_rejected'], 0)
        self.assertEqual(QuoteOutcome.objects.get(quote=q).outcome, 'accepted')

    def test_expiry_still_happens_if_labelling_fails(self):
        # The status transition drives customer-facing behaviour; a training
        # label is strictly secondary and must never hold it hostage.
        from unittest import mock
        q = self._sent('EXP-003')
        with mock.patch(
            'core.services.quote_outcome_capture.record_quote_outcome',
            side_effect=RuntimeError('boom'),
        ):
            result = sweep_expired_quotes()
        self.assertEqual(result['expired'], 1)
        self.assertEqual(result['labelled_rejected'], 0)
        q.refresh_from_db()
        self.assertEqual(q.status, 'EXPIRED')

    def test_sweep_ignores_quotes_still_within_validity(self):
        make_quote(
            self.company, self.customer, number='EXP-004', status='SENT',
            valid_until=date.today() + timedelta(days=5), created_by=self.user,
        )
        result = sweep_expired_quotes()
        self.assertEqual(result['expired'], 0)
        self.assertFalse(QuoteOutcome.objects.exists())

    def test_both_classes_present_after_a_mixed_run(self):
        # The end state that matters: a dataset a classifier can actually use.
        won = make_quote(
            self.company, self.customer, number='EXP-005', status='ACCEPTED',
            outcome='accepted', created_by=self.user)
        QuoteOutcome.objects.create(
            quote=won, company=self.company, outcome='accepted',
            final_price=won.total_amount)
        self._sent('EXP-006')
        sweep_expired_quotes()

        labels = set(QuoteOutcome.objects.values_list('outcome', flat=True))
        self.assertEqual(labels, {'accepted', 'rejected'})


class BackfillCommandTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='Backfill Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Cust', email='cust@backfill.test')

    def _expired(self, number, **extra):
        return make_quote(
            self.company, self.customer, number=number, status='EXPIRED', **extra)

    def _run(self, *args):
        out = StringIO()
        call_command('backfill_expired_quote_outcomes', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        self._expired('BF-001')
        output = self._run('--dry-run')
        self.assertIn('to label: 1', output)
        self.assertIn('Dry run', output)
        self.assertFalse(QuoteOutcome.objects.exists())

    def test_labels_expired_quotes_without_an_outcome(self):
        self._expired('BF-002')
        self._expired('BF-003')
        self._run()
        self.assertEqual(
            list(QuoteOutcome.objects.values_list('outcome', flat=True)),
            ['rejected', 'rejected'],
        )

    def test_never_touches_an_existing_label(self):
        q = self._expired('BF-004', outcome='accepted')
        QuoteOutcome.objects.create(
            quote=q, company=self.company, outcome='accepted',
            final_price=Decimal('25000'))
        self._run()
        self.assertEqual(QuoteOutcome.objects.get(quote=q).outcome, 'accepted')

    def test_is_idempotent(self):
        self._expired('BF-005')
        self._run()
        second = self._run()
        self.assertIn('Nothing to do', second)
        self.assertEqual(QuoteOutcome.objects.count(), 1)

    def test_company_filter_limits_scope(self):
        other = Company.objects.create(company_name='Other Co')
        other_cust = Customer.objects.create(
            company=other, name='C2', email='c2@backfill.test')
        self._expired('BF-006')
        make_quote(other, other_cust, number='BF-007', status='EXPIRED')

        self._run('--company-id', str(self.company.id))
        self.assertEqual(QuoteOutcome.objects.count(), 1)
        self.assertEqual(QuoteOutcome.objects.first().quote.quote_number, 'BF-006')
