"""David's backend list, Sep 2026: figures that disagreed between endpoints.

- A PAID invoice always has paid_at (INV-20260615-96400 dropped out of revenue).
- paid_at is the settling payment's date, not when it was recorded.
- /invoices/stats overdue uses the aging rule (no drafts, balance) and has DSO.
- Aging: due today is current; DSO excludes drafts and is None when unmeasurable.
- dashboard/finance monthly_trend takes a months param.
- admin/overview counts users with no company.
- en-ZA money in text people read.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.formatting import format_number, format_zar
from core.models import Company, Customer, Invoice, Payment
from core.services.aging_service import AgingAnalysisService
from core.services.notify_copy import money
from core.services.payments import record_payment

User = get_user_model()
TODAY = date.today()


def _user(username, company, role='ADMIN', **extra):
    user = User.objects.create_user(username=username, email=f'{username}@dc.test', password='x', **extra)
    user.role = role
    user.company = company
    user.save()
    return user


def _invoice(company, customer, number, *, due, subtotal='1000.00', status='SENT', issue=None):
    inv = Invoice.objects.create(
        company=company, customer=customer, invoice_number=number,
        due_date=due, subtotal=Decimal(subtotal), status=status,
    )
    if issue:
        Invoice.objects.filter(pk=inv.pk).update(issue_date=issue)
        inv.refresh_from_db()
    return inv


class FormattingTests(TestCase):
    def test_en_za_money(self):
        self.assertEqual(format_zar(20505.65), 'R 20 505,65')
        self.assertEqual(format_zar('20505.65', 0), 'R 20 506')
        self.assertEqual(format_zar(-1234.5), '−R 1 234,50')
        self.assertEqual(format_zar(-1234.5, minus='-'), '-R 1 234,50')
        self.assertEqual(format_zar(-0.001), 'R 0,00')  # no "−R 0,00"
        self.assertEqual(format_zar(None), 'R 0,00')
        self.assertEqual(format_number(31.94, 1), '31,9')

    def test_notification_money_fragment(self):
        self.assertEqual(money(Decimal('20505.65')), 'R 20 506')
        self.assertEqual(money(0), '')


class InvoiceFiguresTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='DC Transport')
        cls.user = _user('dc_admin', cls.co)
        cls.customer = Customer.objects.create(
            company=cls.co, name='DC Customer', email='cust@dc.test', phone='', address='',
            city='JHB', state='', zip_code='', credit_score=85, credit_score_source='MANUAL',
        )

    def _pay(self, invoice, amount, payment_date):
        return record_payment(self.co, self.user, {
            'invoice': invoice.id, 'amount': str(amount), 'payment_date': payment_date.isoformat(),
            'payment_method': 'EFT',
        })

    # ---- paid_at ----
    def test_backdated_payment_dates_revenue_by_payment_date(self):
        inv = _invoice(self.co, self.customer, 'INV-DC-1', due=TODAY + timedelta(days=30))
        paid_on = TODAY - timedelta(days=40)
        self._pay(inv, inv.total_amount, paid_on)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'PAID')
        self.assertEqual(timezone.localtime(inv.paid_at).date(), paid_on)

    def test_same_day_payment_uses_now(self):
        inv = _invoice(self.co, self.customer, 'INV-DC-2', due=TODAY + timedelta(days=30))
        before = timezone.now()
        self._pay(inv, inv.total_amount, TODAY)
        inv.refresh_from_db()
        self.assertGreaterEqual(inv.paid_at, before)

    def test_save_stamps_paid_at_when_balance_cleared_outside_record_payment(self):
        inv = _invoice(self.co, self.customer, 'INV-DC-3', due=TODAY + timedelta(days=30))
        inv.paid_amount = inv.total_amount  # e.g. an API edit, import or Xero sync
        inv.save()
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'PAID')
        self.assertIsNotNone(inv.paid_at)

    def test_backfill_invoice_paid_at(self):
        inv = _invoice(self.co, self.customer, 'INV-DC-4', due=TODAY + timedelta(days=30))
        paid_on = TODAY - timedelta(days=20)
        Payment.objects.create(
            company=self.co, invoice=inv, customer=self.customer, payment_number='PAY-DC-4',
            amount=inv.total_amount, payment_date=paid_on, payment_method='EFT',
        )
        # A legacy row: PAID with no paid_at (bypasses save()).
        Invoice.objects.filter(pk=inv.pk).update(status='PAID', paid_amount=inv.total_amount, balance=0, paid_at=None)

        out = StringIO()
        call_command('backfill_invoice_paid_at', stdout=out)
        inv.refresh_from_db()
        self.assertIsNone(inv.paid_at)
        self.assertIn('would backfill 1 invoices', out.getvalue())

        call_command('backfill_invoice_paid_at', '--apply', stdout=StringIO())
        inv.refresh_from_db()
        self.assertEqual(timezone.localtime(inv.paid_at).date(), paid_on)

    # ---- stats vs aging ----
    def test_stats_overdue_matches_aging(self):
        _invoice(self.co, self.customer, 'INV-DC-DRAFT', due=TODAY - timedelta(days=10), status='DRAFT')
        late = _invoice(self.co, self.customer, 'INV-DC-LATE', due=TODAY - timedelta(days=10))
        part = _invoice(self.co, self.customer, 'INV-DC-PART', due=TODAY - timedelta(days=5))
        self._pay(part, Decimal('100.00'), TODAY)
        _invoice(self.co, self.customer, 'INV-DC-TODAY', due=TODAY)  # due today: not overdue
        late.refresh_from_db(); part.refresh_from_db()

        client = APIClient()
        client.force_authenticate(self.user)
        stats = client.get('/api/v1/invoices/stats/').json()
        aging = client.get('/api/v1/invoices/aging/').json()

        expected = float(late.balance + part.balance)
        aged_overdue = sum(aging['summary'][k] for k in ('days_1_30', 'days_31_60', 'days_61_90', 'days_90_plus'))
        self.assertEqual(stats['overdue_count'], 2)
        self.assertAlmostEqual(stats['overdue_amount'], expected, places=2)
        self.assertAlmostEqual(aged_overdue, expected, places=2)
        self.assertEqual(stats['dso'], aging['summary']['dso'])

    def test_past_due_draft_stays_draft(self):
        draft = _invoice(self.co, self.customer, 'INV-DC-D2', due=TODAY - timedelta(days=3), status='DRAFT')
        draft.save()
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'DRAFT')

    def test_dso_none_when_nothing_invoiced_in_window(self):
        _invoice(self.co, self.customer, 'INV-DC-OLD', due=TODAY - timedelta(days=100),
                 issue=TODAY - timedelta(days=130))
        self.assertIsNone(AgingAnalysisService(self.co).calculate_dso())

    def test_dso_ignores_drafts(self):
        _invoice(self.co, self.customer, 'INV-DC-S', due=TODAY + timedelta(days=30))
        _invoice(self.co, self.customer, 'INV-DC-D', due=TODAY + timedelta(days=30), status='DRAFT')
        # One sent invoice of 1150 outstanding against 1150 sales over 90 days.
        self.assertEqual(AgingAnalysisService(self.co).calculate_dso(), 90.0)

    # ---- monthly trend ----
    def test_monthly_trend_months_param(self):
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertEqual(len(client.get('/api/v1/dashboard/finance/').json()['monthly_trend']), 6)
        self.assertEqual(len(client.get('/api/v1/dashboard/finance/?months=12').json()['monthly_trend']), 12)
        self.assertEqual(len(client.get('/api/v1/dashboard/finance/?months=99').json()['monthly_trend']), 24)
        self.assertEqual(client.get('/api/v1/dashboard/finance/?months=x').status_code, 400)


class AdminOverviewUsersTests(TestCase):
    def test_users_without_company_are_counted(self):
        co = Company.objects.create(company_name='Real Co')
        _user('in_co', co)
        _user('no_co', None, role='VIEWER')
        admin = User.objects.create_superuser(username='root_dc', email='root@dc.test', password='x')
        client = APIClient()
        client.force_authenticate(admin)
        before = client.get('/api/v1/admin/overview/').json()
        _user('no_co_2', None, role='VIEWER')
        after = client.get('/api/v1/admin/overview/').json()
        self.assertEqual(after['total_users'], before['total_users'] + 1)
        self.assertEqual(after['users_without_company'], before['users_without_company'] + 1)
        self.assertEqual(before['users_without_company'], User.objects.filter(company__isnull=True).count() - 1)
