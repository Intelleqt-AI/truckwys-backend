"""record_charge_failure must hand the dunning notice to exactly one caller.

A billing sweep charges several invoices for the same company back to back,
each with its own freshly-loaded Company instance. Every one of those reads
'active' before any of them writes 'grace_period', so a read-check-save
transition let all of them claim it and all of them notify — one fleet got
eight "Could not charge delivery fee" pushes in a row.
"""
from django.test import TestCase

from core.models import Company
from core.services.subscription_billing import record_charge_failure, record_charge_success


class RecordChargeFailureTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            company_name='Sweep Co', subscription_status='active')

    def _reload(self):
        return Company.objects.get(pk=self.company.pk)

    def test_only_the_first_failure_claims_the_transition(self):
        self.assertTrue(record_charge_failure(self._reload()))
        self.assertFalse(record_charge_failure(self._reload()))
        self.assertEqual(self._reload().subscription_status, 'grace_period')

    def test_stale_instances_from_one_sweep_do_not_all_notify(self):
        # Every charge in a sweep loads the company before any of them writes,
        # so they all still say 'active' in memory. Exactly one may win.
        instances = [self._reload() for _ in range(8)]
        self.assertEqual(sum(record_charge_failure(c) for c in instances), 1)

    def test_loser_gets_the_real_grace_deadline(self):
        # Callers read company.grace_period_expires_at for the dunning copy,
        # so a losing instance must not be left holding None.
        winner, loser = self._reload(), self._reload()
        record_charge_failure(winner)
        record_charge_failure(loser)
        self.assertIsNotNone(loser.grace_period_expires_at)
        self.assertEqual(loser.subscription_status, 'grace_period')
        self.assertEqual(loser.grace_period_expires_at, self._reload().grace_period_expires_at)

    def test_winner_instance_matches_what_was_written(self):
        company = self._reload()
        record_charge_failure(company)
        self.assertEqual(company.subscription_status, 'grace_period')
        self.assertEqual(company.grace_period_expires_at, self._reload().grace_period_expires_at)

    def test_a_success_reopens_the_window_for_the_next_failure(self):
        record_charge_failure(self._reload())
        record_charge_success(self._reload())
        self.assertEqual(self._reload().subscription_status, 'active')
        self.assertTrue(record_charge_failure(self._reload()))

    def test_suspended_company_never_claims_a_transition(self):
        Company.objects.filter(pk=self.company.pk).update(subscription_status='suspended')
        self.assertFalse(record_charge_failure(self._reload()))
        self.assertEqual(self._reload().subscription_status, 'suspended')
