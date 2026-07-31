"""Tests for per-device sessions and security-settings persistence."""

import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core import mail
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from core.models import UserSession

User = get_user_model()

WIN_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
MAC_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)'


class DuplicateEmailLoginTests(TestCase):
    """Emails aren't unique. Login by email must be DETERMINISTIC: the account
    whose password matches wins; when several match, the most recently used one
    (then lowest id) — never DB row order."""

    def setUp(self):
        cache.clear()  # reset the shared login rate-throttle between tests
        alert_patcher = patch('core.tasks.send_login_alert_email_task')
        alert_patcher.start()
        self.addCleanup(alert_patcher.stop)
        no_2fa = {'two_factor': False, 'login_alerts': False}
        self.user_a = User.objects.create_user(
            username='dup_a', email='dup@example.com', password='passA',
            security_settings=no_2fa,
        )
        self.user_b = User.objects.create_user(
            username='dup_b', email='dup@example.com', password='passB',
            security_settings=no_2fa,
        )

    def _login(self, password):
        client = APIClient(HTTP_USER_AGENT=WIN_UA)
        return client.post('/api/v1/auth/login/',
                           {'username': 'dup@example.com', 'password': password},
                           format='json')

    def test_password_selects_the_matching_account(self):
        resp = self._login('passB')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()['user']['id'], self.user_b.id)

    def test_shared_password_prefers_most_recently_used_account(self):
        # user_b signs in normally (by username) — a REAL login must stamp
        # last_login (complete_login), or the recency ordering below is dead code.
        client = APIClient(HTTP_USER_AGENT=WIN_UA)
        resp = client.post('/api/v1/auth/login/',
                           {'username': 'dup_b', 'password': 'passB'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.user_b.refresh_from_db()
        self.assertIsNotNone(self.user_b.last_login, 'login must stamp last_login')
        # Now both accounts share the password; email login must pick the
        # recently-used B, not the lower-id A.
        self.user_b.set_password('passA')
        self.user_b.save()
        resp = self._login('passA')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()['user']['id'], self.user_b.id)

    def test_shared_password_never_logged_in_falls_back_to_lowest_id(self):
        self.user_b.set_password('passA')
        self.user_b.save()
        resp = self._login('passA')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()['user']['id'], self.user_a.id)


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


class SessionBulkRevokeTestCase(TestCase):
    """DELETE auth/sessions/?scope=others|all — the adaptive bulk-logout button."""

    def setUp(self):
        cache.clear()  # reset the shared login rate-throttle between tests
        alert_patcher = patch('core.tasks.send_login_alert_email_task')
        alert_patcher.start()
        self.addCleanup(alert_patcher.stop)
        self.password = 'testpass123'
        self.user = User.objects.create_user(
            username='bulkuser', email='bulk@example.com', password=self.password,
            security_settings={'two_factor': False, 'login_alerts': False},
        )

    def _login(self, ua):
        client = APIClient(HTTP_USER_AGENT=ua)
        resp = client.post('/api/v1/auth/login/', {'username': 'bulkuser', 'password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        client.credentials(HTTP_AUTHORIZATION='Token ' + resp.json()['token'])
        return client

    def test_revoke_others_keeps_current(self):
        client_a = self._login(WIN_UA)
        client_b = self._login(MAC_UA)
        resp = client_a.delete('/api/v1/auth/sessions/?scope=others')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'revoked': 1})
        self.assertEqual(client_a.get('/api/v1/auth/me/').status_code, status.HTTP_200_OK)
        self.assertEqual(client_b.get('/api/v1/auth/me/').status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)

    def test_revoke_all_kills_everything(self):
        client_a = self._login(WIN_UA)
        client_b = self._login(MAC_UA)
        resp = client_a.delete('/api/v1/auth/sessions/?scope=all')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {'revoked': 2})
        self.assertEqual(client_a.get('/api/v1/auth/me/').status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(client_b.get('/api/v1/auth/me/').status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 0)

    def test_missing_or_bad_scope_400(self):
        client_a = self._login(WIN_UA)
        self._login(MAC_UA)
        for url in ('/api/v1/auth/sessions/', '/api/v1/auth/sessions/?scope=nope'):
            resp = client_a.delete(url)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, url)
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 2)

    def test_bulk_revoke_does_not_touch_other_users(self):
        other = User.objects.create_user(username='bulkother', email='bo@example.com', password='x')
        other_session = UserSession.objects.create(user=other, device='Mac')
        client_a = self._login(WIN_UA)
        client_a.delete('/api/v1/auth/sessions/?scope=all')
        self.assertTrue(UserSession.objects.filter(id=other_session.id).exists())


