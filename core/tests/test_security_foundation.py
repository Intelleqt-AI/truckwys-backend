"""Security foundation tests (2026-10).

* FIELD_ENCRYPTION_KEY is mandatory in production, decryption failures are
  explicit (DecryptionError), encryption never falls back to plaintext, and
  integrations treat undecryptable credentials as disconnected.
* `manage.py reencrypt_fields` rotates stored secrets onto the current key.
* Integration settings/actions and integration API keys/webhooks are limited
  to the company ADMIN role or a superuser; status reads stay open.
"""

import io
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, IntegrationAPIKey, Vehicle, VehicleType, Webhook
from core.utils import crypto
from core.utils.crypto import DecryptionError, decrypt_secret, encrypt_secret

User = get_user_model()

KEY_1 = Fernet.generate_key().decode()
KEY_2 = Fernet.generate_key().decode()


def _prod():
    """Pretend we are a production process (DEBUG off, not the test runner)."""
    return mock.patch.object(crypto, 'running_tests', return_value=False)


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------
class CryptoTests(SimpleTestCase):
    @override_settings(FIELD_ENCRYPTION_KEY=KEY_1)
    def test_roundtrip(self):
        token = encrypt_secret('s3cret')
        self.assertTrue(token.startswith('enc:'))
        self.assertNotIn('s3cret', token)
        self.assertEqual(decrypt_secret(token), 's3cret')

    def test_empty_values(self):
        self.assertEqual(encrypt_secret(''), '')
        self.assertEqual(encrypt_secret(None), '')
        self.assertEqual(decrypt_secret(''), '')
        self.assertEqual(decrypt_secret(None), '')

    def test_legacy_plaintext_still_readable(self):
        self.assertEqual(decrypt_secret('plain-legacy'), 'plain-legacy')

    def test_wrong_key_raises_not_blank(self):
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_1):
            token = encrypt_secret('s3cret')
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_2):
            with self.assertRaises(DecryptionError):
                decrypt_secret(token)
        with self.assertRaises(DecryptionError):
            decrypt_secret('enc:not-a-token')

    def test_multi_key_rotation_decrypts_old(self):
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_1):
            old_token = encrypt_secret('s3cret')
        with override_settings(FIELD_ENCRYPTION_KEY=f'{KEY_2},{KEY_1}'):
            self.assertEqual(decrypt_secret(old_token), 's3cret')
            new_token = encrypt_secret('s3cret')
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_2):
            self.assertEqual(decrypt_secret(new_token), 's3cret')  # first key encrypts

    def test_encrypt_never_falls_back_to_plaintext(self):
        with mock.patch.object(crypto, '_fernet', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                encrypt_secret('s3cret')

    @override_settings(DEBUG=False, FIELD_ENCRYPTION_KEY='')
    def test_production_without_key_fails_closed(self):
        with _prod():
            with self.assertRaises(ImproperlyConfigured):
                crypto.validate_encryption_config()
            with self.assertRaises(ImproperlyConfigured):
                encrypt_secret('s3cret')

    @override_settings(DEBUG=False, FIELD_ENCRYPTION_KEY='not-a-fernet-key')
    def test_production_with_malformed_key_fails_closed(self):
        with _prod():
            with self.assertRaises(ImproperlyConfigured):
                crypto.validate_encryption_config()

    @override_settings(DEBUG=False, FIELD_ENCRYPTION_KEY=KEY_1)
    def test_production_with_key_boots(self):
        with _prod():
            crypto.validate_encryption_config()
            self.assertEqual(decrypt_secret(encrypt_secret('x')), 'x')

    @override_settings(DEBUG=True, FIELD_ENCRYPTION_KEY='')
    def test_dev_derives_key_from_secret_key(self):
        with _prod():  # not the test runner, but DEBUG on
            crypto.validate_encryption_config()
            self.assertEqual(decrypt_secret(encrypt_secret('x')), 'x')

    @override_settings(DEBUG=False, FIELD_ENCRYPTION_KEY='')
    def test_test_runner_works_without_key(self):
        crypto.validate_encryption_config()
        self.assertEqual(decrypt_secret(encrypt_secret('x')), 'x')


class UndecryptableCredentialsTests(TestCase):
    """Credentials encrypted under a key we no longer hold = disconnected."""

    def setUp(self):
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_2):
            foreign = encrypt_secret('lost-secret')
        self.company = Company.objects.create(
            company_name='Crypto Co', cartrack_username='u', cartrack_password=foreign,
            cartrack_base_url='https://cartrack.invalid', ctrlfleet_api_key=foreign,
            xero_access_token=foreign, xero_refresh_token=foreign, xero_tenant_id='t-1',
        )

    @override_settings(FIELD_ENCRYPTION_KEY=KEY_1)
    def test_cartrack_raises_api_error(self):
        from core.integrations.cartrack import CartrackAPIError, CartrackClient
        with self.assertLogs('core.integrations.cartrack', level='ERROR'):
            with self.assertRaises(CartrackAPIError):
                CartrackClient.for_company(self.company)

    @override_settings(FIELD_ENCRYPTION_KEY=KEY_1)
    def test_ctrlfleet_raises_api_error(self):
        from core.integrations.ctrlfleet import CtrlFleetAPIError, CtrlFleetClient
        with self.assertLogs('core.integrations.ctrlfleet', level='ERROR'):
            with self.assertRaises(CtrlFleetAPIError):
                CtrlFleetClient.for_company(self.company)

    @override_settings(FIELD_ENCRYPTION_KEY=KEY_1)
    def test_xero_reports_disconnected(self):
        from core.integrations.xero import XeroClient, XeroCredentialsError
        client = XeroClient(self.company)
        with self.assertLogs('core.integrations.xero', level='ERROR'):
            self.assertFalse(client.is_connected)
        with self.assertRaises(XeroCredentialsError):
            client._get_valid_token()

    @override_settings(FIELD_ENCRYPTION_KEY=KEY_2)
    def test_readable_credentials_still_work(self):
        from core.integrations.xero import XeroClient
        self.assertTrue(XeroClient(self.company).is_connected)


