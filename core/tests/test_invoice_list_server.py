"""Invoices list is server-paginated: the Overdue filter, search, ordering
and the summary use the frontend's rules (core/services/invoice_list.py)."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Invoice

User = get_user_model()
TODAY = timezone.localdate()


class InvoiceListServerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='List Co')
        cls.user = User.objects.create_user(username='list_admin', email='l@list.test', password='x')
        cls.user.role = 'ADMIN'; cls.user.company = cls.co; cls.user.save()
        mk = lambda name: Customer.objects.create(company=cls.co, name=name, email=f'{name}@c.test', phone='', address='',
                                                  city='CPT', state='', zip_code='', credit_score=85, credit_score_source='MANUAL')
        cls.acme, cls.zulu = mk('Acme'), mk('Zulu Freight')
        cls.inv = {}
        for key, cust, status, due_days, issue_days in (
            ('late', cls.acme, 'SENT', -5, -40),       # overdue
            ('due', cls.acme, 'SENT', 10, -1),         # not yet due
            ('draft', cls.zulu, 'DRAFT', -5, -2),      # past due but a draft: never overdue
            ('other', cls.zulu, 'SENT', -3, -35),      # overdue
        ):
            inv = Invoice.objects.create(company=cls.co, customer=cust, invoice_number=f'INV-L-{key}',
                                         issue_date=TODAY + timedelta(days=issue_days),
                                         due_date=TODAY + timedelta(days=due_days), subtotal=Decimal('100'), status=status)
            cls.inv[key] = inv

    def setUp(self):
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def numbers(self, query):
        return [r['invoice_number'] for r in self.c.get(f'/api/v1/invoices/?{query}').json()['results']]

    def test_overdue_filter_uses_the_shared_rule(self):
        self.assertCountEqual(self.numbers('status=OVERDUE'), ['INV-L-late', 'INV-L-other'])

    def test_search_number_and_customer(self):
        self.assertCountEqual(self.numbers('search=zulu'), ['INV-L-draft', 'INV-L-other'])
        self.assertEqual(self.numbers('search=L-due'), ['INV-L-due'])

    def test_newest_issue_date_first_and_paginated(self):
        body = self.c.get('/api/v1/invoices/?page_size=2').json()
        self.assertEqual(body['count'], 4)
        self.assertEqual([r['invoice_number'] for r in body['results']], ['INV-L-due', 'INV-L-draft'])

    def test_summary_counts_and_overdue(self):
        s = self.c.get('/api/v1/invoices/summary/').json()
        # Past-due sent invoices are stored as OVERDUE (Invoice.save), so the
        # Sent chip counts the one still due, exactly as the page counted it.
        self.assertEqual(s['status_counts'], {'All': 4, 'OVERDUE': 2, 'SENT': 1, 'PAID': 0, 'DRAFT': 1})
        self.assertEqual(s['overdue_count'], 2)
        # Balances as stored: these test invoices have no lines, so no VAT
        # (Invoice.save no longer adds 15% on its own; lines carry the VAT).
        self.assertAlmostEqual(s['overdue_amount'], 200.0)
        self.assertEqual(s['draft_count'], 1)
        self.assertIsNone(s['avg_days_to_pay'])
