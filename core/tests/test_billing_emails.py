"""Tests for the mandatory billing email guarantee: every payment event
(success, failure, cancellation, freeze) emails the company's admins,
regardless of their notification preferences — unlike the optional
activity-notification emails, which ARE preference-gated.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Company
from core.services.notify import notify_company_billing_email

User = get_user_model()


class NotifyCompanyBillingEmailTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_name='Billing Email Co')
        self.admin = User.objects.create_user(
            username='admin1', email='admin1@example.com', password='x', role='ADMIN',
        )
        self.admin.company = self.company
        self.admin.save()
        # Explicitly disable the "payments" email preference — mandatory
        # billing emails must ignore this entirely.
        self.admin.notification_settings = {'email': {'payments': False}}
        self.admin.save()

    @mock.patch('core.services.email_service.resend.Emails.send')
    def test_sends_to_admin_even_with_payments_email_preference_disabled(self, send_mock):
        notify_company_billing_email(self.company.id, 'Test billing event', 'Something happened.')
        send_mock.assert_called_once()
        self.assertEqual(send_mock.call_args.args[0]['to'], ['admin1@example.com'])

    @mock.patch('core.services.email_service.resend.Emails.send')
    def test_never_emails_non_admin_users(self, send_mock):
        operator = User.objects.create_user(
            username='op1', email='op1@example.com', password='x', role='OPERATOR', company=self.company,
        )
        notify_company_billing_email(self.company.id, 'Test billing event', 'Something happened.')
        sent_to = [c.args[0]['to'] for c in send_mock.call_args_list]
        self.assertNotIn(['op1@example.com'], sent_to)

    @mock.patch('core.services.email_service.resend.Emails.send', side_effect=Exception('resend down'))
    def test_never_raises_even_if_resend_fails(self, send_mock):
        notify_company_billing_email(self.company.id, 'Test billing event', 'Something happened.')  # must not raise

    def test_no_company_id_is_a_noop(self):
        notify_company_billing_email(None, 'Test', 'Test')  # must not raise