# ---------------------------------------------------------------------------
# reencrypt_fields
# ---------------------------------------------------------------------------
class ReencryptFieldsTests(TestCase):
    def setUp(self):
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_1):
            self.old_token = encrypt_secret('old-key-secret')
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_2):
            self.current_token = encrypt_secret('current-secret')
        self.unknown_token = 'enc:' + Fernet(Fernet.generate_key()).encrypt(b'x').decode()
        self.company = Company.objects.create(
            company_name='Rotate Co',
            xero_access_token=self.old_token,       # old key -> rotate
            xero_refresh_token=self.current_token,  # already current -> untouched
            cartrack_password='legacy-plaintext',   # plaintext -> encrypt
            ctrlfleet_api_key=self.unknown_token,   # unknown key -> report only
        )

    def run_cmd(self, *args):
        out = io.StringIO()
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_2, FIELD_ENCRYPTION_KEY_OLD=KEY_1):
            call_command('reencrypt_fields', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        out = self.run_cmd()
        self.assertIn('DRY RUN', out)
        self.assertIn('rotated=1', out)
        self.assertIn('plaintext encrypted=1', out)
        self.assertIn('undecryptable=1', out)
        self.assertNotIn('old-key-secret', out)
        self.assertNotIn('legacy-plaintext', out)
        self.company.refresh_from_db()
        self.assertEqual(self.company.xero_access_token, self.old_token)
        self.assertEqual(self.company.cartrack_password, 'legacy-plaintext')

    def test_apply_rotates_and_is_idempotent(self):
        out = self.run_cmd('--apply')
        self.assertIn('APPLIED', out)
        self.company.refresh_from_db()
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_2):
            self.assertEqual(decrypt_secret(self.company.xero_access_token), 'old-key-secret')
            self.assertEqual(decrypt_secret(self.company.cartrack_password), 'legacy-plaintext')
        self.assertTrue(self.company.cartrack_password.startswith('enc:'))
        self.assertEqual(self.company.xero_refresh_token, self.current_token)
        self.assertEqual(self.company.ctrlfleet_api_key, self.unknown_token)

        second = self.run_cmd('--apply')
        self.assertIn('rotated=0', second)
        self.assertIn('plaintext encrypted=0', second)
        self.assertIn('already current=3', second)

    def test_include_derived_key_recovers_legacy_secret_key_values(self):
        with override_settings(FIELD_ENCRYPTION_KEY=''):
            derived = encrypt_secret('derived-secret')  # test runner -> SECRET_KEY-derived
        self.company.xero_access_token = derived
        self.company.save(update_fields=['xero_access_token'])
        self.assertIn('undecryptable=2', self.run_cmd())
        self.run_cmd('--include-derived-key', '--apply')
        self.company.refresh_from_db()
        with override_settings(FIELD_ENCRYPTION_KEY=KEY_2):
            self.assertEqual(decrypt_secret(self.company.xero_access_token), 'derived-secret')


