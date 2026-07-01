"""Tests for per-device sessions and security-settings persistence."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.core import mail
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from core.models import UserSession

User = get_user_model()

WIN_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
MAC_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)'


class SessionsTestCase(TestCase):
    def setUp(self):
        cache.clear()  # reset the shared login rate-throttle between tests
        # Silence the new-device login alert (keeps these tests off the network).
        alert_patcher = patch('core.tasks.send_login_alert_email_task')
        alert_patcher.start()
        self.addCleanup(alert_patcher.stop)
        self.password = 'testpass123'
        self.user = User.objects.create_user(
            username='sessuser', email='sess@example.com', password=self.password,
            # These tests exercise single-step session mechanics; keep 2FA off so
            # login returns a token directly (2FA is covered in LoginTwoFactorTestCase).
            security_settings={'two_factor': False, 'login_alerts': True},
        )

    def _login(self, ua):
        client = APIClient(HTTP_USER_AGENT=ua)
        resp = client.post('/api/v1/auth/login/', {'username': 'sessuser', 'password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        token = resp.json()['token']
        client.credentials(HTTP_AUTHORIZATION='Token ' + token)
        return client, token

    def test_login_creates_session(self):
        client, token = self._login(WIN_UA)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)
        session = UserSession.objects.get(user=self.user)
        self.assertEqual(session.key, token)
        self.assertEqual(session.device, 'Windows PC')

    def test_two_logins_two_sessions(self):
        self._login(WIN_UA)
        self._login(MAC_UA)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 2)

    def test_list_marks_current_and_hides_key(self):
        client_a, token_a = self._login(WIN_UA)
        self._login(MAC_UA)
        resp = client_a.get('/api/v1/auth/sessions/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(len(data), 2)
        current = [r for r in data if r['current']]
        self.assertEqual(len(current), 1)
        # The raw key must never be serialized.
        for row in data:
            self.assertNotIn('key', row)
            self.assertNotEqual(row['id'], token_a)

    def test_revoke_kills_that_device(self):
        client_a, _ = self._login(WIN_UA)
        client_b, token_b = self._login(MAC_UA)
        # Device A finds device B in the list and revokes it.
        data = client_a.get('/api/v1/auth/sessions/').json()
        target = [r for r in data if not r['current']][0]['id']
        resp = client_a.delete(f'/api/v1/auth/sessions/{target}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        # Device B's token is now dead.
        self.assertEqual(client_b.get('/api/v1/auth/me/').status_code, status.HTTP_401_UNAUTHORIZED)
        # Device A still works.
        self.assertEqual(client_a.get('/api/v1/auth/me/').status_code, status.HTTP_200_OK)

    def test_revoke_other_users_session_404(self):
        other = User.objects.create_user(username='other', email='o@example.com', password='x')
        other_session = UserSession.objects.create(user=other, device='Mac')
        client_a, _ = self._login(WIN_UA)
        resp = client_a.delete(f'/api/v1/auth/sessions/{other_session.id}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(UserSession.objects.filter(id=other_session.id).exists())

    def test_logout_only_current_device(self):
        client_a, _ = self._login(WIN_UA)
        client_b, token_b = self._login(MAC_UA)
        resp = client_b.post('/api/v1/auth/logout/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # B's session gone, A's intact.
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)
        self.assertEqual(client_a.get('/api/v1/auth/me/').status_code, status.HTTP_200_OK)


class SessionIdleTimeoutTestCase(TestCase):
    """Auto sign-out after inactivity, gated by the session_timeout preference."""

    def setUp(self):
        cache.clear()  # reset the shared login rate-throttle between tests
        # Silence the new-device login alert (keeps these tests off the network).
        alert_patcher = patch('core.tasks.send_login_alert_email_task')
        alert_patcher.start()
        self.addCleanup(alert_patcher.stop)
        self.password = 'testpass123'
        self.user = User.objects.create_user(
            username='idleuser', email='idle@example.com', password=self.password,
            # 2FA off so login returns a token directly. session_timeout is left
            # unset so it takes its default (True) — the common case for real users.
            security_settings={'two_factor': False, 'login_alerts': False},
        )

    def _login(self):
        client = APIClient(HTTP_USER_AGENT=WIN_UA)
        resp = client.post('/api/v1/auth/login/', {'username': 'idleuser', 'password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        client.credentials(HTTP_AUTHORIZATION='Token ' + resp.json()['token'])
        return client

    def _set_last_activity(self, minutes_ago):
        # Bare UPDATE so we control the stored value directly.
        when = timezone.now() - timedelta(minutes=minutes_ago)
        UserSession.objects.filter(user=self.user).update(last_activity=when)

    def test_stale_session_with_timeout_on_is_signed_out(self):
        client = self._login()
        self._set_last_activity(31)  # idle past the 30-min window
        resp = client.get('/api/v1/auth/me/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        # The session row is deleted, so the token is dead for good.
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 0)

    def test_stale_session_with_timeout_off_still_works(self):
        client = self._login()
        self.user.security_settings = {'two_factor': False, 'login_alerts': False, 'session_timeout': False}
        self.user.save(update_fields=['security_settings'])
        self._set_last_activity(31)  # idle, but the user opted out of the timeout
        resp = client.get('/api/v1/auth/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)

    def test_recent_activity_with_timeout_on_still_works(self):
        client = self._login()
        self._set_last_activity(2)  # active within the window
        resp = client.get('/api/v1/auth/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)


class SecuritySettingsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='secuser', email='sec@example.com', password='x')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_defaults_then_persist(self):
        resp = self.client.get('/api/v1/auth/security-settings/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'two_factor': True, 'session_timeout': True, 'login_alerts': True})

        resp = self.client.patch('/api/v1/auth/security-settings/', {'two_factor': False}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.json()['two_factor'])

        # Persisted across a fresh read.
        self.user.refresh_from_db()
        self.assertFalse(self.user.security_settings['two_factor'])
        self.assertFalse(self.client.get('/api/v1/auth/security-settings/').json()['two_factor'])


class LoginAlertTestCase(TestCase):
    """New-device login alert emails, gated by the login_alerts preference."""

    def setUp(self):
        cache.clear()  # reset the shared login rate-throttle between tests
        self.password = 'testpass123'
        self.user = User.objects.create_user(
            username='alertuser', email='alert@example.com', password=self.password,
            first_name='Al',
            # 2FA off so login is single-step here; the alert is what we're testing.
            security_settings={'two_factor': False, 'login_alerts': True},
        )

    def _login(self, ua):
        client = APIClient(HTTP_USER_AGENT=ua)
        resp = client.post('/api/v1/auth/login/', {'username': 'alertuser', 'password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return client

    @patch('core.tasks.send_login_alert_email_task')
    def test_new_device_triggers_alert(self, mock_alert):
        self._login(WIN_UA)
        self.assertEqual(mock_alert.call_count, 1)
        args = mock_alert.call_args.args
        self.assertEqual(args[0], self.user.email)
        self.assertEqual(args[2], 'Windows PC')  # device

    @patch('core.tasks.send_login_alert_email_task')
    def test_same_device_second_login_no_repeat_alert(self, mock_alert):
        self._login(WIN_UA)   # first login on this device -> alert
        self._login(WIN_UA)   # same device+IP still active -> no new alert
        self.assertEqual(mock_alert.call_count, 1)

    @patch('core.tasks.send_login_alert_email_task')
    def test_different_device_triggers_alert(self, mock_alert):
        self._login(WIN_UA)
        self._login(MAC_UA)
        self.assertEqual(mock_alert.call_count, 2)

    @patch('core.tasks.send_login_alert_email_task')
    def test_disabled_preference_no_alert(self, mock_alert):
        self.user.security_settings = {'login_alerts': False, 'two_factor': False}
        self.user.save(update_fields=['security_settings'])
        self._login(WIN_UA)
        mock_alert.assert_not_called()

    @patch('core.tasks.send_login_alert_email_task')
    def test_known_device_after_logout_no_alert(self, mock_alert):
        # First sign-in from a new device -> alert.
        c = APIClient(HTTP_USER_AGENT=WIN_UA)
        r = c.post('/api/v1/auth/login/', {'username': 'alertuser', 'password': self.password}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(mock_alert.call_count, 1)
        # Log out (deletes the active session).
        c.credentials(HTTP_AUTHORIZATION='Token ' + r.json()['token'])
        c.post('/api/v1/auth/logout/')
        # Sign in again from the same device: it's a *known* device (persisted on
        # the user, so it survives logout) -> no new alert.
        self._login(WIN_UA)
        self.assertEqual(mock_alert.call_count, 1)


class LoginAlertEmailDeliveryTestCase(TestCase):
    """The alert email goes through Django's mail backend (not Resend)."""

    def test_alert_sends_via_django_backend(self):
        from core.services.email_service import send_login_alert_email
        ok = send_login_alert_email('to@example.com', 'Al', 'Windows PC', '1.2.3.4', '01 Jul 2026, 10:00')
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)          # captured by the locmem test backend
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ['to@example.com'])
        self.assertIn('sign-in', msg.subject.lower())
        html = msg.alternatives[0][0]                  # the HTML alternative
        self.assertIn('Windows PC', html)
        self.assertIn('1.2.3.4', html)


