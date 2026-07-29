"""Tests for the Paystack-based subscription flow: checkout initiation,
return_url confirmation, webhook activation (the async backup), and status.
Replaces the old PayFast/tier-based tests. All Paystack HTTP calls are
mocked — no network access.
"""
import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import BillingTransaction, Company
from core.services.paystack import MONTHLY_FEE

User = get_user_model()


def _verified_charge_data(reference='ref-123', amount_cents=449900, email='billing@test.com',
                          auth_code='AUTH_test123', status='success'):
    return {
        'id': 555,
        'status': status,
        'reference': reference,
        'amount': amount_cents,
        'authorization': {
            'authorization_code': auth_code,
            'reusable': True,
            'last4': '1381',
            'card_type': 'visa',
            'bank': 'TEST BANK',
        },
        'customer': {'customer_code': 'CUS_test1', 'email': email},
    }


class BillingViewTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Paystack Test Logistics')
        self.user = User.objects.create_user(
            username='billinguser', email='billing@test.com', password='testpass123',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)


class SubscribeViewTests(BillingViewTestCase):
    @mock.patch('core.views_billing.paystack.initialize_transaction')
    def test_initiates_checkout_and_creates_pending_transaction(self, init_mock):
        init_mock.return_value = {
            'success': True,
            'data': {'authorization_url': 'https://checkout.paystack.com/abc', 'access_code': 'abc', 'reference': 'ref-1'},
            'error': None, 'raw': {},
        }
        response = self.client.post('/api/v1/billing/subscribe/', {'return_url': 'https://app.test/return'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['authorization_url'], 'https://checkout.paystack.com/abc')
        self.assertEqual(response.data['reference'], 'ref-1')
        self.assertEqual(response.data['amount'], str(MONTHLY_FEE))
        txn = BillingTransaction.objects.get(company=self.company)
        self.assertEqual(txn.status, 'pending')
        self.assertEqual(txn.payment_id, 'ref-1')
        self.assertEqual(txn.amount, MONTHLY_FEE)
        # amount/plan are never client-controlled
        init_mock.assert_called_once()
        self.assertEqual(init_mock.call_args.kwargs['amount'], MONTHLY_FEE)

    @mock.patch('core.views_billing.paystack.initialize_transaction')
    def test_gateway_failure_returns_502(self, init_mock):
        init_mock.return_value = {'success': False, 'data': None, 'error': 'network down', 'raw': None}
        response = self.client.post('/api/v1/billing/subscribe/', {}, format='json')
        self.assertEqual(response.status_code, 502)


class ConfirmPaymentViewTests(BillingViewTestCase):
    def setUp(self):
        super().setUp()
        self.txn = BillingTransaction.objects.create(
            company=self.company, amount=MONTHLY_FEE, payment_id='ref-123', status='pending', plan='pro',
        )

    @mock.patch('core.views_billing.paystack.verify_transaction')
    def test_success_activates_company_and_stores_card(self, verify_mock):
        verify_mock.return_value = {'success': True, 'data': _verified_charge_data(), 'error': None, 'raw': {}}
        response = self.client.post('/api/v1/billing/confirm/', {'reference': 'ref-123'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.company.refresh_from_db()
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'complete')
        self.assertEqual(self.company.subscription_status, 'active')
        self.assertEqual(self.company.subscription_plan, 'pro')
        self.assertEqual(self.company.paystack_authorization_code, 'AUTH_test123')
        self.assertEqual(self.company.paystack_authorization_email, 'billing@test.com')
        self.assertIsNotNone(self.company.next_billing_date)

    @mock.patch('core.views_billing.paystack.verify_transaction')
    def test_amount_mismatch_is_rejected(self, verify_mock):
        verify_mock.return_value = {
            'success': True, 'data': _verified_charge_data(amount_cents=100), 'error': None, 'raw': {},
        }
        response = self.client.post('/api/v1/billing/confirm/', {'reference': 'ref-123'}, format='json')
        self.assertEqual(response.status_code, 402)
        self.company.refresh_from_db()
        self.assertNotEqual(self.company.subscription_status, 'active')

    @mock.patch('core.views_billing.paystack.verify_transaction')
    def test_unsuccessful_status_is_rejected(self, verify_mock):
        verify_mock.return_value = {
            'success': True, 'data': _verified_charge_data(status='failed'), 'error': None, 'raw': {},
        }
        response = self.client.post('/api/v1/billing/confirm/', {'reference': 'ref-123'}, format='json')
        self.assertEqual(response.status_code, 402)

    def test_already_complete_is_idempotent(self):
        self.txn.status = 'complete'
        self.txn.save()
        with mock.patch('core.views_billing.paystack.verify_transaction') as verify_mock:
            response = self.client.post('/api/v1/billing/confirm/', {'reference': 'ref-123'}, format='json')
            verify_mock.assert_not_called()
        self.assertEqual(response.status_code, 200)


class PaystackWebhookViewTests(BillingViewTestCase):
    def setUp(self):
        super().setUp()
        self.txn = BillingTransaction.objects.create(
            company=self.company, amount=MONTHLY_FEE, payment_id='ref-999', status='pending', plan='pro',
        )

    def _post_signed(self, body: dict):
        from django.conf import settings
        raw = json.dumps(body).encode()
        # Read the actual configured key (whatever's in .env locally) rather
        # than assuming it's empty — a real key there must not break this test.
        secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
        sig = hmac.new(secret_key.encode(), raw, hashlib.sha512).hexdigest()
        return self.client.post(
            '/api/v1/billing/webhook/', data=raw, content_type='application/json',
            HTTP_X_PAYSTACK_SIGNATURE=sig,
        )

    def test_invalid_signature_is_rejected(self):
        response = self.client.post(
            '/api/v1/billing/webhook/', data=json.dumps({'event': 'charge.success'}), content_type='application/json',
            HTTP_X_PAYSTACK_SIGNATURE='bogus',
        )
        self.assertEqual(response.status_code, 400)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'pending')

    def test_valid_charge_success_activates_company(self):
        response = self._post_signed({'event': 'charge.success', 'data': _verified_charge_data(reference='ref-999')})
        self.assertEqual(response.status_code, 200)
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'active')

    def test_unknown_reference_is_acknowledged_but_ignored(self):
        response = self._post_signed({'event': 'charge.success', 'data': _verified_charge_data(reference='no-such-ref')})
        self.assertEqual(response.status_code, 200)  # ack so Paystack stops retrying
        self.company.refresh_from_db()
        self.assertNotEqual(self.company.subscription_status, 'active')

    @mock.patch('core.services.notify.notify_company_billing_email')
    def test_charge_failed_marks_transaction_failed_and_emails(self, email_mock):
        # The safety net for a decline the shopper simply abandons — Paystack's
        # hosted checkout doesn't auto-redirect back to the app on a decline
        # the way it does on success, so without this the app (and the
        # customer) would never find out.
        response = self._post_signed({
            'event': 'charge.failed',
            'data': _verified_charge_data(reference='ref-999', status='failed'),
        })
        self.assertEqual(response.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'failed')
        email_mock.assert_called_once()

    @mock.patch('core.services.notify.notify_company_billing_email')
    def test_charge_failed_is_not_double_emailed_by_a_duplicate_webhook(self, email_mock):
        # Paystack can and does redeliver webhooks — a retry for the same
        # event must not send a second "payment failed" email.
        body = {'event': 'charge.failed', 'data': _verified_charge_data(reference='ref-999', status='failed')}
        self._post_signed(body)
        self._post_signed(body)
        email_mock.assert_called_once()

    def test_charge_failed_for_unknown_reference_is_acknowledged_but_ignored(self):
        response = self._post_signed({
            'event': 'charge.failed',
            'data': _verified_charge_data(reference='no-such-ref', status='failed'),
        })
        self.assertEqual(response.status_code, 200)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, 'pending')  # untouched — a different reference


