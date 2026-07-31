"""Tests for the 0.25% delivery take-rate: ad-hoc Paystack charging, retry/
grace-period escalation to suspension, and the suspended-account gating in
PlanLimitsMiddleware. All Paystack HTTP calls are mocked — no network access.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company, Customer, DeliveryFeeCharge, Invoice, Load
from core.services.delivery_fee_billing import (
    charge_delivery_fee_for_invoice,
    retry_failed_delivery_fee_charges,
)
from core.services.paystack import charge_authorization, verify_webhook_signature
from core.services.subscription_billing import check_grace_period_expirations

User = get_user_model()


def _ok_response(amount_cents=2875):
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        'status': True, 'message': 'Charge attempted',
        'data': {'id': 1, 'status': 'success', 'reference': 'ref-x', 'amount': amount_cents},
    }
    return resp


def _declined_response():
    """A 2xx envelope (status: true — the API call itself worked) describing
    a declined charge (data.status != success) — the case Paystack's own
    docs warn about: the outer envelope isn't the charge outcome."""
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        'status': True, 'message': 'Charge attempted',
        'data': {'id': 2, 'status': 'failed', 'gateway_response': 'Insufficient Funds'},
    }
    return resp


def _api_error_response():
    resp = mock.Mock(status_code=400)
    resp.json.return_value = {'status': False, 'message': 'Authorization could not be verified'}
    return resp


class WebhookSignatureTests(TestCase):
    def test_valid_signature_accepted(self):
        import hashlib, hmac
        from django.conf import settings
        body = b'{"event":"charge.success"}'
        sig = hmac.new(getattr(settings, 'PAYSTACK_SECRET_KEY', '').encode(), body, hashlib.sha512).hexdigest()
        self.assertTrue(verify_webhook_signature(body, sig))

    def test_tampered_body_rejected(self):
        import hashlib, hmac
        from django.conf import settings
        sig = hmac.new(getattr(settings, 'PAYSTACK_SECRET_KEY', '').encode(), b'original', hashlib.sha512).hexdigest()
        self.assertFalse(verify_webhook_signature(b'tampered', sig))

    def test_missing_signature_rejected(self):
        self.assertFalse(verify_webhook_signature(b'{}', ''))


class ChargeAuthorizationTests(TestCase):
    def test_no_authorization_code_short_circuits_without_network_call(self):
        with mock.patch('core.services.paystack.requests.request') as req:
            result = charge_authorization(None, 'a@test.com', Decimal('25.00'))
        req.assert_not_called()
        self.assertFalse(result['success'])

    @mock.patch('core.services.paystack.requests.request')
    def test_success_sends_bearer_auth_and_cents(self, req):
        req.return_value = _ok_response()
        result = charge_authorization('AUTH_abc', 'a@test.com', Decimal('28.75'))
        self.assertTrue(result['success'])
        args, kwargs = req.call_args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], 'https://api.paystack.co/transaction/charge_authorization')
        self.assertIn('Bearer', kwargs['headers']['Authorization'])
        self.assertEqual(kwargs['json']['amount'], 2875)  # rand -> cents
        self.assertEqual(kwargs['json']['authorization_code'], 'AUTH_abc')

    @mock.patch('core.services.paystack.requests.request')
    def test_declined_charge_in_2xx_envelope_is_a_failure(self, req):
        # Confirmed from Paystack's own docs: the outer {status:true} envelope
        # only means the API call was accepted — data.status is the real outcome.
        req.return_value = _declined_response()
        result = charge_authorization('AUTH_abc', 'a@test.com', Decimal('10.00'))
        self.assertFalse(result['success'])
        self.assertIn('Insufficient Funds', result['error'])

    @mock.patch('core.services.paystack.requests.request')
    def test_api_level_error_is_a_failure(self, req):
        req.return_value = _api_error_response()
        result = charge_authorization('AUTH_abc', 'a@test.com', Decimal('10.00'))
        self.assertFalse(result['success'])

    @mock.patch('core.services.paystack.requests.request', side_effect=requests.ConnectionError('network down'))
    def test_network_error_never_raises(self, req):
        result = charge_authorization('AUTH_abc', 'a@test.com', Decimal('10.00'))
        self.assertFalse(result['success'])
        self.assertIn('network down', result['error'])


class DeliveryFeeBillingTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            company_name='Take Rate Co',
            subscription_status='active',
            paystack_authorization_code='AUTH_xyz',
            paystack_authorization_email='billing@takerate.test',
        )
        self.customer = Customer.objects.create(
            name='Acme Shipper', email='acme@example.com', phone='0110000000',
            address='1 Main Rd', city='JHB', state='GP', zip_code='2000', company=self.company,
        )
        now = timezone.now()
        self.load = Load.objects.create(
            company=self.company, load_number='LOAD-TR-1', customer=self.customer,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=now, delivery_location='CPT', delivery_city='CPT', delivery_state='WC',
            delivery_zip='8000', delivery_date=now + timedelta(days=1),
            cargo_description='Freight', weight=Decimal('1000.00'),
            distance=Decimal('1400.00'), rate=Decimal('10000.00'),
            total_amount=Decimal('10000.00'), status='ASSIGNED',
        )
        self.invoice = Invoice.objects.create(
            invoice_number='INV-TR-1', company=self.company, customer=self.customer, load=self.load,
            issue_date=now.date(), due_date=now.date() + timedelta(days=30),
            subtotal=Decimal('10000.00'), vat_amount=Decimal('1500.00'), tax_amount=Decimal('1500.00'),
            total_amount=Decimal('11500.00'), paid_amount=Decimal('0'), balance=Decimal('11500.00'),
            status='SENT', payment_terms='NET30',
        )


class ChargeDeliveryFeeForInvoiceTests(DeliveryFeeBillingTestCase):
    @mock.patch('core.services.paystack.requests.request')
    def test_computes_quarter_percent_and_marks_charged_on_success(self, req):
        req.return_value = _ok_response(amount_cents=2875)
        charge = charge_delivery_fee_for_invoice(self.invoice)
        self.assertEqual(charge.amount, Decimal('28.75'))  # 0.25% of 11500.00
        self.assertEqual(charge.status, 'charged')
        self.assertIsNotNone(charge.charged_at)

    @mock.patch('core.services.paystack.requests.request')
    def test_is_idempotent_never_double_charges(self, req):
        req.return_value = _ok_response()
        charge_delivery_fee_for_invoice(self.invoice)
        self.assertEqual(req.call_count, 1)
        charge_delivery_fee_for_invoice(self.invoice)  # e.g. a retry of the signal
        self.assertEqual(req.call_count, 1)  # no second network call
        self.assertEqual(DeliveryFeeCharge.objects.filter(invoice=self.invoice).count(), 1)

    @mock.patch('core.services.paystack.requests.request')
    def test_failure_marks_failed_and_enters_grace_period(self, req):
        req.return_value = _declined_response()
        charge = charge_delivery_fee_for_invoice(self.invoice)
        self.assertEqual(charge.status, 'failed')
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'grace_period')
        self.assertIsNotNone(self.company.grace_period_expires_at)

    @mock.patch('core.services.paystack.requests.request')
    def test_suspended_company_is_never_charged(self, req):
        self.company.subscription_status = 'suspended'
        self.company.save()
        charge_delivery_fee_for_invoice(self.invoice)
        req.assert_not_called()

    @mock.patch('core.services.paystack.requests.request')
    def test_grace_period_company_is_still_charged_as_health_check(self, req):
        # spec §3: a grace_period company keeps getting charge attempts —
        # a success is exactly how it recovers back to 'active'.
        req.return_value = _ok_response()
        self.company.subscription_status = 'grace_period'
        self.company.grace_period_expires_at = timezone.now() + timedelta(days=3)
        self.company.save()
        charge = charge_delivery_fee_for_invoice(self.invoice)
        req.assert_called_once()
        self.assertEqual(charge.status, 'charged')
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'active')
        self.assertIsNone(self.company.grace_period_expires_at)

    @mock.patch('core.services.paystack.requests.request')
    def test_free_tier_company_is_never_charged_or_recorded(self, req):
        # A company that never subscribed (no card, subscription_status
        # defaults to 'none') never agreed to the take-rate — it must not be
        # charged, and no failed/pending DeliveryFeeCharge row should appear
        # for them (that would be a false "we owe money" record for someone
        # who never opted in).
        self.company.subscription_status = 'none'
        self.company.paystack_authorization_code = ''
        self.company.save()
        result = charge_delivery_fee_for_invoice(self.invoice)
        req.assert_not_called()
        self.assertIsNone(result)
        self.assertFalse(DeliveryFeeCharge.objects.filter(invoice=self.invoice).exists())

    @mock.patch('core.services.paystack.requests.request')
    def test_cancelled_subscription_is_never_charged(self, req):
        # Even with a card still on file, a cancelled subscription shouldn't
        # keep incurring the take-rate — it's bundled with the paid plan.
        self.company.subscription_status = 'cancelled'
        self.company.save()
        result = charge_delivery_fee_for_invoice(self.invoice)
        req.assert_not_called()
        self.assertIsNone(result)


