"""The customer email for a quote going SENT goes out only once the save has
committed: a save that rolls back never emails the customer, a good save
emails exactly once, and send_to_customer still reports the send."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from core.models import Company, Quote
from core.tests.test_price_analysis import make_quote
from core.tests.test_pricing_analysis import _Base, _DecisionHelpers, make_customer

SENDER = 'core.services.quote_share.send_quote_to_customer_email'


class SendEmailOnCommitTests(_DecisionHelpers, _Base):
    def draft(self):
        return make_quote(self.company, self.customer, number=f'OC-{Quote.objects.count()}', status='DRAFT',
                          created_by=self.user)

    def test_rolled_back_save_sends_no_email(self):
        q = self.draft()
        with mock.patch(SENDER, return_value=(True, 'kestrel@x.test')) as send, \
                mock.patch('core.services.pricing_decisions.save_pricing_decision', side_effect=DatabaseError('busy')), \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'SENT', 'total_amount': str(q.total_amount),
                                                              'pricing_decision': self.decision(
                                                                  final_price=float(q.total_amount))}, format='json')
        self.assertGreaterEqual(resp.status_code, 500, resp.content)
        self.assertEqual(Quote.objects.get(id=q.id).status, 'DRAFT')     # nothing saved...
        send.assert_not_called()                                          # ...and no email

    def test_committed_save_sends_one_email(self):
        q = self.draft()
        with mock.patch(SENDER, return_value=(True, 'kestrel@x.test')) as send, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.api.patch(f'/api/v1/quotes/{q.id}/', {'status': 'SENT'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Quote.objects.get(id=q.id).status, 'SENT')
        self.assertEqual(send.call_count, 1)


class SendToCustomerReportsTheSendTests(TransactionTestCase):
    """Real commits, as in production: the Send button's response still says
    whether the email went out."""

    def test_send_button_reports_email_sent(self):
        company = Company.objects.create(company_name='Commit Co')
        user = get_user_model().objects.create_user(username='commit_user', password='x', company=company)
        customer = make_customer(company, 'Hornbill Foods', email='orders@hornbill.test')
        q = make_quote(company, customer, number='OC-T-1', status='DRAFT', created_by=user)
        api = APIClient()
        api.force_authenticate(user=user)
        with mock.patch(SENDER, return_value=(True, 'orders@hornbill.test')) as send:
            resp = api.post(f'/api/v1/quotes/{q.id}/send_to_customer/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['email_sent'])
        self.assertEqual(resp.json()['customer_email'], 'orders@hornbill.test')
        self.assertEqual(send.call_count, 1)
