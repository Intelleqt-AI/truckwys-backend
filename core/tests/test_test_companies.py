"""Test companies (the team's own, flagged by a superuser) are never
suspended when a grace period ends; ordinary companies still are."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company
from core.services.subscription_billing import check_grace_period_expirations

User = get_user_model()


class TestCompanyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = User.objects.create_superuser(username='tc_root', email='r@tc.test', password='x')
        cls.owner = User.objects.create_user(username='tc_owner', email='o@tc.test', password='x')

    def company(self, name, test=False, status='grace_period', expired=True):
        when = timezone.now() + (timedelta(days=-1) if expired else timedelta(days=3))
        return Company.objects.create(company_name=name, subscription_status=status, is_test_company=test,
                                      grace_period_expires_at=when)

    def api(self, user):
        c = APIClient(); c.force_authenticate(user); return c

    def test_grace_end_keeps_test_company_active(self):
        team, real = self.company('Team Co', test=True), self.company('Real Co')
        summary = check_grace_period_expirations()
        team.refresh_from_db(); real.refresh_from_db()
        self.assertEqual((team.subscription_status, team.grace_period_expires_at), ('active', None))
        self.assertEqual(real.subscription_status, 'suspended')
        self.assertEqual((summary['suspended'], summary['kept_active']), (1, 1))

    def test_grace_not_yet_over_is_left_alone(self):
        team = self.company('Team Co', test=True, expired=False)
        check_grace_period_expirations()
        team.refresh_from_db()
        self.assertEqual(team.subscription_status, 'grace_period')

    def test_superuser_marks_and_unmarks(self):
        co = self.company('Dev Co', status='suspended')
        c = self.api(self.root)
        r = c.post(f'/api/v1/admin/companies/{co.id}/action/', {'action': 'test_on'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['is_test_company'])
        co.refresh_from_db()
        self.assertEqual((co.is_test_company, co.subscription_status, co.grace_period_expires_at), (True, 'active', None))
        row = next(x for x in c.get('/api/v1/admin/companies/?search=Dev Co').json()['results'] if x['id'] == co.id)
        self.assertTrue(row['is_test_company'])
        c.post(f'/api/v1/admin/companies/{co.id}/action/', {'action': 'test_off'}, format='json')
        co.refresh_from_db()
        self.assertEqual((co.is_test_company, co.subscription_status), (False, 'active'))

    def test_only_superusers_can_flag(self):
        co = self.company('Other Co')
        r = self.api(self.owner).post(f'/api/v1/admin/companies/{co.id}/action/', {'action': 'test_on'}, format='json')
        self.assertEqual(r.status_code, 403)
        co.refresh_from_db()
        self.assertFalse(co.is_test_company)


class TestCompanyOnMeTests(TestCase):
    def test_me_says_whether_the_company_is_a_test_company(self):
        team = Company.objects.create(company_name='Team Me Co', is_test_company=True)
        real = Company.objects.create(company_name='Real Me Co')
        for co, expected in ((team, True), (real, False)):
            u = User.objects.create_user(username=f'me_{co.id}', email=f'm{co.id}@tc.test', password='x')
            u.company = co; u.save()
            c = APIClient(); c.force_authenticate(u)
            self.assertIs(c.get('/api/v1/auth/me/').json()['is_test_company'], expected)