class RetryFailedDeliveryFeeChargesTests(DeliveryFeeBillingTestCase):
    def _make_failed_charge(self):
        return DeliveryFeeCharge.objects.create(
            company=self.company, invoice=self.invoice, rate_pct=Decimal('0.25'),
            base_amount=self.invoice.total_amount, amount=Decimal('28.75'),
            status='failed', attempt_count=1,
        )

    @mock.patch('core.services.paystack.requests.request')
    def test_retry_success_marks_charged_and_returns_company_to_active(self, req):
        req.return_value = _ok_response()
        self.company.subscription_status = 'grace_period'
        self.company.grace_period_expires_at = timezone.now() + timedelta(days=3)
        self.company.save()
        self._make_failed_charge()
        summary = retry_failed_delivery_fee_charges()
        self.assertEqual(summary['charged'], 1)
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'active')
        self.assertIsNone(self.company.grace_period_expires_at)

    @mock.patch('core.services.paystack.requests.request')
    def test_retry_still_failing_company_already_in_grace_period(self, req):
        req.return_value = _declined_response()
        self.company.subscription_status = 'grace_period'
        self.company.grace_period_expires_at = timezone.now() + timedelta(days=3)
        self.company.save()
        self._make_failed_charge()
        summary = retry_failed_delivery_fee_charges()
        self.assertEqual(summary['still_failing'], 1)
        self.assertEqual(summary['entered_grace'], 0)  # already there, not a new transition
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'grace_period')

    @mock.patch('core.services.paystack.requests.request')
    def test_retry_first_failure_enters_grace_period(self, req):
        req.return_value = _declined_response()
        self._make_failed_charge()  # company still 'active' from setUp
        summary = retry_failed_delivery_fee_charges()
        self.assertEqual(summary['entered_grace'], 1)
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'grace_period')
        self.assertIsNotNone(self.company.grace_period_expires_at)

    @mock.patch('core.services.paystack.requests.request')
    def test_suspended_company_is_skipped_not_retried(self, req):
        self.company.subscription_status = 'suspended'
        self.company.save()
        self._make_failed_charge()
        summary = retry_failed_delivery_fee_charges()
        req.assert_not_called()
        self.assertEqual(summary['skipped_not_billable'], 1)


class GracePeriodExpirationTests(DeliveryFeeBillingTestCase):
    def test_expired_grace_period_suspends_company(self):
        self.company.subscription_status = 'grace_period'
        self.company.grace_period_expires_at = timezone.now() - timedelta(days=1)
        self.company.save()
        summary = check_grace_period_expirations()
        self.assertEqual(summary['suspended'], 1)
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'suspended')

    def test_grace_period_not_yet_expired_is_untouched(self):
        self.company.subscription_status = 'grace_period'
        self.company.grace_period_expires_at = timezone.now() + timedelta(days=1)
        self.company.save()
        summary = check_grace_period_expirations()
        self.assertEqual(summary['suspended'], 0)
        self.company.refresh_from_db()
        self.assertEqual(self.company.subscription_status, 'grace_period')


