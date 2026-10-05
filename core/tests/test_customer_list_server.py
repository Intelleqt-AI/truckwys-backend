"""The Customers list carries each customer's owed/overdue balance, sorts on
the server and returns the page-wide flags, so the page needn't load the
invoice ledger."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, Invoice

User = get_user_model()
TODAY = timezone.localdate()


class CustomerListServerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Cust Co')
        other = Company.objects.create(company_name='Other Co')
        cls.user = User.objects.create_user(username='cust_admin', email='c@cust.test', password='x')
        cls.user.role = 'ADMIN'; cls.user.company = cls.co; cls.user.save()

        def mk(name, company_name='', city='', co=None, **kw):
            return Customer.objects.create(company=co or cls.co, name=name, company_name=company_name, email=f'{name}@c.test',
                                           phone='', address='', city=city, state='', zip_code='', **kw)
        cls.acme = mk('Ann', company_name='Acme Haulage', city='Durban')       # sorts as "Acme"
        cls.zulu = mk('Zulu Freight', city='Cape Town')
        cls.bare = mk('Bongi', city='Pretoria')                               # owes nothing
        mk('Elsewhere', co=other)

        def inv(key, cust, status, due_days, amount='100'):
            Invoice.objects.create(company=cls.co, customer=cust, invoice_number=f'INV-C-{key}',
                                   issue_date=TODAY - timedelta(days=60), due_date=TODAY + timedelta(days=due_days),
                                   subtotal=Decimal(amount), status=status)
        inv('a-late', cls.acme, 'SENT', -40)      # overdue
        inv('a-due', cls.acme, 'SENT', 10)        # not yet due: Acme is partly late
        inv('z-late', cls.zulu, 'SENT', -3, '300')
        inv('z-draft', cls.zulu, 'DRAFT', -5)     # a draft owes nothing

    def setUp(self):
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def get(self, query=''):
        return self.c.get(f'/api/v1/customers/?{query}').json()

    def test_rows_carry_balances(self):
        rows = {r['name']: r for r in self.get()['results']}
        self.assertEqual(set(rows), {'Ann', 'Zulu Freight', 'Bongi'})           # this company only
        self.assertEqual((rows['Ann']['owed_amount'], rows['Ann']['overdue_amount']), (200.0, 100.0))
        self.assertEqual(rows['Ann']['oldest_overdue_due'], (TODAY - timedelta(days=40)).isoformat())
        self.assertEqual((rows['Zulu Freight']['owed_amount'], rows['Zulu Freight']['overdue_amount']), (300.0, 300.0))
        self.assertEqual((rows['Bongi']['owed_amount'], rows['Bongi']['overdue_amount']), (0.0, 0.0))
        self.assertIsNone(rows['Bongi']['oldest_overdue_due'])

    def test_server_sorts(self):
        names = lambda sort: [r['name'] for r in self.get(f'sort={sort}')['results']]
        self.assertEqual(names('name_asc'), ['Ann', 'Bongi', 'Zulu Freight'])    # company name first
        self.assertEqual(names('name_desc'), ['Zulu Freight', 'Bongi', 'Ann'])
        self.assertEqual(names('owed'), ['Zulu Freight', 'Ann', 'Bongi'])
        self.assertEqual(names('overdue'), ['Zulu Freight', 'Ann', 'Bongi'])
        self.assertEqual(names('city'), ['Zulu Freight', 'Ann', 'Bongi'])
        self.assertEqual(names('newest'), ['Bongi', 'Zulu Freight', 'Ann'])

    def test_flags_and_paging(self):
        body = self.get('page_size=1&sort=owed')
        self.assertEqual(body['count'], 3)
        self.assertEqual([r['name'] for r in body['results']], ['Zulu Freight'])
        self.assertEqual(body['flags'], {'total_overdue': 400.0, 'any_partly_late': True, 'any_inactive': False})
        Customer.objects.filter(pk=self.bare.pk).update(is_active=False)
        self.assertTrue(self.get()['flags']['any_inactive'])
        self.assertFalse(self.get('search=zulu')['flags']['any_inactive'])     # inactive among the matches only
        self.assertEqual([r['name'] for r in self.get('search=durban')['results']], ['Ann'])   # city search

    def test_detail_has_no_balances(self):
        row = self.c.get(f'/api/v1/customers/{self.acme.pk}/').json()
        self.assertIsNone(row['owed_amount'])
