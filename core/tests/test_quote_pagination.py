"""Tests for the quotes board's per-column backend pagination: page_size
override, the status filter (including legacy IT/COMPLETED folding into the
Accepted column), the total_amount aggregate, and search — all added so the
board can fetch each pipeline column independently (10 at a time, "load
more") instead of pulling every quote up front and slicing it client-side.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, Quote

User = get_user_model()


def make_quote(company, customer, *, number, total=1000, status='DRAFT'):
    return Quote.objects.create(
        company=company, customer=customer, quote_number=number,
        pickup_location='Johannesburg', delivery_location='Cape Town',
        origin='JHB', destination='CPT',
        cargo_description='test cargo', weight=Decimal('20000'),
        base_rate=Decimal('500'), fuel_surcharge=Decimal('400'),
        toll_charges=Decimal('50'), driver_allowance=Decimal('30'),
        additional_charges=Decimal('20'),
        total_amount=Decimal(str(total)),
        valid_until=date.today() + timedelta(days=14),
        status=status,
    )


class QuotePaginationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Pagination Co')
        self.customer = Customer.objects.create(
            company=self.company, name='Shipper', email='s@example.com',
            phone='', address='', city='', state='', zip_code='',
        )
        self.user = User.objects.create_user(
            username='paginationuser', email='pagination@test.com', password='testpass123',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    def test_page_size_query_param_overrides_default(self):
        for i in range(15):
            make_quote(self.company, self.customer, number=f'Q-DRAFT-{i}', status='DRAFT')
        response = self.client.get('/api/v1/quotes/?status=DRAFT&page_size=10')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 10)
        self.assertEqual(response.data['count'], 15)
        self.assertIsNotNone(response.data['next'])

    def test_second_page_returns_remainder(self):
        for i in range(15):
            make_quote(self.company, self.customer, number=f'Q-DRAFT-{i}', status='DRAFT')
        response = self.client.get('/api/v1/quotes/?status=DRAFT&page_size=10&page=2')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 5)
        self.assertIsNone(response.data['next'])

    def test_status_filter_scopes_to_that_column_only(self):
        make_quote(self.company, self.customer, number='Q-D1', status='DRAFT')
        make_quote(self.company, self.customer, number='Q-S1', status='SENT')
        response = self.client.get('/api/v1/quotes/?status=DRAFT&page_size=10')
        numbers = {r['quote_number'] for r in response.data['results']}
        self.assertEqual(numbers, {'Q-D1'})

    def test_accepted_filter_includes_legacy_it_and_completed(self):
        make_quote(self.company, self.customer, number='Q-ACC', status='ACCEPTED')
        make_quote(self.company, self.customer, number='Q-IT', status='IT')
        make_quote(self.company, self.customer, number='Q-DONE', status='COMPLETED')
        make_quote(self.company, self.customer, number='Q-DRAFT', status='DRAFT')
        response = self.client.get('/api/v1/quotes/?status=ACCEPTED&page_size=10')
        numbers = {r['quote_number'] for r in response.data['results']}
        self.assertEqual(numbers, {'Q-ACC', 'Q-IT', 'Q-DONE'})
        self.assertEqual(response.data['count'], 3)

    def test_declined_filter_does_not_pick_up_legacy_statuses(self):
        make_quote(self.company, self.customer, number='Q-IT', status='IT')
        response = self.client.get('/api/v1/quotes/?status=DECLINED&page_size=10')
        self.assertEqual(response.data['count'], 0)

    def test_total_amount_sums_the_whole_filtered_set_not_just_the_page(self):
        for i in range(12):
            make_quote(self.company, self.customer, number=f'Q-{i}', total=100, status='DRAFT')
        response = self.client.get('/api/v1/quotes/?status=DRAFT&page_size=10')
        # 12 quotes at R100 each = R1200, even though only 10 are on this page.
        self.assertEqual(Decimal(response.data['total_amount']), Decimal('1200'))

    def test_search_scoped_within_the_status_filter(self):
        make_quote(self.company, self.customer, number='Q-FINDME', status='DRAFT')
        make_quote(self.company, self.customer, number='Q-OTHER', status='DRAFT')
        response = self.client.get('/api/v1/quotes/?status=DRAFT&page_size=10&search=FINDME')
        numbers = {r['quote_number'] for r in response.data['results']}
        self.assertEqual(numbers, {'Q-FINDME'})