class SignalIntegrationTests(DeliveryFeeBillingTestCase):
    @mock.patch('core.services.paystack.requests.request')
    def test_load_delivered_creates_invoice_and_charges_take_rate(self, req):
        req.return_value = _ok_response(amount_cents=1250)
        # A fresh load (no invoice/charge yet) transitioning to DELIVERED —
        # _auto_invoice_on_delivery only fires on the UPDATE path, not create.
        load = Load.objects.create(
            company=self.company, load_number='LOAD-TR-2', customer=self.customer,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=timezone.now(), delivery_location='DBN', delivery_city='DBN', delivery_state='KZN',
            delivery_zip='4000', delivery_date=timezone.now() + timedelta(days=1),
            cargo_description='Freight', weight=Decimal('500.00'),
            distance=Decimal('600.00'), rate=Decimal('5000.00'),
            total_amount=Decimal('5000.00'), status='ASSIGNED',
        )
        load.status = 'DELIVERED'
        load.save()

        invoice = Invoice.objects.get(load=load)
        charge = DeliveryFeeCharge.objects.get(invoice=invoice)
        self.assertEqual(charge.status, 'charged')
        self.assertEqual(charge.amount, (invoice.total_amount * Decimal('0.0025')).quantize(Decimal('0.01')))


class InvoiceSerializerDeliveryFeeChargeTests(DeliveryFeeBillingTestCase):
    """The reverse OneToOneField (invoice.delivery_fee_charge) raises
    DoesNotExist for any invoice without a charge yet — the serializer must
    not crash the endpoint for drafts / pre-this-feature invoices."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create_user(username='invuser', email='inv@test.com', password='testpass123')
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def test_invoice_without_charge_serializes_null(self):
        response = self.client.get(f'/api/v1/invoices/{self.invoice.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['delivery_fee_charge'])

    def test_invoice_with_charge_serializes_it(self):
        DeliveryFeeCharge.objects.create(
            company=self.company, invoice=self.invoice, rate_pct=Decimal('0.25'),
            base_amount=self.invoice.total_amount, amount=Decimal('28.75'), status='charged',
        )
        response = self.client.get(f'/api/v1/invoices/{self.invoice.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['delivery_fee_charge']['amount'], '28.75')
        self.assertEqual(response.data['delivery_fee_charge']['status'], 'charged')


class SuspendedMiddlewareTests(DeliveryFeeBillingTestCase):
    """PlanLimitsMiddleware reads Django's request.user, which APIClient's
    force_authenticate() never populates (it only patches the DRF-wrapped
    request seen inside the view). A real session login is required so
    AuthenticationMiddleware sets request.user before PlanLimitsMiddleware runs.

    Spec §5: only quote create/accept and invoice generation are blocked —
    everything else (reads, drivers/vehicles/users/settings, billing) stays
    reachable so a suspended company can still fix its payment method.
    """
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user = User.objects.create_user(username='suspendeduser', email='suspended@test.com', password='testpass123')
        self.user.company = self.company
        self.user.save()
        self.client.login(username='suspendeduser', password='testpass123')
        self.company.subscription_status = 'suspended'
        self.company.save()

    def test_post_to_new_quote_is_blocked_when_suspended(self):
        # A plain Django JsonResponse (from the middleware, before the view
        # ever runs) — not a DRF Response, so parse .content, not .data.
        response = self.client.post('/api/v1/quotes/', {}, format='json')
        self.assertEqual(response.status_code, 402)
        self.assertTrue(json.loads(response.content).get('account_suspended'))

    def test_post_to_new_invoice_is_blocked_when_suspended(self):
        response = self.client.post('/api/v1/invoices/', {}, format='json')
        self.assertEqual(response.status_code, 402)

    def test_get_requests_still_work_when_suspended(self):
        response = self.client.get('/api/v1/quotes/')
        self.assertNotEqual(response.status_code, 402)

    def test_billing_post_still_works_when_suspended(self):
        # Suspended companies must still be able to fix billing to reactivate.
        response = self.client.post('/api/v1/billing/cancel/', {}, format='json')
        self.assertNotEqual(response.status_code, 402)

    def test_non_quote_non_invoice_post_still_works_when_suspended(self):
        # Narrow gate — driver/vehicle/settings management stays open.
        response = self.client.post('/api/v1/drivers/', {}, format='json')
        self.assertNotEqual(response.status_code, 402)
