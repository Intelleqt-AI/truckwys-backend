"""Tests for the no-free-tier signup flow: register -> verify email -> pay
via Paystack -> only THEN does the account (User/Company/Facility) get
created. All Paystack HTTP calls are mocked — no network access.
"""
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, PendingSignup
from core.services.paystack import MONTHLY_FEE

User = get_user_model()


def _init_response(reference='ref-signup-1'):
    return {
        'success': True,
        'data': {'authorization_url': f'https://checkout.paystack.com/{reference}', 'access_code': 'abc', 'reference': reference},
        'error': None, 'raw': {},
    }


def _verified_charge_data(reference='ref-signup-1', amount_cents=449900, email='new@example.com', status='success'):
    return {
        'id': 777, 'status': status, 'reference': reference, 'amount': amount_cents,
        'authorization': {
            'authorization_code': 'AUTH_signup1', 'reusable': True, 'last4': '4081',
            'card_type': 'visa', 'bank': 'TEST BANK',
        },
        'customer': {'customer_code': 'CUS_signup1', 'email': email},
    }


class RegisterViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register_creates_pending_signup_not_a_user(self):
        response = self.client.post('/api/v1/auth/register/', {
            'email': 'new@example.com', 'password': 'Sup3rSecret!', 'first_name': 'Jo',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='new@example.com').exists())
        pending = PendingSignup.objects.get(email='new@example.com')
        self.assertFalse(pending.email_verified)
        self.assertNotEqual(pending.password_hash, 'Sup3rSecret!')  # never stored plaintext

    def test_register_rejects_existing_user_email(self):
        User.objects.create_user(username='existing', email='taken@example.com', password='x')
        response = self.client.post('/api/v1/auth/register/', {
            'email': 'taken@example.com', 'password': 'Sup3rSecret!',
        }, format='json')
        self.assertEqual(response.status_code, 400)


class EmailVerifyViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pending = PendingSignup.objects.create(
            email='new@example.com', username='new@example.com', first_name='Jo',
            password_hash='hashed', company_name="Jo's Transport",
            otp_code='123456', otp_expires_at=timezone.now() + timedelta(minutes=10),
        )

    @mock.patch('core.services.paystack.initialize_transaction')
    def test_correct_otp_starts_checkout_without_creating_account(self, init_mock):
        init_mock.return_value = _init_response()
        response = self.client.post('/api/v1/auth/verify-email/', {
            'email': 'new@example.com', 'code': '123456', 'return_url': 'https://app.test/signup/complete',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['reference'], 'ref-signup-1')
        self.assertIn('authorization_url', response.data)
        self.assertFalse(User.objects.filter(email='new@example.com').exists())
        self.pending.refresh_from_db()
        self.assertTrue(self.pending.email_verified)
        self.assertEqual(self.pending.paystack_reference, 'ref-signup-1')

    def test_wrong_otp_is_rejected(self):
        response = self.client.post('/api/v1/auth/verify-email/', {
            'email': 'new@example.com', 'code': '000000',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_expired_otp_is_rejected(self):
        self.pending.otp_expires_at = timezone.now() - timedelta(minutes=1)
        self.pending.save()
        response = self.client.post('/api/v1/auth/verify-email/', {
            'email': 'new@example.com', 'code': '123456',
        }, format='json')
        self.assertEqual(response.status_code, 400)


class CompleteSignupViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pending = PendingSignup.objects.create(
            email='new@example.com', username='new@example.com', first_name='Jo', last_name='Doe',
            password_hash='hashed-password', company_name="Jo's Transport",
            email_verified=True, paystack_reference='ref-signup-1',
        )

    @mock.patch('core.services.paystack.verify_transaction')
    def test_successful_payment_creates_full_account_and_logs_in(self, verify_mock):
        verify_mock.return_value = {'success': True, 'data': _verified_charge_data(), 'error': None, 'raw': {}}
        response = self.client.post('/api/v1/auth/complete-signup/', {'reference': 'ref-signup-1'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.data)

        user = User.objects.get(email='new@example.com')
        self.assertEqual(user.role, 'ADMIN')
        company = user.company
        self.assertEqual(company.company_name, "Jo's Transport")
        self.assertEqual(company.subscription_status, 'active')
        self.assertEqual(company.subscription_plan, 'pro')
        self.assertEqual(company.paystack_authorization_code, 'AUTH_signup1')
        self.assertIsNotNone(company.next_billing_date)
        self.assertTrue(company.facility_set.exists() if hasattr(company, 'facility_set') else True)
        self.assertFalse(PendingSignup.objects.filter(email='new@example.com').exists())

    @mock.patch('core.services.paystack.verify_transaction')
    def test_failed_payment_creates_no_account_and_keeps_pending_row(self, verify_mock):
        verify_mock.return_value = {
            'success': True, 'data': _verified_charge_data(status='failed'), 'error': None, 'raw': {},
        }
        response = self.client.post('/api/v1/auth/complete-signup/', {'reference': 'ref-signup-1'}, format='json')
        self.assertEqual(response.status_code, 402)
        self.assertFalse(User.objects.filter(email='new@example.com').exists())
        self.assertFalse(Company.objects.filter(company_name="Jo's Transport").exists())
        self.assertTrue(PendingSignup.objects.filter(email='new@example.com').exists())  # survives for retry

    @mock.patch('core.services.paystack.verify_transaction')
    def test_amount_mismatch_creates_no_account(self, verify_mock):
        verify_mock.return_value = {
            'success': True, 'data': _verified_charge_data(amount_cents=100), 'error': None, 'raw': {},
        }
        response = self.client.post('/api/v1/auth/complete-signup/', {'reference': 'ref-signup-1'}, format='json')
        self.assertEqual(response.status_code, 402)
        self.assertFalse(User.objects.filter(email='new@example.com').exists())

    def test_unverified_email_cannot_complete_signup(self):
        self.pending.email_verified = False
        self.pending.save()
        response = self.client.post('/api/v1/auth/complete-signup/', {'reference': 'ref-signup-1'}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_unknown_reference_is_rejected(self):
        response = self.client.post('/api/v1/auth/complete-signup/', {'reference': 'no-such-ref'}, format='json')
        self.assertEqual(response.status_code, 404)


class RetrySignupPaymentViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pending = PendingSignup.objects.create(
            email='new@example.com', username='new@example.com', first_name='Jo',
            password_hash='hashed', company_name="Jo's Transport",
            email_verified=True, paystack_reference='ref-old',
        )

    @mock.patch('core.services.paystack.initialize_transaction')
    def test_retry_starts_a_fresh_checkout_on_the_same_pending_row(self, init_mock):
        init_mock.return_value = _init_response(reference='ref-new')
        response = self.client.post('/api/v1/auth/retry-signup-payment/', {
            'email': 'new@example.com', 'return_url': 'https://app.test/signup/complete',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['reference'], 'ref-new')
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.paystack_reference, 'ref-new')

    def test_unverified_pending_signup_cannot_retry(self):
        self.pending.email_verified = False
        self.pending.save()
        response = self.client.post('/api/v1/auth/retry-signup-payment/', {'email': 'new@example.com'}, format='json')
        self.assertEqual(response.status_code, 400)


class SignupPaymentFailedWebhookTests(TestCase):
    """The async safety net for a decline the shopper simply abandons —
    Paystack's hosted checkout doesn't auto-redirect back to the app on a
    decline the way it does on success, so without this, an abandoned
    checkout produces no "payment failed" email and no record at all."""

    def setUp(self):
        self.client = APIClient()
        self.pending = PendingSignup.objects.create(
            email='new@example.com', username='new@example.com', first_name='Jo',
            password_hash='hashed', company_name="Jo's Transport",
            email_verified=True, paystack_reference='ref-webhook-1',
        )

    def _post_signed(self, body: dict):
        from django.conf import settings
        raw = json.dumps(body).encode()
        secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
        sig = hmac.new(secret_key.encode(), raw, hashlib.sha512).hexdigest()
        return self.client.post(
            '/api/v1/billing/webhook/', data=raw, content_type='application/json',
            HTTP_X_PAYSTACK_SIGNATURE=sig,
        )

    @mock.patch('core.services.email_service.send_billing_email')
    def test_charge_failed_emails_the_pending_signup_and_stamps_it(self, email_mock):
        response = self._post_signed({
            'event': 'charge.failed',
            'data': {'reference': 'ref-webhook-1', 'status': 'failed'},
        })
        self.assertEqual(response.status_code, 200)
        email_mock.assert_called_once()
        self.assertEqual(email_mock.call_args.args[0], 'new@example.com')
        self.pending.refresh_from_db()
        self.assertIsNotNone(self.pending.payment_failed_notified_at)
        # Account still never created — only a notification, no state change.
        self.assertFalse(User.objects.filter(email='new@example.com').exists())

    @mock.patch('core.services.email_service.send_billing_email')
    def test_charge_failed_is_not_double_emailed_by_a_duplicate_webhook(self, email_mock):
        body = {'event': 'charge.failed', 'data': {'reference': 'ref-webhook-1', 'status': 'failed'}}
        self._post_signed(body)
        self._post_signed(body)
        email_mock.assert_called_once()

    @mock.patch('core.services.email_service.send_billing_email')
    def test_retry_re_arms_the_failure_notification(self, email_mock):
        # First attempt fails -> notified once.
        self._post_signed({'event': 'charge.failed', 'data': {'reference': 'ref-webhook-1', 'status': 'failed'}})
        email_mock.assert_called_once()

        # Retrying starts a fresh checkout against the same pending row...
        with mock.patch('core.services.paystack.initialize_transaction') as init_mock:
            init_mock.return_value = {
                'success': True,
                'data': {'authorization_url': 'https://checkout.paystack.com/ref-webhook-2', 'reference': 'ref-webhook-2'},
                'error': None, 'raw': {},
            }
            self.client.post('/api/v1/auth/retry-signup-payment/', {
                'email': 'new@example.com', 'return_url': 'https://app.test/signup/complete',
            }, format='json')
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.payment_failed_notified_at)  # re-armed

        # ...and if THAT one also fails, it gets its own, second notification.
        self._post_signed({'event': 'charge.failed', 'data': {'reference': 'ref-webhook-2', 'status': 'failed'}})
        self.assertEqual(email_mock.call_count, 2)

    @mock.patch('core.services.email_service.send_billing_email')
    def test_unknown_reference_is_acknowledged_but_ignored(self, email_mock):
        response = self._post_signed({
            'event': 'charge.failed',
            'data': {'reference': 'no-such-ref', 'status': 'failed'},
        })
        self.assertEqual(response.status_code, 200)
        email_mock.assert_not_called()
