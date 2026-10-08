"""Whether a company is a VAT vendor can be set from Settings -> Company
(and onboarding), the superuser Companies panel, and Django admin."""
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from core.admin import CompanyAdmin
from core.models import Company

User = get_user_model()


class VatRegisteredControlsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Vat Co')
        cls.admin = User.objects.create_user(username='vat_admin', email='a@vat.test', password='x')
        cls.admin.role = 'ADMIN'; cls.admin.company = cls.co; cls.admin.save()
        cls.staff = User.objects.create_user(username='vat_staff', email='s@vat.test', password='x')
        cls.staff.role = 'DISPATCHER'; cls.staff.company = cls.co; cls.staff.save()
        cls.root = User.objects.create_superuser(username='vat_root', email='r@vat.test', password='x')

    def client_for(self, user):
        c = APIClient(); c.force_authenticate(user); return c

    def test_company_admin_sets_it_on_the_profile(self):
        c = self.client_for(self.admin)
        self.assertTrue(c.get('/api/v1/company/profile/').json()['vat_registered'])   # default: on
        r = c.patch('/api/v1/company/profile/', {'vat_registered': False, 'vat_number': ''}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.co.refresh_from_db()
        self.assertFalse(self.co.vat_registered)
        self.assertFalse(c.get('/api/v1/finance/settings/').json()['vat_registered'])

    def test_non_admin_cannot_change_it(self):
        r = self.client_for(self.staff).patch('/api/v1/company/profile/', {'vat_registered': False}, format='json')
        self.assertEqual(r.status_code, 403)
        self.co.refresh_from_db()
        self.assertTrue(self.co.vat_registered)

    def test_superuser_panel_shows_and_switches_it(self):
        c = self.client_for(self.root)
        row = next(r for r in c.get('/api/v1/admin/companies/?search=Vat Co').json()['results'] if r['id'] == self.co.id)
        self.assertEqual((row['vat_registered'], row['has_vat_number']), (True, False))
        r = c.post(f'/api/v1/admin/companies/{self.co.id}/action/', {'action': 'vat_off'}, format='json')
        self.assertEqual(r.json()['vat_registered'], False)
        c.post(f'/api/v1/admin/companies/{self.co.id}/action/', {'action': 'vat_on'}, format='json')
        self.co.refresh_from_db()
        self.assertTrue(self.co.vat_registered)
        self.assertEqual(self.client_for(self.admin).post(
            f'/api/v1/admin/companies/{self.co.id}/action/', {'action': 'vat_off'}, format='json').status_code, 403)

    def test_django_admin_actions(self):
        request = RequestFactory().post('/admin/core/company/')
        request.user = self.root
        setattr(request, 'session', {}); setattr(request, '_messages', FallbackStorage(request))
        ma = CompanyAdmin(Company, AdminSite())
        ma.mark_not_vat_registered(request, Company.objects.filter(pk=self.co.pk))
        self.co.refresh_from_db(); self.assertFalse(self.co.vat_registered)
        ma.mark_vat_registered(request, Company.objects.filter(pk=self.co.pk))
        self.co.refresh_from_db(); self.assertTrue(self.co.vat_registered)
        self.assertIn('vat_registered', ma.list_filter)