class LoginActivityTestCase(TestCase):
    """Auth events land in AuditLog and surface via auth/sessions/activity/."""

    def setUp(self):
        cache.clear()  # reset the shared login rate-throttle between tests
        alert_patcher = patch('core.tasks.send_login_alert_email_task')
        alert_patcher.start()
        self.addCleanup(alert_patcher.stop)
        self.password = 'testpass123'
        self.user = User.objects.create_user(
            username='actuser', email='act@example.com', password=self.password,
            security_settings={'two_factor': False, 'login_alerts': False},
        )

    def _login(self, ua):
        client = APIClient(HTTP_USER_AGENT=ua)
        resp = client.post('/api/v1/auth/login/', {'username': 'actuser', 'password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        client.credentials(HTTP_AUTHORIZATION='Token ' + resp.json()['token'])
        return client

    def _auth_rows(self):
        from core.models import AuditLog
        return AuditLog.objects.filter(user=self.user, action__in=('LOGIN', 'LOGOUT'))

    def test_login_writes_audit_row(self):
        self._login(WIN_UA)
        rows = self._auth_rows()
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.action, 'LOGIN')
        self.assertEqual(row.resource_type, 'UserSession')
        self.assertEqual(row.details['event'], 'login')
        self.assertEqual(row.details['device'], 'Windows PC')

    def test_logout_writes_logout_row(self):
        client = self._login(WIN_UA)
        session_id = str(UserSession.objects.get(user=self.user).id)
        client.post('/api/v1/auth/logout/')
        row = self._auth_rows().filter(action='LOGOUT').get()
        self.assertEqual(row.details['event'], 'logout')
        self.assertEqual(row.resource_id, session_id)

    def test_single_revoke_writes_revoked_row(self):
        client_a = self._login(WIN_UA)
        self._login(MAC_UA)
        target = [r for r in client_a.get('/api/v1/auth/sessions/').json() if not r['current']][0]['id']
        client_a.delete(f'/api/v1/auth/sessions/{target}/')
        row = self._auth_rows().filter(action='LOGOUT').get()
        self.assertEqual(row.details['event'], 'revoked')
        self.assertEqual(row.resource_id, target)

    def test_bulk_revoke_writes_one_aggregate_row(self):
        client_a = self._login(WIN_UA)
        self._login(MAC_UA)
        self._login(MAC_UA)
        client_a.delete('/api/v1/auth/sessions/?scope=others')
        rows = self._auth_rows().filter(action='LOGOUT')
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.details['event'], 'revoked_others')
        self.assertEqual(row.details['count'], 2)

    def test_activity_endpoint_own_events_desc_capped(self):
        from core.models import AuditLog
        client = self._login(WIN_UA)  # writes 1 LOGIN row
        for i in range(11):
            AuditLog.log_action(
                action='LOGIN', resource_type='UserSession', resource_id='-',
                user=self.user, details={'event': 'login', 'device': f'Device {i}'},
            )
        other = User.objects.create_user(username='actother', email='ao@example.com', password='x')
        foreign = AuditLog.log_action(
            action='LOGIN', resource_type='UserSession', resource_id='-',
            user=other, details={'event': 'login', 'device': 'Mac'},
        )
        business = AuditLog.log_action(
            action='CREATE', resource_type='Load', resource_id='1', user=self.user,
        )
        resp = client.get('/api/v1/auth/sessions/activity/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(len(data), 10)
        returned_ids = {r['id'] for r in data}
        self.assertNotIn(foreign.id, returned_ids)
        self.assertNotIn(business.id, returned_ids)
        times = [r['time'] for r in data]
        self.assertEqual(times, sorted(times, reverse=True))  # newest first
        for r in data:
            self.assertEqual(set(r.keys()), {'id', 'action', 'event', 'device', 'ip', 'time'})
            self.assertNotIn('key', r)

    def test_activity_requires_auth(self):
        resp = APIClient().get('/api/v1/auth/sessions/activity/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class LoginAvatarUrlTestCase(TestCase):
    """Login responses must return the avatar as an ABSOLUTE URL, like auth/me/.

    Without the request in the serializer context the avatar came back as a
    relative /media/... path, which the SPA resolved against the Vite origin
    → 404 → the header avatar only appeared after a reload (which refetches
    auth/me/, the one endpoint that did pass the context)."""

    def setUp(self):
        cache.clear()  # reset the shared login rate-throttle between tests
        alert_patcher = patch('core.tasks.send_login_alert_email_task')
        alert_patcher.start()
        self.addCleanup(alert_patcher.stop)
        self.password = 'testpass123'
        self.user = User.objects.create_user(
            username='avataruser', email='avatar@example.com', password=self.password,
            security_settings={'two_factor': False, 'login_alerts': False},
        )
        # Smallest valid GIF (1x1 transparent pixel).
        gif = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff'
               b'!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
        self.user.avatar.save('test-avatar.gif', ContentFile(gif), save=True)

    def test_login_returns_absolute_avatar_url(self):
        client = APIClient(HTTP_USER_AGENT=WIN_UA)
        resp = client.post('/api/v1/auth/login/',
                           {'username': 'avataruser', 'password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        avatar = resp.json()['user']['avatar']
        self.assertTrue(avatar and avatar.startswith('http'), f'expected absolute URL, got {avatar!r}')
        # And it matches what auth/me/ returns (the reload path).
        client.credentials(HTTP_AUTHORIZATION='Token ' + resp.json()['token'])
        me = client.get('/api/v1/auth/me/').json()
        self.assertEqual(avatar, me['avatar'])
