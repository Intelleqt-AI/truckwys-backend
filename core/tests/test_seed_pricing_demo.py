"""seed_pricing_demo: runs, is idempotent, resets only what it created,
refuses to run with DEBUG off, and writes fictional names only."""
import re
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from core.management.commands import seed_pricing_demo as seed
from core.models import Company, Customer, Quote, QuoteOutcome, Trip, User
from core.tests.test_demo_company_seed import REAL_BRAND_DENYLIST


def _counts():
    return {
        'companies': Company.objects.filter(company_name__in=seed.all_company_names()).count(),
        'quotes': Quote.objects.filter(quote_number__startswith=f'{seed.PREFIX}-').count(),
        'outcomes': QuoteOutcome.objects.filter(quote__quote_number__startswith=f'{seed.PREFIX}-').count(),
        'trips': Trip.objects.filter(load__load_number__startswith=f'{seed.PREFIX}-').count(),
        'customers': Customer.objects.filter(company__company_name__in=seed.all_company_names()).count(),
        'logins': User.objects.filter(username__contains=seed.DEMO_EMAIL_DOMAIN).count(),
    }


class SeedPricingDemoTests(TestCase):
    def _run(self, *args):
        call_command('seed_pricing_demo', '--force', *args, stdout=StringIO())

    def test_refuses_without_debug(self):
        with override_settings(DEBUG=False):
            with self.assertRaises(CommandError):
                call_command('seed_pricing_demo', stdout=StringIO())

    def test_seeds_every_state_and_is_idempotent(self):
        bystander = Company.objects.create(company_name='Unrelated Tenant')
        self._run()
        first = _counts()
        self.assertEqual(first['companies'], 8)
        self.assertEqual(first['logins'], 8)

        model = Company.objects.get(company_name=seed.COMPANIES['model']['name'])
        rules = Company.objects.get(company_name=seed.COMPANIES['rules']['name'])
        cold = Company.objects.get(company_name=seed.COMPANIES['cold']['name'])
        decided = QuoteOutcome.objects.filter(quote__company=model)
        self.assertGreaterEqual(decided.count(), 60)
        self.assertTrue(decided.filter(outcome='accepted').exists())
        self.assertTrue(decided.filter(outcome='rejected').exists())
        self.assertGreaterEqual(Trip.objects.filter(load__company=model, status='COMPLETED').count(), 15)
        self.assertTrue(12 <= QuoteOutcome.objects.filter(quote__company=rules).count() < 40)
        self.assertLess(Trip.objects.filter(load__company=rules, status='COMPLETED').count(), 10)
        self.assertFalse(Quote.objects.filter(company=cold).exists())

        # Every outcome carries a current feature snapshot; won JHB->DBN
        # quotes are on average cheaper vs market than lost ones.
        from core.services.quote_features import FEATURE_VERSION
        rows = list(decided.filter(quote__destination='DBN'))
        self.assertTrue(all(o.feature_snapshot.get('feature_version') == FEATURE_VERSION for o in rows))
        won = [o.feature_snapshot['features']['price_ratio'] for o in rows if o.outcome == 'accepted']
        lost = [o.feature_snapshot['features']['price_ratio'] for o in rows if o.outcome == 'rejected']
        self.assertLess(sum(won) / len(won), sum(lost) / len(lost))

        # All-in operating cost (trip-linked + company-level bills, net of
        # VAT) lands at R13-15/km for the model company; the rules company
        # stays below the trip threshold. Night-out allowances are never
        # booked as expenses (the floor's own allowance line covers them).
        from core.models import Expense
        from core.services.pricing_analysis import fixed_cost_per_km
        fixed = fixed_cost_per_km(model, vt_name='Superlink Tautliner')
        self.assertEqual(fixed['source'], 'company_actuals')
        self.assertTrue(13 <= fixed['value'] <= 15, fixed)
        self.assertGreater(fixed['actuals']['company_level'], 0)
        self.assertEqual(fixed_cost_per_km(rules, vt_name='Superlink Tautliner')['source'], 'vehicle_default')
        self.assertTrue(Expense.objects.filter(company=rules, trip__isnull=True).exists())
        self.assertFalse(Expense.objects.filter(company__in=[model, rules],
                                                description__icontains='allowance').exists())

        from core.services.lane_benchmark import compute_lane_benchmark
        self.assertTrue(compute_lane_benchmark('JHB', 'DBN')['available'])

        self._run()
        self.assertEqual(_counts(), first)

        self._run('--reset', '--no-reseed')
        self.assertEqual(sum(_counts().values()), 0)
        self.assertTrue(Company.objects.filter(pk=bystander.pk).exists())

    def test_fictional_names_only(self):
        for name in seed.all_fictional_names():
            lowered = name.lower()
            for brand in REAL_BRAND_DENYLIST:
                self.assertIsNone(re.search(rf'\b{re.escape(brand)}\b', lowered), f'{name!r} contains {brand!r}')
        for spec in seed.COMPANIES.values():
            self.assertTrue(spec['name'].endswith('(Demo)'))
