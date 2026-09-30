"""Consistency tests for the fictional demo company seed (Karoo Line Logistics).

The seed backs website screenshots and the public "Open the demo" login, so
its books have to add up the way the app's own screens add them up, it must
never borrow a real company's name, and it must never touch another tenant.
"""
import os
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db.models import Count, Sum
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    ActivityEvent, Company, CopilotConversation, CopilotMessage, Customer, Driver, Expense, Invoice, Load,
    Notification, Payment, Quote, Settlement, Trip, User, Vehicle, VehicleLog, VehicleType,
)

_MEDIA = tempfile.mkdtemp(prefix='demo-seed-media-')
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


@override_settings(MEDIA_ROOT=_MEDIA)
class DemoCompanySeedTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

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
        cls.today = timezone.localdate()
        cls.window_start = demo_seed._window_start(cls.today)

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

    def test_demo_is_on_an_active_paid_plan_that_can_never_be_charged(self):
        company = Company.objects.get(pk=self.company.pk)
        self.assertEqual(company.subscription_plan, 'pro')  # not "Free plan" in the sidebar
        self.assertEqual(company.subscription_status, 'active')
        self.assertFalse(company.cancel_at_period_end)
        self.assertGreater(company.next_billing_date, self.today)
        # No card on file: the monthly sweep skips it, so no Paystack call, ever.
        self.assertFalse(company.paystack_authorization_code)
        from core.services.subscription_billing import charge_monthly_subscription_fee
        company.next_billing_date = self.today
        with mock.patch('core.services.paystack.charge_authorization') as charge:
            result = charge_monthly_subscription_fee(company)
        charge.assert_not_called()
        self.assertFalse(result['charged'])
        # The billing status the sidebar reads.
        from core.serializers_billing import BillingStatusSerializer
        data = BillingStatusSerializer(Company.objects.get(pk=self.company.pk)).data
        self.assertEqual((data['subscription_plan'], data['subscription_status']), ('pro', 'active'))

    def test_invoices_carry_fictional_bank_details(self):
        from core.services.payment_details import public_payment_details
        details = public_payment_details(self.company)
        self.assertIsNotNone(details)
        self.assertIn('fictional', details['bank_name'].lower())

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
        today = self.today
        in_window = loads.filter(pickup_date__date__gte=self.window_start)
        first = in_window.order_by('pickup_date').first().pickup_date.astimezone(timezone.get_current_timezone()).date()
        self.assertLessEqual((first - self.window_start).days, 4)
        self.assertEqual(self.window_start.day, 1)
        self.assertEqual((today.year * 12 + today.month) - (self.window_start.year * 12 + self.window_start.month), 11)
        self.assertTrue(loads.filter(pickup_date__date=today - timedelta(days=1)).exists()
                        or loads.filter(pickup_date__date=today).exists())
        by_status = dict(loads.values_list('status').annotate(n=Count('id')))
        self.assertGreater(by_status['INVOICED'], 0.9 * loads.count())
        self.assertTrue(set(by_status) & {'IN_TRANSIT', 'LOADING', 'ASSIGNED'}, by_status)

    def test_every_list_stays_under_the_dashboard_row_cap(self):
        # The dashboard reads each list through fetchAllPages (50 pages x 20):
        # at 1 000 rows or more Home, P&L, Debtors, VAT and Expenses go partial.
        cap = demo_seed.FRONTEND_ROW_CAP
        self.assertEqual(cap, 1000)
        counts = {
            'quotes': Quote.objects.filter(company=self.company).count(),
            'loads': Load.objects.filter(company=self.company).count(),
            'invoices': Invoice.objects.filter(company=self.company).count(),
            'payments': Payment.objects.filter(company=self.company).count(),
            'expenses': Expense.objects.filter(company=self.company).count(),
            'trips': Trip.objects.filter(load__company=self.company).count(),
            'customers': Customer.objects.filter(company=self.company).count(),
            'vehicles': Vehicle.objects.filter(company=self.company).count(),
            'drivers': Driver.objects.filter(company=self.company).count(),
        }
        for kind, n in counts.items():
            self.assertLess(n, cap, f'{kind}: {n} rows')
        # Headroom so a busy month never tips a list over the cap.
        self.assertLessEqual(counts['loads'], 960, counts)
        self.assertLessEqual(counts['invoices'], 930, counts)
        self.assertLessEqual(counts['payments'], 880, counts)
        self.assertLessEqual(counts['expenses'], 900, counts)
        self.assertLessEqual(counts['quotes'], 950, counts)
        # About five loads per truck a month.
        in_window = Load.objects.filter(company=self.company, pickup_date__date__gte=self.window_start).count()
        per_truck_month = in_window / len(demo_seed_data.DEMO_VEHICLES) / 12
        self.assertTrue(3.8 <= per_truck_month <= 5.5, per_truck_month)

    def test_nothing_is_dated_after_today(self):
        today, now = self.today, timezone.now()
        c = self.company
        self.assertFalse(Load.objects.filter(company=c, pickup_date__date__gt=today).exists())
        self.assertFalse(Load.objects.filter(company=c, delivery_date__date__gt=today).exists())
        self.assertFalse(Load.objects.filter(company=c, actual_delivered_at__gt=now).exists())
        self.assertFalse(Load.objects.filter(company=c, created_at__gt=now).exists())
        self.assertFalse(Invoice.objects.filter(company=c, issue_date__gt=today).exists())
        self.assertFalse(Invoice.objects.filter(company=c, created_at__gt=now).exists())
        self.assertFalse(Payment.objects.filter(company=c, payment_date__gt=today).exists())
        self.assertFalse(Expense.objects.filter(company=c, expense_date__gt=today).exists())
        self.assertFalse(Quote.objects.filter(company=c, created_at__gt=now).exists())
        self.assertFalse(Trip.objects.filter(load__company=c, start_time__gt=now).exists())
        self.assertFalse(Settlement.objects.filter(company=c, end_date__gt=today).exists())
        self.assertFalse(CopilotMessage.objects.filter(user__company=c, created_at__gt=now).exists())

    def test_only_an_opening_debtors_book_predates_the_window(self):
        c, start = self.company, self.window_start
        # Home compares with "the prior 12 months": nothing there to compare.
        self.assertFalse(Payment.objects.filter(company=c, payment_date__lt=start).exists())
        self.assertFalse(Expense.objects.filter(company=c, expense_date__lt=start).exists())
        self.assertFalse(Quote.objects.filter(company=c, created_at__date__lt=start).exists())
        opening = Invoice.objects.filter(company=c, issue_date__lt=start)
        self.assertTrue(opening.exists())
        for inv in opening:
            self.assertFalse(Payment.objects.filter(invoice=inv, payment_date__lt=start).exists())
            self.assertGreaterEqual(inv.issue_date, start - timedelta(days=demo_seed.RUN_IN_DAYS + 1))

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
        since = self.window_start
        revenue = Invoice.objects.filter(company=self.company, issue_date__gte=since).exclude(
            status__in=['DRAFT', 'CANCELLED']).aggregate(s=Sum('subtotal'))['s']
        costs = Expense.objects.filter(company=self.company, expense_date__gte=since,
                                       status='APPROVED').aggregate(s=Sum('amount'))['s']
        margin = (revenue - costs) / revenue * 100
        # Home's net margin is on the cash basis: receipts excl. VAT.
        cash = Payment.objects.filter(company=self.company, payment_date__gte=since).aggregate(s=Sum('amount'))['s']
        cash_excl = cash / Decimal('1.15')
        cash_margin = (cash_excl - costs) / cash_excl * 100
        # Target 12-20% on both bases; a little tolerance because the history
        # is generated relative to whatever day the tests run.
        self.assertTrue(10 <= margin <= 22, f'invoice-basis margin {margin:.1f}%')
        self.assertTrue(10 <= cash_margin <= 22, f'cash-basis margin {cash_margin:.1f}%')

        from core.services.reports import margin_by_lane
        lanes = margin_by_lane(self.company)
        rows = lanes['lanes'] if isinstance(lanes, dict) else lanes
        losing = {r['lane'] for r in rows if r['margin_pct'] is not None and r['margin_pct'] < 0}
        self.assertEqual(losing, {'Durban → Gqeberha', 'Johannesburg → Polokwane'})

    def test_collection_rate_is_realistic(self):
        # Most of the book is collected within 60 days of invoicing.
        cutoff = self.today - timedelta(days=60)
        invoices = Invoice.objects.filter(company=self.company, issue_date__gte=self.window_start,
                                          issue_date__lte=cutoff).exclude(status__in=['DRAFT', 'CANCELLED'])
        billed = collected = Decimal('0')
        for inv in invoices:
            billed += inv.total_amount
            collected += sum((p.amount for p in inv.payments.all()
                              if (p.payment_date - inv.issue_date).days <= 60), Decimal('0'))
        rate = collected / billed * 100
        self.assertTrue(78 <= rate <= 93, f'collected within 60 days: {rate:.1f}%')
        # This month's invoices already partly paid (the Invoices "Collected" tile).
        month = Invoice.objects.filter(company=self.company, issue_date__gte=self.today.replace(day=1)).exclude(
            status__in=['DRAFT', 'CANCELLED'])
        if self.today.day >= 20:
            billed = month.aggregate(s=Sum('total_amount'))['s']
            paid = month.aggregate(s=Sum('paid_amount'))['s']
            self.assertGreater(paid / billed, Decimal('0.1'))

    def test_quote_margins_are_believable(self):
        margins = list(Quote.objects.filter(company=self.company).values_list('margin_percentage', flat=True))
        self.assertTrue(margins)
        self.assertTrue(all(Decimal('12') <= m <= Decimal('25') for m in margins), (min(margins), max(margins)))

    def test_every_lane_has_a_sample_worth_ranking(self):
        per_lane = defaultdict(int)
        for pickup, delivery in Load.objects.filter(company=self.company, status='INVOICED').values_list(
                'pickup_city', 'delivery_city'):
            per_lane[(pickup, delivery)] += 1
        self.assertEqual(len(per_lane), len(demo_seed_data.DEMO_LANES))
        self.assertGreaterEqual(min(per_lane.values()), 20, per_lane)

    def test_pod_files_on_most_delivered_loads(self):
        from django.core.files.storage import default_storage
        delivered = Load.objects.filter(company=self.company, status='INVOICED')
        with_pod = delivered.exclude(pod_document='').exclude(pod_document__isnull=True)
        share = with_pod.count() / delivered.count()
        self.assertTrue(0.5 <= share <= 0.7, share)
        sample = with_pod.first()
        self.assertTrue(sample.pod_document.name.startswith(demo_seed.POD_DIR))
        self.assertTrue(default_storage.exists(sample.pod_document.name))
        with default_storage.open(sample.pod_document.name) as fh:
            self.assertEqual(fh.read(8), b'\x89PNG\r\n\x1a\n')
        # The two "no POD on file" stories stay without one.
        self.assertTrue(delivered.filter(pod_received_by='', pod_document='').exists())

    def test_copilot_has_stored_sample_conversations_that_match_the_books(self):
        user = User.objects.get(username=demo_seed.DEMO_USER_EMAIL)
        convs = CopilotConversation.objects.filter(user=user)
        self.assertTrue(2 <= convs.count() <= 3)
        for conv in convs:
            roles = list(conv.messages.order_by('created_at').values_list('role', flat=True))
            self.assertEqual(roles, ['user', 'assistant'])
        debtors = convs.get(messages__content='Who owes me the most right now?')
        answer = debtors.messages.get(role='assistant').content
        owed = defaultdict(Decimal)
        for inv in Invoice.objects.filter(company=self.company, balance__gt=0,
                                          status__in=['SENT', 'VIEWED', 'PARTIALLY_PAID', 'OVERDUE']):
            owed[inv.customer.name] += inv.balance
        top = max(owed, key=owed.get)
        self.assertIn(f'**{top}**', answer)
        self.assertIn(f'R{owed[top]:,.0f}', answer)
        self.assertRegex(answer, r'KL-INV-\d{5}')
        lanes = convs.get(messages__content='Which lane lost money last month?').messages.get(role='assistant').content
        self.assertIn('Durban → Gqeberha', lanes)

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
                                   load__delivery_city='Durban', vehicle__vehicle_type__sanral_toll_class=4,
                                   start_time__date__gte=self.window_start).first()
        self.assertEqual(trip.actual_toll_cost, n3)
        # Fuel is booked as one fuel-card statement per truck a month: the
        # loaded legs' litres plus the empty-running share, at that month's
        # diesel price.
        tz = timezone.get_current_timezone()
        month = trip.start_time.astimezone(tz).date().replace(day=1)
        statement = Expense.objects.get(company=self.company, category='FUEL', vehicle=trip.vehicle,
                                        description__startswith=f'Fuel card statement - {trip.vehicle.plate} - {month:%b %Y}')
        price = demo_seed._diesel_by_month(month, month)[(month.year, month.month)]
        litres = int(re.search(r'\((\d+) L\)', statement.description).group(1))
        self.assertAlmostEqual(float(statement.amount), litres * float(price), delta=float(price) * 1.01)
        loaded = sum((t.actual_fuel_litres or 0) for t in Trip.objects.filter(
            vehicle=trip.vehicle, status='COMPLETED', start_time__date__gte=month,
            start_time__date__lt=(month + timedelta(days=32)).replace(day=1)))
        self.assertGreaterEqual(litres + 1, loaded)
        self.assertLessEqual(litres, loaded * Decimal('1.5') + 900)

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
