"""Credit notes and suppliers are server-paginated, searched and counted."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Supplier

User = get_user_model()


class SupplierServerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Sup Co')
        other = Company.objects.create(company_name='Other Co')
        cls.user = User.objects.create_user(username='sup_admin', email='s@sup.test', password='x')
        cls.user.role = 'ADMIN'; cls.user.company = cls.co; cls.user.save()
        Supplier.objects.create(company=cls.co, name='Engen Garage', email='accounts@engen.test')
        Supplier.objects.create(company=cls.co, name='Bridgestone', registration_number='2001/123456/07')
        Supplier.objects.create(company=cls.co, name='Old Tyres', is_active=False)
        Supplier.objects.create(company=other, name='Not ours')

    def setUp(self):
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def test_search_counts_and_pages(self):
        body = self.c.get('/api/v1/suppliers/?is_active=true&page_size=1').json()
        self.assertEqual(body['count'], 2)
        self.assertEqual([r['name'] for r in body['results']], ['Bridgestone'])     # by name
        self.assertEqual(body['counts'], {'ACTIVE': 2, 'INACTIVE': 1, 'ALL': 3})     # this company only
        self.assertEqual([r['name'] for r in self.c.get('/api/v1/suppliers/?search=engen.test').json()['results']],
                         ['Engen Garage'])                                          # email
        self.assertEqual([r['name'] for r in self.c.get('/api/v1/suppliers/?search=2001/123').json()['results']],
                         ['Bridgestone'])                                           # registration number


class CreditNoteListShapeTests(TestCase):
    def test_list_carries_counts_and_issued_total_and_searches(self):
        co = Company.objects.create(company_name='CN Co')
        user = User.objects.create_user(username='cn_admin', email='c@cn.test', password='x')
        user.role = 'ADMIN'; user.company = co; user.save()
        c = APIClient(); c.force_authenticate(user)
        body = c.get('/api/v1/credit-notes/?search=anything&status=ISSUED&page_size=20').json()
        self.assertEqual(body['status_counts'], {'ALL': 0, 'ISSUED': 0, 'VOID': 0})
        self.assertEqual(body['issued_total'], 0.0)
        self.assertEqual(body['results'], [])
