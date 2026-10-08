"""A delete blocked by PROTECT foreign keys is a 409 with a plain reason,
not a 500 "unexpected server error" (customer with quotes/invoices)."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, Invoice

User = get_user_model()


class ProtectedDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Protect Co')
        cls.user = User.objects.create_user(username='protect_admin', email='p@protect.test', password='x')
        cls.user.role = 'ADMIN'
        cls.user.company = cls.co
        cls.user.save()

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _customer(self, name):
        return Customer.objects.create(company=self.co, name=name, email=f'{name}@protect.test', phone='', address='',
                                       city='JHB', state='', zip_code='', credit_score=85, credit_score_source='MANUAL')

    def test_customer_with_invoices_gets_409_and_a_reason(self):
        c = self._customer('linked')
        for n in (1, 2):
            Invoice.objects.create(company=self.co, customer=c, invoice_number=f'INV-PROT-{n}',
                                   due_date=date.today() + timedelta(days=30), subtotal=Decimal('100'), status='DRAFT')
        r = self.client.delete(f'/api/v1/customers/{c.id}/')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['error'], "This customer can't be deleted: 2 invoices are linked to it.")
        self.assertEqual(r.json()['linked'], {'invoices': 2})
        self.assertTrue(Customer.objects.filter(pk=c.pk).exists())

    def test_unlinked_customer_still_deletes(self):
        c = self._customer('free')
        self.assertEqual(self.client.delete(f'/api/v1/customers/{c.id}/').status_code, 204)
        self.assertFalse(Customer.objects.filter(pk=c.pk).exists())
