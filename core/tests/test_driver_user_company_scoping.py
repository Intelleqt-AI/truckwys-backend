"""DriverSerializer.user must not accept a user id from another company.

Before the fix, 'user' was a default ModelSerializer PK field with the
unscoped User.objects.all() queryset, so a driver record in company A could
be linked to a user who belongs to company B (or to no company at all).
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Company

User = get_user_model()


class DriverUserCompanyScopingTests(TestCase):

    def setUp(self):
        cache.clear()
        self.company_a = Company.objects.create(company_name='Company A')
        self.company_b = Company.objects.create(company_name='Company B')
        self.admin_a = User.objects.create_user(
            username='admin_a', email='admin_a@example.com', password='Pass-123!',
            role='ADMIN', company=self.company_a,
        )
        self.driver_user_a = User.objects.create_user(
            username='driver_a', email='driver_a@example.com', password='Pass-123!',
            role='DRIVER', company=self.company_a,
        )
        self.driver_user_b = User.objects.create_user(
            username='driver_b', email='driver_b@example.com', password='Pass-123!',
            role='DRIVER', company=self.company_b,
        )
        self.companyless_user = User.objects.create_user(
            username='no_co', email='no_co@example.com', password='Pass-123!',
            role='DRIVER', company=None,
        )
        self.platform_superuser = User.objects.create_superuser(
            username='platform_su', email='platform_su@example.com', password='Pass-123!',
        )
        self.platform_superuser.company = None
        self.platform_superuser.save(update_fields=['company'])

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _payload(self, user_id):
        return {
            'user': user_id,
            'license_number': f'LIC-{user_id}',
            'license_expiry': '2030-01-01',
            'license_state': 'GP',
            'hire_date': '2026-01-01',
        }

    def test_cannot_link_driver_to_another_companys_user(self):
        resp = self._client(self.admin_a).post(
            '/api/v1/drivers/', self._payload(self.driver_user_b.id), format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('user', resp.json())

    def test_cannot_link_driver_to_a_companyless_user(self):
        resp = self._client(self.admin_a).post(
            '/api/v1/drivers/', self._payload(self.companyless_user.id), format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

    def test_can_link_driver_to_own_companys_user(self):
        resp = self._client(self.admin_a).post(
            '/api/v1/drivers/', self._payload(self.driver_user_a.id), format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()['user'], self.driver_user_a.id)

    def test_platform_superuser_with_no_company_is_unscoped(self):
        resp = self._client(self.platform_superuser).post(
            '/api/v1/drivers/', self._payload(self.driver_user_b.id), format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
