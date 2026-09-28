"""API data-correctness regressions (docs/backend-changes/2026-09-api-data-correctness.md).

Each test class pins one change and was written to FAIL on main @ 45039ee
before the fix:

- PageSizeTests            ?page_size= honoured (max 100), default page unchanged
- FinanceFilterTests       invoices/payments/expenses filters applied; bad values 400
- FleetOverviewHonestyTests no invented fallback numbers on fleet/overview
- SignalsHonestyTests      no invented fee/timing/revenue-loss copy in signals
- BriefingExpenseTests     briefing spend counts APPROVED expenses only (audit #43)
- IntelligenceErrorTests   recommendations/cashflow failures are not 200 + zeros (#44, #45)
- SchemaTests              /api/schema/ builds (C2)
- ThrottlePolicyTests      normal read navigation is not throttled at 60/min (H3)
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, Expense, Invoice, Payment, Vehicle, VehicleType

User = get_user_model()


def _user(username, company, role='ADMIN'):
    user = User.objects.create_user(username=username, email=f'{username}@dc.test', password='x')
    user.role = role
    user.company = company
    user.save()
    return user


def _customer(company, name='DC Customer'):
    return Customer.objects.create(
        company=company, name=name, email=f'{name.replace(" ", "").lower()}@dc.test',
        phone='', address='', city='JHB', state='', zip_code='',
        credit_score=85, credit_score_source='MANUAL',
    )


def _expense(company, n, status='APPROVED', category='FUEL', amount='100.00', expense_date=None):
    return Expense(
        company=company, expense_number=f'EXP-DC-{company.pk}-{n}', category=category,
        description='x', amount=Decimal(amount), expense_date=expense_date or date.today(),
        status=status,
    )


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='DC Transport')
        cls.user = _user('dc_admin', cls.co)

    def setUp(self):
        cache.clear()  # throttle state lives in the default cache
        self.client = APIClient()
        self.client.force_authenticate(self.user)


class PageSizeTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Expense.objects.bulk_create([_expense(cls.co, i) for i in range(105)])

    def test_default_page_size_is_unchanged(self):
        r = self.client.get('/api/v1/expenses/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['count'], 105)
        self.assertEqual(len(r.data['results']), 20)

    def test_page_size_is_honoured(self):
        r = self.client.get('/api/v1/expenses/?page_size=50')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['results']), 50)

    def test_page_size_is_capped_at_100(self):
        r = self.client.get('/api/v1/expenses/?page_size=500')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['results']), 100)
        self.assertIsNotNone(r.data['next'])

    def test_next_link_keeps_page_size(self):
        r = self.client.get('/api/v1/expenses/?page_size=50')
        self.assertIn('page_size=50', r.data['next'])

    def test_bad_page_size_falls_back_to_default(self):
        # DRF behaviour: a non-numeric page_size is ignored, not an error.
        r = self.client.get('/api/v1/expenses/?page_size=abc')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data['results']), 20)

    def test_quotes_pagination_unchanged(self):
        # QuoteViewSet already had its own page_size support; still works.
        r = self.client.get('/api/v1/quotes/?page_size=5')
        self.assertEqual(r.status_code, 200)


class FinanceFilterTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = date.today()
        cls.cust1 = _customer(cls.co, 'DC One')
        cls.cust2 = _customer(cls.co, 'DC Two')
        cls.inv_draft = Invoice.objects.create(
            company=cls.co, customer=cls.cust1, invoice_number='INV-DC-1',
            due_date=today + timedelta(days=30), subtotal=Decimal('1000.00'), status='DRAFT',
        )
        cls.inv_sent = Invoice.objects.create(
            company=cls.co, customer=cls.cust2, invoice_number='INV-DC-2',
            due_date=today + timedelta(days=30), subtotal=Decimal('2000.00'), status='SENT',
        )
        cls.pay1 = Payment.objects.create(
            company=cls.co, payment_number='PAY-DC-1', invoice=cls.inv_sent, customer=cls.cust2,
            amount=Decimal('100.00'), payment_date=today, payment_method='EFT',
        )
        cls.pay2 = Payment.objects.create(
            company=cls.co, payment_number='PAY-DC-2', invoice=cls.inv_draft, customer=cls.cust1,
            amount=Decimal('50.00'), payment_date=today, payment_method='CASH',
        )
        Expense.objects.bulk_create([
            _expense(cls.co, 1, status='APPROVED', category='FUEL', expense_date=today),
            _expense(cls.co, 2, status='PENDING', category='TOLLS', expense_date=today - timedelta(days=40)),
            _expense(cls.co, 3, status='REJECTED', category='FUEL', expense_date=today - timedelta(days=5)),
        ])

    def _numbers(self, r, key):
        self.assertEqual(r.status_code, 200, r.data)
        return sorted(row[key] for row in r.data['results'])

    # invoices
    def test_invoice_status_filter(self):
        r = self.client.get('/api/v1/invoices/?status=DRAFT')
        self.assertEqual(self._numbers(r, 'invoice_number'), ['INV-DC-1'])

    def test_invoice_invalid_status_is_400(self):
        r = self.client.get('/api/v1/invoices/?status=zzz')
        self.assertEqual(r.status_code, 400)

    def test_invoice_customer_filter(self):
        r = self.client.get(f'/api/v1/invoices/?customer={self.cust2.pk}')
        self.assertEqual(self._numbers(r, 'invoice_number'), ['INV-DC-2'])

    def test_invoice_unfiltered_list_unchanged(self):
        r = self.client.get('/api/v1/invoices/')
        self.assertEqual(self._numbers(r, 'invoice_number'), ['INV-DC-1', 'INV-DC-2'])

    # payments — InvoiceDetail.tsx fetches payments/?invoice=<id>
    def test_payment_invoice_filter(self):
        r = self.client.get(f'/api/v1/payments/?invoice={self.inv_sent.pk}')
        self.assertEqual(self._numbers(r, 'payment_number'), ['PAY-DC-1'])

    def test_payment_invoice_filter_non_numeric_is_400(self):
        r = self.client.get('/api/v1/payments/?invoice=abc')
        self.assertEqual(r.status_code, 400)

    def test_payment_filter_on_other_tenants_invoice_is_empty_not_400(self):
        # No existence oracle: an id that is not yours reads the same as one
        # that does not exist — an empty list.
        other = Company.objects.create(company_name='DC Other')
        oc = _customer(other, 'DC Foreign')
        foreign = Invoice.objects.create(
            company=other, customer=oc, invoice_number='INV-DC-X',
            due_date=date.today() + timedelta(days=30), subtotal=Decimal('10.00'), status='SENT',
        )
        for inv_id in (foreign.pk, 999999):
            r = self.client.get(f'/api/v1/payments/?invoice={inv_id}')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data['count'], 0)

    def test_payment_method_filter(self):
        r = self.client.get('/api/v1/payments/?payment_method=CASH')
        self.assertEqual(self._numbers(r, 'payment_number'), ['PAY-DC-2'])

    # expenses
    def test_expense_status_filter(self):
        r = self.client.get('/api/v1/expenses/?status=APPROVED')
        self.assertEqual(self._numbers(r, 'expense_number'), [f'EXP-DC-{self.co.pk}-1'])

    def test_expense_invalid_status_is_400(self):
        r = self.client.get('/api/v1/expenses/?status=nope')
        self.assertEqual(r.status_code, 400)

    def test_expense_category_filter(self):
        r = self.client.get('/api/v1/expenses/?category=TOLLS')
        self.assertEqual(self._numbers(r, 'expense_number'), [f'EXP-DC-{self.co.pk}-2'])

    def test_expense_date_range_filter(self):
        since = (date.today() - timedelta(days=10)).isoformat()
        r = self.client.get(f'/api/v1/expenses/?expense_date__gte={since}')
        self.assertEqual(
            self._numbers(r, 'expense_number'),
            [f'EXP-DC-{self.co.pk}-1', f'EXP-DC-{self.co.pk}-3'],
        )
        until = (date.today() - timedelta(days=30)).isoformat()
        r = self.client.get(f'/api/v1/expenses/?expense_date__lte={until}')
        self.assertEqual(self._numbers(r, 'expense_number'), [f'EXP-DC-{self.co.pk}-2'])

    def test_expense_bad_date_is_400(self):
        r = self.client.get('/api/v1/expenses/?expense_date__gte=not-a-date')
        self.assertEqual(r.status_code, 400)


class FleetOverviewHonestyTests(_Base):
    """A company with no loads and no expenses must not be shown a margin,
    an improvement %, a cost per km or a 'margin up 2.3%' banner."""

    def test_no_invented_numbers_without_data(self):
        r = self.client.get('/api/v1/fleet/overview/')
        self.assertEqual(r.status_code, 200)
        cards = {c['id']: c for c in r.data['kpi_cards']}

        margin = cards['avg_margin_per_vehicle']
        self.assertIsNone(margin['raw_value'])
        self.assertIsNone(margin['value'])
        self.assertIsNone(margin['trend'])
        self.assertEqual(margin['data_status'], 'insufficient_data')

        cpk = cards['fleet_cost_per_km']
        self.assertIsNone(cpk['raw_value'])
        self.assertIsNone(cpk['value'])
        self.assertEqual(cpk['data_status'], 'insufficient_data')

        banner = r.data['banner']['message']
        self.assertNotIn('2.3', banner)
        self.assertNotIn('route pairing', banner)
        self.assertNotIn('idling', banner)

        flat = str(r.data)
        for invented in ('7266.67', '7,266.67', '6500', '11.8', '12.0'):
            self.assertNotIn(invented, flat)

    def test_real_margin_trend_is_signed(self):
        """With real data the trend is computed, and a fall reads as a fall."""
        from core.models import Load
        from django.utils import timezone
        cust = _customer(self.co, 'DC Fleet Cust')
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month = month_start - timedelta(days=5)

        def load(n, amount, created):
            ld = Load.objects.create(
                company=self.co, load_number=f'LOAD-DC-{n}', customer=cust,
                pickup_location='a', pickup_city='JHB', pickup_state='GP', pickup_zip='1',
                pickup_date=now, delivery_location='b', delivery_city='DBN',
                delivery_state='KZN', delivery_zip='2', delivery_date=now,
                cargo_description='x', weight=Decimal('1000'), rate=Decimal(amount), total_amount=Decimal(amount),
                status='DELIVERED',
            )
            Load.objects.filter(pk=ld.pk).update(created_at=created, total_amount=Decimal(amount))

        load(1, '1000.00', last_month)
        load(2, '800.00', now)
        r = self.client.get('/api/v1/fleet/overview/')
        margin = {c['id']: c for c in r.data['kpi_cards']}['avg_margin_per_vehicle']
        self.assertEqual(margin['raw_value'], 800.0)
        self.assertEqual(margin['trend']['value'], -20.0)
        self.assertEqual(margin['trend']['direction'], 'down')
        self.assertIn('-20.0%', margin['trend']['label'])
        self.assertIn('down 20.0%', r.data['banner']['message'])


class SignalsHonestyTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        vt = VehicleType.objects.create(
            name='DCTruck', capacity=Decimal('20000.00'),
            max_distance=Decimal('2000.00'), base_rate=Decimal('15.00'),
        )
        for i in range(2):
            Vehicle.objects.create(
                company=cls.co, vin=f'DCVIN{i}', plate=f'DC{i}GP', vehicle_type=vt,
                make='Merc', model='Actros', year=2020, type='Truck',
                capacity=Decimal('20000.00'), fuel_type='Diesel', status='AVAILABLE',
            )
        cust = _customer(cls.co, 'DC Signals')
        Invoice.objects.create(
            company=cls.co, customer=cust, invoice_number='INV-DC-S1',
            due_date=date.today() + timedelta(days=30), subtotal=Decimal('1000.00'), status='SENT',
        )

    def _bodies(self):
        r = self.client.get('/api/v1/dashboard/signals/')
        self.assertEqual(r.status_code, 200)
        signals = r.data if isinstance(r.data, list) else r.data.get('signals', r.data)
        return {s['title']: s['body'] for s in signals}

    def test_idle_signal_keeps_fact_drops_invented_loss(self):
        bodies = self._bodies()
        idle = next(b for t, b in bodies.items() if 'Idle' in t)
        self.assertIn('available with no assigned load', idle)
        self.assertNotIn('revenue loss', idle.lower())
        self.assertNotIn('/day', idle)

    def test_fast_pay_signal_has_no_invented_fee_or_timing(self):
        Invoice.objects.filter(invoice_number='INV-DC-S1').update(early_pay_eligible=True)
        bodies = self._bodies()
        fp = next(b for t, b in bodies.items() if t.startswith('Fast Pay'))
        self.assertIn('R 1,150', fp)  # real figure (subtotal + 15% VAT) is kept
        self.assertNotIn('fee', fp.lower())
        self.assertNotIn('4 hours', fp)

    def test_fast_pay_fallback_signal_has_no_invented_fee(self):
        Invoice.objects.filter(invoice_number='INV-DC-S1').update(early_pay_eligible=False)
        bodies = self._bodies()
        fp = next(b for t, b in bodies.items() if t.startswith('Fast Pay'))
        self.assertIn('awaiting payment', fp)
        self.assertNotIn('fee', fp.lower())
        self.assertNotIn('Eligible', fp)


class BriefingExpenseTests(_Base):
    def test_only_approved_expenses_count_as_spend(self):
        from core.services.llm_insights import build_company_metrics
        today = date.today()
        Expense.objects.bulk_create([
            _expense(self.co, 'a', status='APPROVED', amount='100.00', expense_date=today),
            _expense(self.co, 'p', status='PENDING', amount='40.00', expense_date=today),
            _expense(self.co, 'r', status='REJECTED', amount='13.00', expense_date=today),
        ])
        m = build_company_metrics(self.co, today.replace(day=1), today)
        self.assertEqual(m['expenses_period'], 100.0)


class IntelligenceErrorTests(_Base):
    def test_recommendation_failure_is_not_200_empty(self):
        with mock.patch(
            'core.views_integrations.IntelligenceService.generate_recommendations',
            side_effect=RuntimeError('boom'),
        ):
            r = self.client.get('/api/v1/dashboard/insights/')
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data['data_status'], 'error')
        self.assertIn('error', r.data)
        self.assertNotIn('boom', str(r.data))  # no exception text leaked

    def test_recommendation_success_unchanged(self):
        with mock.patch(
            'core.views_integrations.IntelligenceService.generate_recommendations',
            return_value=[{'type': 'CASH', 'severity': 'HIGH', 'title': 't'}],
        ):
            r = self.client.get('/api/v1/dashboard/insights/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['total'], 1)
        self.assertIn('recommendations', r.data)

    def test_cashflow_failure_is_not_200_zeros(self):
        with mock.patch(
            'core.views_integrations.CashFlowForecastService.forecast_cashflow',
            side_effect=RuntimeError('boom'),
        ):
            r = self.client.get('/api/v1/dashboard/cashflow/')
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data['data_status'], 'error')
        self.assertNotIn('summary', r.data)  # no zero summary a UI could render
        self.assertNotIn('boom', str(r.data))

    def test_cashflow_success_unchanged(self):
        r = self.client.get('/api/v1/dashboard/cashflow/?days=14')
        self.assertEqual(r.status_code, 200)
        self.assertIn('forecast', r.data)
        self.assertIn('summary', r.data)
        self.assertEqual(r.data['period_days'], 14)


class SchemaTests(TestCase):
    def test_openapi_schema_builds(self):
        r = APIClient().get('/api/schema/')
        self.assertEqual(r.status_code, 200)

    def test_import_views_keep_their_columns(self):
        from core.services.bulk_import import CUSTOMER_COLUMNS, VEHICLE_COLUMNS
        from core import views_import as v
        self.assertIs(v.CustomerImportValidateView.import_columns, CUSTOMER_COLUMNS)
        self.assertIs(v.CustomerImportCommitView.import_columns, CUSTOMER_COLUMNS)
        self.assertIs(v.VehicleImportValidateView.import_columns, VEHICLE_COLUMNS)
        self.assertIs(v.VehicleImportCommitView.import_columns, VEHICLE_COLUMNS)


class ThrottlePolicyTests(_Base):
    def test_read_navigation_is_not_throttled_at_60_per_minute(self):
        # An Overview cold load is ~16 GETs; Insights/Reports page through
        # five lists. 90 GETs in a minute is ordinary use and must pass.
        for i in range(90):
            r = self.client.get('/api/v1/notifications/')
            self.assertEqual(r.status_code, 200, f'request {i + 1} was throttled')

    def test_policy_rates(self):
        from django.conf import settings
        rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
        self.assertEqual(rates['user_read'], '600/minute')
        self.assertEqual(rates['user_write'], '120/minute')
        # Auth limits untouched.
        self.assertEqual(rates['anon'], '20/minute')
        self.assertEqual(rates['login'], '5/minute')
        self.assertEqual(rates['otp_verify'], '10/minute')
        self.assertEqual(rates['otp_resend'], '3/minute')
        self.assertEqual(rates['handoff'], '10/minute')

    def test_write_throttle_only_counts_unsafe_methods(self):
        from rest_framework.test import APIRequestFactory
        from core.throttling import UserReadRateThrottle, UserWriteRateThrottle
        f = APIRequestFactory()
        get, post = f.get('/x'), f.post('/x')
        for req in (get, post):
            req.user = self.user
        write, read = UserWriteRateThrottle(), UserReadRateThrottle()
        with mock.patch.object(UserWriteRateThrottle, 'get_cache_key') as w_key, \
                mock.patch.object(UserReadRateThrottle, 'get_cache_key') as r_key:
            self.assertTrue(write.allow_request(get, None))
            self.assertTrue(read.allow_request(post, None))
            w_key.assert_not_called()
            r_key.assert_not_called()

    def test_writes_are_limited(self):
        from core.throttling import UserWriteRateThrottle
        from rest_framework.test import APIRequestFactory
        t = UserWriteRateThrottle()
        t.rate = '3/minute'
        t.num_requests, t.duration = t.parse_rate(t.rate)
        f = APIRequestFactory()
        results = []
        for _ in range(4):
            req = f.post('/x')
            req.user = self.user
            results.append(t.allow_request(req, None))
        self.assertEqual(results, [True, True, True, False])
