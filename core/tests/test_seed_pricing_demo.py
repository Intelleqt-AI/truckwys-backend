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
        # VAT) lands at R16-18/km for the model company; the rules company
        # stays below the trip threshold. Night-out allowances are never
        # booked as expenses (the floor's own allowance line covers them).
        from core.models import Expense
        from core.services.pricing_analysis import fixed_cost_per_km
        fixed = fixed_cost_per_km(model, vt_name='Superlink Tautliner')
        self.assertEqual(fixed['source'], 'company_actuals')
        self.assertTrue(16 <= fixed['value'] <= 18, fixed)
        self.assertGreater(fixed['actuals']['company_level'], 0)
        self.assertEqual(fixed_cost_per_km(rules, vt_name='Superlink Tautliner')['source'], 'vehicle_default')
        self.assertTrue(Expense.objects.filter(company=rules, trip__isnull=True).exists())
        self.assertFalse(Expense.objects.filter(company__in=[model, rules],
                                                description__icontains='allowance').exists())

        # Believable SA fuel figures at rated payload (seed data, not engine).
        from core.models import VehicleType
        l100 = {vt.name: float(vt.fuel_consumption_l_per_100km) for vt in VehicleType.objects.filter(company=model)}
        self.assertTrue(45 <= l100['Superlink Tautliner'] <= 48, l100)
        self.assertTrue(38 <= l100['Tri-axle Tautliner'] <= 40, l100)
        self.assertTrue(30 <= l100['Rigid 6x4 Curtainsider'] <= 32, l100)

        # 2026 JHB->DBN superlink market: platform band in the low-to-mid
        # R20 000s, so one-way margins over a ~R17/km floor stay believable.
        from core.services.lane_benchmark import compute_lane_benchmark
        bench = compute_lane_benchmark('JHB', 'DBN')
        self.assertTrue(bench['available'])
        self.assertTrue(20000 <= bench['p25'] < bench['market_median_rate'] < bench['p75'] <= 30000, bench)

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


class SeedPricingDemoSafetyTests(TestCase):
    """Never against a remote (production) database, whatever the flags, and
    no password known in advance."""

    def test_database_is_local_rules(self):
        local = seed.database_is_local
        self.assertTrue(local({'ENGINE': 'django.db.backends.sqlite3', 'NAME': '/tmp/db.sqlite3'})[0])
        for host in ('', 'localhost', '127.0.0.1', '::1'):
            self.assertTrue(local({'ENGINE': 'django.db.backends.postgresql', 'HOST': host})[0], host)
        self.assertFalse(local({'ENGINE': 'django.db.backends.postgresql',
                                'HOST': 'dpg-abc123.oregon-postgres.render.com'})[0])
        from unittest import mock
        with mock.patch.dict('os.environ', {seed.ALLOWED_HOSTS_ENV: 'db'}):
            self.assertTrue(local({'ENGINE': 'django.db.backends.postgresql', 'HOST': 'db'})[0])
            self.assertFalse(local({'ENGINE': 'django.db.backends.postgresql', 'HOST': 'prod.example.com'})[0])

    def test_remote_database_refused_even_with_force_and_debug(self):
        from unittest import mock
        with mock.patch.object(seed, 'database_is_local', return_value=(False, 'host prod.example.com')), \
                override_settings(DEBUG=True):
            with self.assertRaisesRegex(CommandError, 'never run against production'):
                call_command('seed_pricing_demo', '--force', '--reset', stdout=StringIO())
        self.assertFalse(Company.objects.filter(company_name__in=seed.all_company_names()).exists())

    def test_random_password_each_run_and_printed(self):
        from unittest import mock
        with mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop(seed.PASSWORD_ENV, None)
            outs = []
            for _ in range(2):
                buf = StringIO()
                call_command('seed_pricing_demo', '--force', stdout=buf)
                outs.append(buf.getvalue())
        pw = [re.search(r'password "([^"]+)"', o).group(1) for o in outs]
        self.assertNotEqual(pw[0], pw[1])
        self.assertNotIn('demo12345', pw)
        user = User.objects.get(username=f'model@{seed.DEMO_EMAIL_DOMAIN}')
        self.assertTrue(user.check_password(pw[1]))          # the latest printed password works
        self.assertFalse(user.check_password('demo12345'))

    def test_password_from_env_when_set(self):
        from unittest import mock
        with mock.patch.dict('os.environ', {seed.PASSWORD_ENV: 'local-only-pw'}):
            call_command('seed_pricing_demo', '--force', stdout=StringIO())
        self.assertTrue(User.objects.get(username=f'model@{seed.DEMO_EMAIL_DOMAIN}').check_password('local-only-pw'))