@patch('core.tasks.send_login_otp_email_task', return_value=True)
class LoginTwoFactorTestCase(TestCase):
    """Email-OTP 2FA at login (two_factor on)."""

    def setUp(self):
        cache.clear()
        self.password = 'testpass123'
        self.user = User.objects.create_user(
            username='tfa', email='tfa@example.com', password=self.password, first_name='Tara',
            # two_factor defaults True; be explicit for clarity.
            security_settings={'two_factor': True, 'login_alerts': True},
        )

    def _password_step(self, client=None):
        client = client or APIClient(HTTP_USER_AGENT=WIN_UA)
        resp = client.post('/api/v1/auth/login/', {'username': 'tfa', 'password': self.password}, format='json')
        return client, resp

    def _current_otp(self, pending_token):
        return cache.get(f'login_pending_{pending_token}')['otp']

    def test_step1_issues_challenge_not_token(self, mock_otp):
        client, resp = self._password_step()
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        body = resp.json()
        self.assertTrue(body['otp_required'])
        self.assertIn('pending_token', body)
        self.assertNotIn('token', body)
        self.assertIn('***', body['email'])                       # masked
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 0)  # nothing created yet
        self.assertEqual(mock_otp.call_count, 1)

    @patch('core.tasks.send_login_alert_email_task')
    def test_verify_success_completes_login(self, mock_alert, mock_otp):
        client, resp = self._password_step()
        pending = resp.json()['pending_token']
        mock_alert.assert_not_called()                            # no alert on step 1
        otp = self._current_otp(pending)
        r = client.post('/api/v1/auth/login/verify-otp/', {'pending_token': pending, 'code': otp}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertIn('token', r.json())
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)
        self.assertEqual(mock_alert.call_count, 1)                # alert fires from the verify path
        self.assertIsNone(cache.get(f'login_pending_{pending}'))  # challenge burned

    def test_wrong_code_rejected_and_counts(self, mock_otp):
        client, resp = self._password_step()
        pending = resp.json()['pending_token']
        r = client.post('/api/v1/auth/login/verify-otp/', {'pending_token': pending, 'code': '000000'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(cache.get(f'login_pending_{pending}')['attempts'], 1)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 0)

    def test_attempt_cap_burns_challenge(self, mock_otp):
        client, resp = self._password_step()
        pending = resp.json()['pending_token']
        good = self._current_otp(pending)
        for _ in range(5):
            client.post('/api/v1/auth/login/verify-otp/', {'pending_token': pending, 'code': '000000'}, format='json')
        # Challenge is gone; even the correct code now reads as expired.
        self.assertIsNone(cache.get(f'login_pending_{pending}'))
        r = client.post('/api/v1/auth/login/verify-otp/', {'pending_token': pending, 'code': good}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 0)

    def test_expired_or_unknown_token(self, mock_otp):
        client = APIClient(HTTP_USER_AGENT=WIN_UA)
        r = client.post('/api/v1/auth/login/verify-otp/', {'pending_token': 'nope', 'code': '123456'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resend_rotates_code_and_cools_down(self, mock_otp):
        client, resp = self._password_step()
        pending = resp.json()['pending_token']
        old = self._current_otp(pending)
        # Immediate resend is blocked by the 60s cooldown.
        r = client.post('/api/v1/auth/login/resend-otp/', {'pending_token': pending}, format='json')
        self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        # Simulate the cooldown elapsing, then resend succeeds with a fresh code.
        challenge = cache.get(f'login_pending_{pending}')
        challenge['last_sent'] = 0
        cache.set(f'login_pending_{pending}', challenge, timeout=600)
        r = client.post('/api/v1/auth/login/resend-otp/', {'pending_token': pending}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        new = self._current_otp(pending)
        # Old code no longer works; new one does.
        bad = client.post('/api/v1/auth/login/verify-otp/', {'pending_token': pending, 'code': old}, format='json')
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        good = client.post('/api/v1/auth/login/verify-otp/', {'pending_token': pending, 'code': new}, format='json')
        self.assertEqual(good.status_code, status.HTTP_200_OK, good.content)

    def test_two_factor_off_is_single_step(self, mock_otp):
        self.user.security_settings = {'two_factor': False}
        self.user.save(update_fields=['security_settings'])
        client, resp = self._password_step()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('token', resp.json())
        mock_otp.assert_not_called()

    def test_wrong_password_is_401_no_challenge(self, mock_otp):
        client = APIClient(HTTP_USER_AGENT=WIN_UA)
        r = client.post('/api/v1/auth/login/', {'username': 'tfa', 'password': 'wrong'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)
        mock_otp.assert_not_called()
