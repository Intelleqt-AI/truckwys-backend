"""The public accept/decline endpoint must refuse an expired quote itself,
not rely on the customer-facing page's UI to stop the request before it's
sent. Checked directly against valid_until (not just status == 'EXPIRED') so
there's no window between a quote going stale and the next expiry sweep run.
"""

from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Company, Customer
from core.tests.test_price_analysis import make_quote

User = get_user_model()


class PublicQuoteExpiryTests(TestCase):

    def setUp(self):
        self.company = Company.objects.create(company_name='Expiry Resp Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Cust', email='cust@expiryresp.test')
        self.user = User.objects.create_user(
            username='exp_resp', email='exp_resp@test.com', password='x', company=self.company)
        self.client = APIClient()

    def _respond(self, quote, action='accept'):
        return self.client.post(
            f'/api/v1/quotes/public/{quote.id}/{quote.token}/respond/',
            {'action': action}, format='json')

    def test_expired_by_date_cannot_be_accepted(self):
        quote = make_quote(
            self.company, self.customer, number='PUB-EXP-001', status='SENT',
            valid_until=date.today() - timedelta(days=1), created_by=self.user)
        resp = self._respond(quote)
        self.assertEqual(resp.status_code, status.HTTP_410_GONE, resp.content)
        self.assertTrue(resp.json()['expired'])
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'SENT')

    def test_expired_by_date_cannot_be_declined(self):
        quote = make_quote(
            self.company, self.customer, number='PUB-EXP-002', status='SENT',
            valid_until=date.today() - timedelta(days=1), created_by=self.user)
        resp = self._respond(quote, action='decline')
        self.assertEqual(resp.status_code, status.HTTP_410_GONE, resp.content)

    def test_status_already_expired_is_rejected(self):
        """Belt and braces: if the daily sweep already flipped status to
        EXPIRED, that must be caught too, not just a fresh date comparison."""
        quote = make_quote(
            self.company, self.customer, number='PUB-EXP-003', status='EXPIRED',
            valid_until=date.today() - timedelta(days=5), created_by=self.user)
        resp = self._respond(quote)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.content)
        self.assertTrue(resp.json()['already_responded'])

    def test_quote_valid_today_can_still_be_accepted(self):
        quote = make_quote(
            self.company, self.customer, number='PUB-EXP-004', status='SENT',
            valid_until=date.today(), created_by=self.user)
        resp = self._respond(quote)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'ACCEPTED')

    def test_quote_valid_in_future_can_still_be_accepted(self):
        quote = make_quote(
            self.company, self.customer, number='PUB-EXP-005', status='SENT',
            valid_until=date.today() + timedelta(days=7), created_by=self.user)
        resp = self._respond(quote)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