# ---------------------------------------------------------------------------
# Integration permissions
# ---------------------------------------------------------------------------
class IntegrationPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(company_name='Perm Co')

        def mk(name, role, **extra):
            u = User.objects.create_user(username=name, email=f'{name}@perm.test', password='x')
            u.role = role
            u.company = cls.company
            for k, v in extra.items():
                setattr(u, k, v)
            u.save()
            return u

        cls.admin = mk('perm_admin', 'ADMIN')
        cls.manager = mk('perm_manager', 'MANAGER')
        cls.dispatcher = mk('perm_dispatcher', 'DISPATCHER')
        cls.superuser = mk('perm_super', 'VIEWER', is_superuser=True, is_staff=True)

    def client_for(self, user):
        c = APIClient(HTTP_HOST='localhost')
        c.force_authenticate(user=user)
        return c

    ACTIONS = [
        ('get', '/api/v1/integrations/xero/connect/'),
        ('post', '/api/v1/integrations/xero/disconnect/'),
        ('post', '/api/v1/integrations/xero/sync-invoices/'),
        ('post', '/api/v1/integrations/xero/sync-payments/'),
        ('post', '/api/v1/integrations/cartrack/connect/'),
        ('post', '/api/v1/integrations/ctrlfleet/connect/'),
        ('post', '/api/v1/integrations/ctrlfleet/disconnect/'),
        ('post', '/api/v1/integrations/ctrlfleet/sync-vehicles/'),
        ('post', '/api/v1/integrations/ctrlfleet/link-vehicle/'),
        ('post', '/api/v1/integrations/ctrlfleet/sync-positions/'),
        ('get', '/api/v1/integrations/api-keys/'),
        ('post', '/api/v1/integrations/api-keys/'),
        ('get', '/api/v1/webhooks/'),
        ('post', '/api/v1/webhooks/'),
    ]

    def test_non_admin_roles_get_403_with_message(self):
        for user in (self.manager, self.dispatcher):
            c = self.client_for(user)
            for method, url in self.ACTIONS:
                with self.subTest(user=user.role, url=url, method=method):
                    resp = getattr(c, method)(url, {}, format='json')
                    self.assertEqual(resp.status_code, 403, resp.content)
                    body = resp.json()
                    self.assertIn('company admin', (body.get('detail') or body.get('error') or '').lower())

    def test_status_reads_stay_open(self):
        c = self.client_for(self.dispatcher)
        for url in ('/api/v1/integrations/xero/status/', '/api/v1/integrations/cartrack/status/',
                    '/api/v1/integrations/ctrlfleet/status/', '/api/v1/integrations/xero/sync-log/'):
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 200)

    def test_admin_and_superuser_pass_the_gate(self):
        for user in (self.admin, self.superuser):
            c = self.client_for(user)
            with self.subTest(user=user.username):
                # Local-only actions (no external API call) succeed outright.
                self.assertEqual(c.post('/api/v1/integrations/ctrlfleet/disconnect/', {},
                                        format='json').status_code, 200)
                self.assertEqual(c.post('/api/v1/integrations/xero/disconnect/', {},
                                        format='json').status_code, 200)
                self.assertEqual(c.get('/api/v1/integrations/api-keys/').status_code, 200)
                self.assertEqual(c.get('/api/v1/webhooks/').status_code, 200)
                # Validation errors, not permission errors, for the rest.
                self.assertEqual(c.post('/api/v1/integrations/cartrack/connect/', {},
                                        format='json').status_code, 400)

    def test_admin_can_create_api_key_but_not_bind_companies(self):
        other = Company.objects.create(company_name='Someone Else')
        resp = self.client_for(self.admin).post('/api/v1/integrations/api-keys/', {
            'name': 'k', 'key_type': 'LENDER', 'allowed_companies': [other.id],
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        key = IntegrationAPIKey.objects.get(pk=resp.json()['id'])
        self.assertEqual(list(key.allowed_companies.all()), [])

    def test_dispatcher_single_vehicle_location_sync_still_allowed(self):
        vt = VehicleType.objects.create(name='PermT', capacity=1, max_distance=1, base_rate=1)
        v = Vehicle.objects.create(company=self.company, vin='PERMVIN', plate='PRM1GP',
                                   vehicle_type=vt, make='M', model='A', year=2020, type='Truck',
                                   capacity=1, fuel_type='Diesel', status='AVAILABLE')
        resp = self.client_for(self.dispatcher).post(
            '/api/v1/integrations/ctrlfleet/sync-positions/', {'vehicle_id': v.id}, format='json')
        # Not connected -> 400; the point is it is not a 403.
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_webhook_rows_hidden_from_demoted_user(self):
        Webhook.objects.create(operator=self.dispatcher, url='https://example.invalid/h', events=['x'])
        self.assertEqual(self.client_for(self.dispatcher).get('/api/v1/webhooks/').status_code, 403)