class BillingStatusViewTests(BillingViewTestCase):
    def test_free_company_status(self):
        response = self.client.get('/api/v1/billing/status/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['flat_plan']['amount'], str(MONTHLY_FEE))
        self.assertIsNone(response.data['card'])

    def test_flat_plan_discloses_take_rate_before_signup(self):
        # The 0.25% take-rate must be visible on the FREE/pre-signup response
        # too, not just after a card is added — that's the whole point.
        response = self.client.get('/api/v1/billing/status/')
        self.assertEqual(response.data['flat_plan']['take_rate_pct'], '0.25')

    def test_active_company_shows_card_details(self):
        self.company.subscription_plan = 'pro'
        self.company.subscription_status = 'active'
        self.company.paystack_card_last4 = '1381'
        self.company.paystack_card_type = 'visa'
        self.company.paystack_bank = 'TEST BANK'
        self.company.save()
        response = self.client.get('/api/v1/billing/status/')
        self.assertEqual(response.data['amount'], str(MONTHLY_FEE))
        self.assertEqual(response.data['card']['last4'], '1381')


class BillingHistoryViewTests(BillingViewTestCase):
    def test_merges_subscription_and_delivery_fee_charges(self):
        from core.models import BillingTransaction, Customer, DeliveryFeeCharge, Invoice, Load
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone

        BillingTransaction.objects.create(
            company=self.company, amount=MONTHLY_FEE, payment_id='ref-1', status='complete', plan='pro',
        )
        customer = Customer.objects.create(
            name='Acme', email='acme-hist@example.com', phone='0110000000',
            address='1 Main Rd', city='JHB', state='GP', zip_code='2000', company=self.company,
        )
        now = timezone.now()
        load = Load.objects.create(
            company=self.company, load_number='LOAD-HIST-1', customer=customer,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=now, delivery_location='CPT', delivery_city='CPT', delivery_state='WC',
            delivery_zip='8000', delivery_date=now + timedelta(days=1),
            cargo_description='Freight', weight=Decimal('500.00'),
            distance=Decimal('600.00'), rate=Decimal('5000.00'),
            total_amount=Decimal('5000.00'), status='DELIVERED',
        )
        invoice = Invoice.objects.create(
            invoice_number='INV-HIST-1', company=self.company, customer=customer, load=load,
            issue_date=now.date(), due_date=now.date() + timedelta(days=30),
            subtotal=Decimal('5000.00'), vat_amount=Decimal('750.00'), tax_amount=Decimal('750.00'),
            total_amount=Decimal('5750.00'), paid_amount=Decimal('0'), balance=Decimal('5750.00'),
            status='SENT', payment_terms='NET30',
        )
        DeliveryFeeCharge.objects.create(
            company=self.company, invoice=invoice, rate_pct=Decimal('0.25'),
            base_amount=invoice.total_amount, amount=Decimal('14.38'), status='charged',
        )

        response = self.client.get('/api/v1/billing/history/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        kinds = {r['kind'] for r in response.data['results']}
        self.assertEqual(kinds, {'subscription', 'delivery_fee'})
        fee_row = next(r for r in response.data['results'] if r['kind'] == 'delivery_fee')
        self.assertEqual(fee_row['amount'], '14.38')
        self.assertEqual(fee_row['status'], 'complete')  # normalised from 'charged'
        self.assertIn('INV-HIST-1', fee_row['label'])
