"""PATCH /api/v1/auth/me/ must not let a user change their own authorisation
fields (role, status, is_active, username).

Before the fix, UserProfileView saved through UserSerializer, where ``role``
(and status/is_active/username) were writable — so any logged-in DRIVER,
DISPATCHER or VIEWER could send {"role": "ADMIN"} and become a company ADMIN.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Company

User = get_user_model()

ME_URLS = ['/api/v1/auth/me/', '/api/auth/me/']


class SelfProfileEscalationTests(TestCase):

    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(company_name='Escalation Co')
        self.users = {
            role: User.objects.create_user(
                username=f'{role.lower()}_user', email=f'{role.lower()}@example.com',
                password='Orig-pass-123!', role=role, company=self.company,
            )
            for role in ('DRIVER', 'DISPATCHER', 'VIEWER', 'MANAGER', 'OPERATOR')
        }
        self.admin = User.objects.create_user(
            username='admin_user', email='admin@example.com',
            password='Admin-pass-123!', role='ADMIN', company=self.company,
        )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    # ---- role escalation -------------------------------------------------

    def test_non_admin_roles_cannot_self_promote_to_admin(self):
        for role, user in self.users.items():
            for url in ME_URLS:
                for value in ('ADMIN', 'admin'):
                    with self.subTest(role=role, url=url, value=value):
                        resp = self._client(user).patch(url, {'role': value}, format='json')
                        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
                        self.assertIn('role', resp.json())
                        user.refresh_from_db()
                        self.assertEqual(user.role, role)

    def test_self_promotion_via_multipart_is_blocked(self):
        user = self.users['DRIVER']
        resp = self._client(user).patch('/api/v1/auth/me/', {'role': 'ADMIN'}, format='multipart')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.role, 'DRIVER')

    def test_role_change_mixed_with_profile_fields_changes_nothing(self):
        user = self.users['DRIVER']
        resp = self._client(user).patch(
            '/api/v1/auth/me/', {'first_name': 'Sneaky', 'role': 'ADMIN'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.role, 'DRIVER')
        self.assertNotEqual(user.first_name, 'Sneaky')

    def test_admin_cannot_change_own_role_via_me(self):
        resp = self._client(self.admin).patch('/api/v1/auth/me/', {'role': 'VIEWER'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'ADMIN')

    # ---- other authorisation fields -------------------------------------

    def test_cannot_change_is_active(self):
        user = self.users['DISPATCHER']
        resp = self._client(user).patch('/api/v1/auth/me/', {'is_active': False}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('is_active', resp.json())
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_inactive_flagged_user_cannot_reactivate_self(self):
        user = self.users['VIEWER']
        User.objects.filter(pk=user.pk).update(status='INACTIVE')
        user.refresh_from_db()
        resp = self._client(user).patch('/api/v1/auth/me/', {'status': 'ACTIVE'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('status', resp.json())
        user.refresh_from_db()
        self.assertEqual(user.status, 'INACTIVE')

    def test_cannot_change_status(self):
        user = self.users['DRIVER']
        resp = self._client(user).patch('/api/v1/auth/me/', {'status': 'PENDING'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.status, 'ACTIVE')

    def test_cannot_change_username(self):
        user = self.users['DRIVER']
        resp = self._client(user).patch('/api/v1/auth/me/', {'username': 'admin2'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('username', resp.json())
        user.refresh_from_db()
        self.assertEqual(user.username, 'driver_user')

    def test_is_superuser_and_company_are_ignored(self):
        user = self.users['DRIVER']
        other = Company.objects.create(company_name='Other Co')
        resp = self._client(user).patch(
            '/api/v1/auth/me/',
            {'is_superuser': True, 'is_staff': True, 'company': other.id, 'company_id': other.id},
            format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        user.refresh_from_db()
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertEqual(user.company_id, self.company.id)

    # ---- positive controls ----------------------------------------------

    def test_unchanged_protected_values_are_accepted(self):
        """Echoing back the GET payload (same role/status/is_active/username)
        must keep working, so clients that round-trip the object don't break."""
        user = self.users['DRIVER']
        client = self._client(user)
        payload = client.get('/api/v1/auth/me/').json()
        body = {k: payload[k] for k in ('role', 'status', 'is_active', 'username')}
        body['role'] = body['role'].lower()
        body['first_name'] = 'Echo'
        resp = client.patch('/api/v1/auth/me/', body, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Echo')
        self.assertEqual(user.role, 'DRIVER')

    def test_user_can_update_own_profile_fields(self):
        user = self.users['DRIVER']
        data = {
            'first_name': 'New', 'last_name': 'Name', 'email': 'new-driver@example.com',
            'phone': '+27 82 000 0000', 'job_title': 'Lead driver',
            'timezone': 'Europe/London', 'language': 'af', 'date_format': 'YYYY-MM-DD',
            'address': '1 Road',
        }
        resp = self._client(user).patch('/api/v1/auth/me/', data, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        user.refresh_from_db()
        for k, v in data.items():
            self.assertEqual(getattr(user, k), v, k)
        self.assertEqual(resp.json()['role'], 'DRIVER')

    def test_user_can_still_change_password_via_me(self):
        user = self.users['DISPATCHER']
        resp = self._client(user).patch(
            '/api/v1/auth/me/', {'password': 'Brand-new-pass-456!'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        user.refresh_from_db()
        self.assertTrue(user.check_password('Brand-new-pass-456!'))

    def test_get_me_still_returns_role_and_status(self):
        resp = self._client(self.users['VIEWER']).get('/api/v1/auth/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body['role'], 'VIEWER')
        self.assertEqual(body['status'], 'ACTIVE')
        self.assertTrue(body['is_active'])
        self.assertEqual(body['username'], 'viewer_user')

    # ---- admin user-management endpoint unchanged ------------------------

    def test_admin_can_change_another_users_role_via_users_endpoint(self):
        target = self.users['DRIVER']
        resp = self._client(self.admin).patch(
            f'/api/v1/users/{target.id}/', {'role': 'dispatcher', 'status': 'INACTIVE'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        target.refresh_from_db()
        self.assertEqual(target.role, 'DISPATCHER')
        self.assertEqual(target.status, 'INACTIVE')

    def test_admin_still_cannot_change_own_role_via_users_endpoint(self):
        resp = self._client(self.admin).patch(
            f'/api/v1/users/{self.admin.id}/', {'role': 'VIEWER'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_non_admin_cannot_use_users_endpoint(self):
        user = self.users['DRIVER']
        resp = self._client(user).patch(f'/api/v1/users/{user.id}/', {'role': 'ADMIN'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.role, 'DRIVER')
