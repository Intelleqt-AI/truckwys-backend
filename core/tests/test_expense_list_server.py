"""Expenses list is server-paginated: filters, search, ordering and the
overview summary (core/services/expense_list.py)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Expense, Supplier

User = get_user_model()
TODAY = timezone.localdate()


class ExpenseListServerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Exp Co')
        cls.user = User.objects.create_user(username='exp_admin', email='e@exp.test', password='x')
        cls.user.role = 'ADMIN'; cls.user.company = cls.co; cls.user.save()
        sup = Supplier.objects.create(company=cls.co, name='Engen Garage')
        for n, cat, status, days, amt, supplier in (
            (1, 'FUEL', 'APPROVED', 0, '1000', sup),
            (2, 'FUEL', 'PENDING', -3, '500', None),
            (3, 'TOLLS', 'REJECTED', -5, '200', None),
            (4, 'MAINTENANCE', 'APPROVED', -400, '900', None),   # older than 12 months
        ):
            Expense.objects.create(company=cls.co, expense_number=f'EXP-S-{n}', category=cat, description=f'item {n}',
                                   amount=Decimal(amt), expense_date=TODAY + timedelta(days=days), status=status,
                                   supplier=supplier)

    def setUp(self):
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def numbers(self, q):
        return [r['expense_number'] for r in self.c.get(f'/api/v1/expenses/?{q}').json()['results']]

    def test_filters_search_and_order(self):
        self.assertEqual(self.numbers(''), ['EXP-S-1', 'EXP-S-2', 'EXP-S-3', 'EXP-S-4'])   # newest first
        self.assertEqual(self.numbers('status=PENDING'), ['EXP-S-2'])
        self.assertEqual(self.numbers('category=FUEL&status=APPROVED'), ['EXP-S-1'])
        self.assertEqual(self.numbers('search=engen'), ['EXP-S-1'])                        # supplier name
        since = (TODAY - timedelta(days=4)).isoformat()
        self.assertEqual(self.numbers(f'expense_date__gte={since}'), ['EXP-S-1', 'EXP-S-2'])

    def test_summary(self):
        s = self.c.get('/api/v1/expenses/summary/').json()
        self.assertEqual((s['spend_total'], s['spend_count']), (2400.0, 3))          # rejected left out
        self.assertEqual((s['approved_year_amount'], s['approved_year_count']), (1000.0, 1))
        self.assertEqual((s['pending_amount'], s['pending_count']), (500.0, 1))
        self.assertEqual(s['status_counts'], {'ALL': 4, 'PENDING': 1, 'APPROVED': 2, 'REJECTED': 1})
        self.assertEqual({c['category']: c['amount'] for c in s['by_category']}, {'FUEL': 1500.0, 'MAINTENANCE': 900.0})
