"""Tests for the self-service delete-account endpoint."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Company, UserSession

User = get_user_model()


def _make_company(name):
    return Company.objects.create(company_name=name)


def _make_user(company, username, role='ADMIN', password='testpass123'):
    user = User.objects.create_user(username=username, email=f'{username}@example.com', password=password)
    user.company = company
    user.role = role
    user.save()
    return user


def _authed_client(user):
    client = APIClient()
    session = UserSession.objects.create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f'Token {session.key}')
    return client


class DeleteAccountViewTests(TestCase):
    def setUp(self):
        self.company = _make_company('Delete Test Co')

    def test_wrong_password_returns_400_and_leaves_account_active(self):
        user = _make_user(self.company, 'soleadmin')
        client = _authed_client(user)

        response = client.delete(reverse('delete-account'), {'password': 'wrongpass'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_correct_password_deactivates_and_clears_all_sessions(self):
        user = _make_user(self.company, 'soleadmin2')
        client = _authed_client(user)
        UserSession.objects.create(user=user)  # a second device's session

        response = client.delete(reverse('delete-account'), {'password': 'testpass123'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(user.status, 'INACTIVE')
        self.assertEqual(UserSession.objects.filter(user=user).count(), 0)

    def test_sole_admin_with_other_active_users_is_blocked(self):
        admin = _make_user(self.company, 'admin1', role='ADMIN')
        _make_user(self.company, 'operator1', role='OPERATOR')
        client = _authed_client(admin)

        response = client.delete(reverse('delete-account'), {'password': 'testpass123'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        admin.refresh_from_db()
        self.assertTrue(admin.is_active)

    def test_sole_admin_with_no_other_users_is_allowed(self):
        admin = _make_user(self.company, 'lonelyadmin', role='ADMIN')
        client = _authed_client(admin)

        response = client.delete(reverse('delete-account'), {'password': 'testpass123'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_with_another_active_admin_is_allowed(self):
        admin1 = _make_user(self.company, 'admin_a', role='ADMIN')
        _make_user(self.company, 'admin_b', role='ADMIN')
        client = _authed_client(admin1)

        response = client.delete(reverse('delete-account'), {'password': 'testpass123'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_admin_role_never_blocked_by_admin_guard(self):
        _make_user(self.company, 'admin_z', role='ADMIN')
        driver = _make_user(self.company, 'driver_z', role='DRIVER')
        client = _authed_client(driver)

        response = client.delete(reverse('delete-account'), {'password': 'testpass123'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_reauthenticating_after_deletion_fails_is_active_check(self):
        user = _make_user(self.company, 'soleadmin3')
        client = _authed_client(user)
        client.delete(reverse('delete-account'), {'password': 'testpass123'}, format='json')

        # Simulate a stray token still existing for the now-deactivated user —
        # UserSessionTokenAuthentication should reject it on is_active alone.
        user.refresh_from_db()
        stray_session = UserSession.objects.create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f'Token {stray_session.key}')
        response = client.get(reverse('user-profile'))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
