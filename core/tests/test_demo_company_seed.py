"""Consistency tests for the fictional demo company seed (Karoo Line Logistics).

The seed backs website screenshots and the public "Open the demo" login, so
its books have to add up the way the app's own screens add them up, it must
never borrow a real company's name, and it must never touch another tenant.
"""
import os
import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db.models import Count, Sum
from django.test import TestCase
from django.utils import timezone

from core.models import (
    ActivityEvent, Company, Customer, Driver, Expense, Invoice, Load, Notification,
    Payment, Quote, Settlement, Trip, User, Vehicle, VehicleLog, VehicleType,
)
from core.services import demo_seed, demo_seed_data

# Real South African (and multinational) brands a fictional demo must never
# name — checked as whole words against every customer, vendor and the company.
REAL_BRAND_DENYLIST = [
    'shoprite', 'checkers', 'woolworths', 'pick n pay', 'spar', 'massmart', 'makro', 'game stores',
    'coca-cola', 'coca cola', 'tiger brands', 'imperial', 'bidvest', 'barloworld', 'sasol', 'eskom',
    'transnet', 'unitrans', 'super group', 'value logistics', 'rtt', 'dsv', 'afrox', 'build it', 'cashbuild',
    'italtile', 'ctm', 'builders warehouse', 'pep', 'clover', 'sappi', 'mondi', 'rcl foods', 'astral',
    'pioneer foods', 'sab', 'ab inbev', 'distell', 'heineken', 'nestle', 'unilever', 'illovo', 'tongaat hulett',
    'senwes', 'afgri', 'vkb', 'omnia', 'arcelormittal', 'ppc', 'afrisam', 'lafarge', 'mr price', 'dis-chem',
    'clicks', 'anglo american', 'glencore', 'exxaro', 'harmony gold', 'sibanye', 'implats', 'amplats', 'kumba',
    'engen', 'shell', 'totalenergies', 'bp', 'caltex', 'astron', 'santam', 'old mutual', 'discovery', 'outsurance',
    'standard bank', 'absa', 'fnb', 'nedbank', 'capitec', 'investec', 'cartrack', 'netstar', 'tracker',
    'bridgestone', 'goodyear', 'supa quick', 'tiger wheel', 'hi-q', 'protea hotels', 'rainbow chicken',
    'county fair', 'fair cape', 'parmalat', 'lancewood', 'simba', 'bakers', 'albany', 'sasko', 'iwisa',
]


def _all_fictional_names():
    names = [demo_seed_data.DEMO_COMPANY_PROFILE['company_name'], demo_seed_data.DEMO_COMPANY_PROFILE['bank_name']]
    names += [c['name'] for c in demo_seed_data.DEMO_CUSTOMERS]
    names += list(demo_seed_data.VENDORS.values())
    return names


class DemoSeedDataTests(TestCase):
    """Static checks on the fixture itself — no database needed."""

    def test_no_real_brand_names(self):
        for name in _all_fictional_names():
            lowered = name.lower()
            for brand in REAL_BRAND_DENYLIST:
                self.assertIsNone(
                    re.search(rf'(?<![a-z]){re.escape(brand)}(?![a-z])', lowered),
                    f'{name!r} contains the real brand {brand!r}',
                )

    def test_fixture_sizes_match_the_brief(self):
        self.assertTrue(12 <= len(demo_seed_data.DEMO_VEHICLES) <= 18)
        self.assertEqual(len(demo_seed_data.DEMO_DRIVERS), 14)
        self.assertTrue(25 <= len(demo_seed_data.DEMO_CUSTOMERS) <= 35)
        self.assertEqual(len({c['name'] for c in demo_seed_data.DEMO_CUSTOMERS}), len(demo_seed_data.DEMO_CUSTOMERS))
        self.assertEqual(len({v['plate'] for v in demo_seed_data.DEMO_VEHICLES}), len(demo_seed_data.DEMO_VEHICLES))

    def test_identifiers_are_obviously_dummy(self):
        profile = demo_seed_data.DEMO_COMPANY_PROFILE
        self.assertEqual(profile['vat_number'], '4000000000')
        self.assertIn('000000', profile['registration_number'])
        self.assertIn('fictional', profile['bank_name'].lower())

    def test_no_password_in_source(self):
        source = open(demo_seed.__file__).read()
        self.assertNotIn('TruckDemo', source)
        self.assertFalse(hasattr(demo_seed, 'DEMO_USER_PASSWORD'))


class DemoCompanySeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A real tenant with its own data — the seed must never touch it.
        cls.other = Company.objects.create(company_name='Other Tenant Haulage')
        cls.other_user = User.objects.create_user(username='owner@other.test', email='owner@other.test', password='x')
        cls.other_user.company = cls.other
        cls.other_user.save()
        cls.other_customer = Customer.objects.create(company=cls.other, name='Other Customer', email='ap@other.test')
        cls.other_load = Load.objects.create(
            company=cls.other, load_number='OTHER-1', customer=cls.other_customer,
            pickup_location='A', pickup_city='A', pickup_state='GP', pickup_zip='1', pickup_date=timezone.now(),
            delivery_location='B', delivery_city='B', delivery_state='GP', delivery_zip='2',
            delivery_date=timezone.now() + timedelta(hours=5), cargo_description='x', weight=1,
            rate=Decimal('1000'), total_amount=Decimal('1000'), status='PENDING',
        )
        cls.other_snapshot = cls._snapshot(cls.other)
        cls.other_notifications = Notification.objects.filter(user=cls.other_user).count()
        cls.other_events = ActivityEvent.objects.filter(company=cls.other).count()

        with mock.patch.dict(os.environ, {demo_seed.DEMO_PASSWORD_ENV: 'local-only-test-pw'}):
            cls.summary = demo_seed.seed_demo_company()
        cls.company = cls.summary['company']

    @staticmethod
    def _snapshot(company):
        return {
            model.__name__: model.objects.filter(company=company).count()
            for model in (Customer, Load, Quote, Invoice, Payment, Expense, Vehicle, Driver, VehicleType, Settlement)
        }

    # -- login ---------------------------------------------------------------
    def test_login_uses_env_password_and_is_admin(self):
        user = User.objects.get(username=demo_seed.DEMO_USER_EMAIL)
        self.assertEqual(user.company, self.company)
        self.assertEqual(user.role, 'ADMIN')
        self.assertTrue(user.check_password('local-only-test-pw'))
        self.assertTrue(self.summary['user_created'])
        self.assertIsNone(self.summary['user_password'])  # chosen via env, so nothing to print

    def test_generated_password_is_returned_once_and_never_rotated(self):
        User.objects.filter(username=demo_seed.DEMO_USER_EMAIL).delete()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(demo_seed.DEMO_PASSWORD_ENV, None)
            first = demo_seed.seed_demo_company()
            second = demo_seed.seed_demo_company()
        self.assertTrue(first['user_password'])
        self.assertIsNone(second['user_password'])
        self.assertTrue(User.objects.get(username=demo_seed.DEMO_USER_EMAIL).check_password(first['user_password']))

    # -- shape ---------------------------------------------------------------
    def test_company_profile_is_the_fictional_demo(self):
        self.assertTrue(self.company.is_demo)
        self.assertEqual(self.company.company_name, 'Karoo Line Logistics (Pty) Ltd')
        self.assertEqual(self.company.subscription_status, 'active')
        self.assertEqual(Company.objects.filter(is_demo=True).count(), 1)

    def test_fleet_drivers_customers(self):
        self.assertEqual(Vehicle.objects.filter(company=self.company).count(), len(demo_seed_data.DEMO_VEHICLES))
        drivers = Driver.objects.filter(company=self.company)
        self.assertEqual(drivers.count(), 14)
        self.assertEqual(drivers.filter(status='INACTIVE').count(), 2)
        soon = timezone.localdate() + timedelta(days=45)
        self.assertGreaterEqual(drivers.filter(license_expiry__lte=soon).count(), 2)
        customers = Customer.objects.filter(company=self.company)
        self.assertTrue(25 <= customers.count() <= 35)
        for email in customers.values_list('email', flat=True):
            self.assertTrue(email.endswith('.example.com'), email)
        for user in User.objects.filter(company=self.company).exclude(username=demo_seed.DEMO_USER_EMAIL):
            self.assertTrue(user.email.endswith('.example.com'), user.email)
            self.assertFalse(user.has_usable_password())
            self.assertFalse(user.is_active)  # never notified / e-mailed

    def test_twelve_months_of_history_ending_today(self):
        loads = Load.objects.filter(company=self.company)
        today = timezone.localdate()
        first = loads.order_by('pickup_date').first().pickup_date.date()
        self.assertLessEqual(first, today - timedelta(days=365))
        self.assertTrue(loads.filter(pickup_date__date=today - timedelta(days=1)).exists()
                        or loads.filter(pickup_date__date=today).exists())
        by_status = dict(loads.values_list('status').annotate(n=Count('id')))
        self.assertGreater(by_status['INVOICED'], 0.9 * loads.count())
        for status in ('IN_TRANSIT', 'ASSIGNED'):
            self.assertIn(status, by_status)

    def test_stale_open_loads_are_deliberate_and_rare(self):
        now = timezone.now()
        today = timezone.localdate()
        stale = [ld for ld in Load.objects.filter(company=self.company, status__in=['PENDING', 'ASSIGNED', 'LOADING', 'IN_TRANSIT'])
                 if ld.delivery_date.date() < today or (now - ld.pickup_date).days > 30]
        self.assertTrue(1 <= len(stale) <= 2, stale)

    # -- money ---------------------------------------------------------------
    def test_every_payment_has_the_company_and_matches_its_invoice(self):
        payments = Payment.objects.filter(invoice__company=self.company)
        self.assertGreater(payments.count(), 0)
        self.assertFalse(payments.exclude(company=self.company).exists())
        self.assertFalse(Payment.objects.filter(company__isnull=True).exists())
        paid_by_invoice = dict(payments.values_list('invoice').annotate(s=Sum('amount')))
        for inv in Invoice.objects.filter(company=self.company):
            self.assertEqual(inv.total_amount, inv.subtotal + inv.vat_amount - inv.discount, inv.invoice_number)
            self.assertEqual(inv.vat_amount, (inv.subtotal * Decimal('0.15')).quantize(Decimal('0.01')))
            self.assertEqual(inv.balance, inv.total_amount - inv.paid_amount, inv.invoice_number)
            self.assertEqual(paid_by_invoice.get(inv.pk, Decimal('0')), inv.paid_amount, inv.invoice_number)
            self.assertEqual(inv.customer.company_id, self.company.pk)
            if inv.status == 'PAID':
                self.assertEqual(inv.balance, 0)
                self.assertIsNotNone(inv.paid_at)
            if inv.load_id:
                self.assertEqual(inv.subtotal, inv.load.total_amount)
                self.assertEqual(inv.issue_date, timezone.localtime(inv.load.actual_delivered_at).date())

    def test_invoiced_loads_have_exactly_one_invoice_and_a_completed_trip(self):
        invoiced = Load.objects.filter(company=self.company, status='INVOICED').annotate(n=Count('invoices'))
        self.assertFalse(invoiced.exclude(n=1).exists())
        self.assertFalse(Trip.objects.filter(load__in=invoiced).exclude(status='COMPLETED').exists())

    def test_debtors_book_is_realistic(self):
        today = timezone.localdate()
        open_invoices = Invoice.objects.filter(company=self.company, balance__gt=0,
                                               status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE'])
        buckets = {'1-30': 0, '31-60': 0, '61-90': 0, '90+': 0}
        for inv in open_invoices:
            late = (today - inv.due_date).days
            if late > 90:
                buckets['90+'] += 1
            elif late > 60:
                buckets['61-90'] += 1
            elif late > 30:
                buckets['31-60'] += 1
            elif late > 0:
                buckets['1-30'] += 1
        for name, count in buckets.items():
            self.assertGreater(count, 0, f'empty ageing bucket {name}')
        self.assertTrue(Invoice.objects.filter(company=self.company, status='PARTIALLY_PAID').exists())
        # The "stopped paying" customer: always paid early, now 30+ days late.
        riverbend = Customer.objects.get(company=self.company, name='Riverbend Dairy Distributors')
        paid = Invoice.objects.filter(customer=riverbend, status='PAID')
        self.assertGreaterEqual(paid.count(), 2)
        self.assertFalse([i for i in paid if i.paid_at.date() > i.due_date])
        self.assertTrue(Invoice.objects.filter(customer=riverbend, status='OVERDUE',
                                               due_date__lt=today - timedelta(days=30)).exists())

    def test_margin_is_believable_and_two_lanes_lose_money(self):
        today = timezone.localdate()
        since = today - timedelta(days=365)
        revenue = Invoice.objects.filter(company=self.company, issue_date__gte=since).exclude(
            status__in=['DRAFT', 'CANCELLED']).aggregate(s=Sum('subtotal'))['s']
        costs = Expense.objects.filter(company=self.company, expense_date__gte=since).exclude(
            status='REJECTED').aggregate(s=Sum('amount'))['s']
        margin = (revenue - costs) / revenue * 100
        self.assertTrue(10 <= margin <= 25, f'overall margin {margin:.1f}%')

        from core.services.reports import margin_by_lane
        lanes = margin_by_lane(self.company)
        rows = lanes['lanes'] if isinstance(lanes, dict) else lanes
        losing = {r['lane'] for r in rows if r['margin_pct'] is not None and r['margin_pct'] < 0}
        self.assertEqual(losing, {'Durban → Gqeberha', 'Johannesburg → Polokwane'})

    def test_quote_win_rate(self):
        quotes = Quote.objects.filter(company=self.company)
        counts = dict(quotes.values_list('status').annotate(n=Count('id')))
        closed = counts.get('ACCEPTED', 0) + counts.get('DECLINED', 0) + counts.get('EXPIRED', 0)
        win = counts['ACCEPTED'] / closed * 100
        self.assertTrue(35 <= win <= 55, f'win rate {win:.1f}%')
        for status in ('DRAFT', 'SENT', 'ACCEPTED', 'DECLINED', 'EXPIRED'):
            self.assertIn(status, counts)
        self.assertEqual(quotes.filter(token='').count(), 0)

    def test_tolls_and_diesel_come_from_the_reference_tables(self):
        from core.management.commands.seed_toll_data import _PLAZA_DATA
        n3 = sum(d['tariff_class_5'] for d in _PLAZA_DATA if d['route'] == 'N3')
        trip = Trip.objects.filter(load__company=self.company, status='COMPLETED', load__pickup_city='Johannesburg',
                                   load__delivery_city='Durban', vehicle__vehicle_type__sanral_toll_class=4).first()
        self.assertEqual(trip.actual_toll_cost, n3)
        fuel = Expense.objects.get(trip=trip, category='FUEL')
        month = trip.load.pickup_date.astimezone(timezone.get_current_timezone()).date()
        price = demo_seed._diesel_by_month(month, month)[(month.year, month.month)]
        self.assertEqual(fuel.amount, (trip.actual_fuel_litres * price).quantize(Decimal('0.01')))

    def test_expenses_include_pending_approvals_and_every_category(self):
        expenses = Expense.objects.filter(company=self.company)
        cats = set(expenses.values_list('category', flat=True))
        for cat in ('FUEL', 'TOLLS', 'MAINTENANCE', 'DRIVER_COST', 'INSURANCE', 'OVERHEAD'):
            self.assertIn(cat, cats)
        self.assertTrue(expenses.filter(status='PENDING', expense_date__lt=timezone.localdate() - timedelta(days=7)).exists())
        self.assertTrue(VehicleLog.objects.filter(vehicle__company=self.company).exists())

    # -- isolation & idempotency -----------------------------------------------
    def test_nothing_leaks_into_other_companies(self):
        self.assertEqual(self._snapshot(self.other), self.other_snapshot)
        self.assertFalse(Load.objects.filter(company=self.company).exclude(customer__company=self.company).exists())
        self.assertFalse(Trip.objects.filter(load__company=self.company).exclude(vehicle__company=self.company).exists())
        self.assertFalse(Expense.objects.filter(company=self.company, vehicle__isnull=False)
                         .exclude(vehicle__company=self.company).exists())
        self.assertEqual(Notification.objects.filter(user=self.other_user).count(), self.other_notifications)
        self.assertEqual(ActivityEvent.objects.filter(company=self.other).count(), self.other_events)

    def test_rerun_is_idempotent(self):
        before = self._snapshot(self.company)
        again = demo_seed.seed_demo_company()
        self.assertFalse(again['history_created'])
        self.assertEqual(self._snapshot(self.company), before)
        self.assertEqual(User.objects.filter(username=demo_seed.DEMO_USER_EMAIL).count(), 1)

    def test_reset_rebuilds_only_the_demo_company_and_keeps_the_login(self):
        user = User.objects.get(username=demo_seed.DEMO_USER_EMAIL)
        out = StringIO()
        call_command('seed_demo_company', '--reset', stdout=out)
        self.assertIn('history generated', out.getvalue())
        self.assertIn('password unchanged', out.getvalue())
        user.refresh_from_db()
        self.assertTrue(user.check_password('local-only-test-pw'))
        self.assertTrue(Company.objects.filter(pk=self.company.pk, is_demo=True).exists())
        self.assertEqual(self._snapshot(self.other), self.other_snapshot)
        self.assertTrue(Load.objects.filter(pk=self.other_load.pk).exists())
        self.assertGreater(Invoice.objects.filter(company=self.company).count(), 500)
