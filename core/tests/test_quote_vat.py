"""Customer-facing quotes show price excl. VAT, VAT and total incl. VAT
(core/services/quote_vat.py), the same on the PDF, emails, quote page and API."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, Customer, Quote
from core.services.quote_vat import quote_vat, vat_label

User = get_user_model()


class QuoteVatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='VAT Co')
        cls.user = User.objects.create_user(username='vat_admin', email='v@vat.test', password='x')
        cls.user.role = 'ADMIN'
        cls.user.company = cls.co
        cls.user.save()
        cls.customer = Customer.objects.create(
            company=cls.co, name='VAT Customer', email='c@vat.test', phone='', address='', city='CPT',
            state='', zip_code='', credit_score=85, credit_score_source='MANUAL')
        cls.quote = Quote.objects.create(
            company=cls.co, customer=cls.customer, quote_number='QT-VAT-1', token='tok-vat-1',
            pickup_location='Cape Town', delivery_location='Maputo', cargo_description='20t', weight=Decimal('20000'),
            base_rate=Decimal('60000'), fuel_surcharge=Decimal('20000'), toll_charges=Decimal('3193.03'),
            driver_allowance=Decimal('0'), additional_charges=Decimal('0'), total_amount=Decimal('83193.03'),
            valid_until=date.today() + timedelta(days=7), status='SENT')

    def test_fifteen_percent_rounded_to_the_cent(self):
        v = quote_vat(self.quote)
        self.assertEqual((v['subtotal'], v['vat'], v['total']),
                         (Decimal('83193.03'), Decimal('12478.95'), Decimal('95671.98')))
        self.assertEqual(vat_label(v), 'VAT (15%)')

    def test_not_vat_registered_has_no_vat(self):
        Company.objects.filter(pk=self.co.pk).update(vat_registered=False)
        self.quote.company.refresh_from_db()
        v = quote_vat(self.quote)
        self.assertFalse(v['vat_registered'])
        self.assertEqual(v['total'], Decimal('83193.03'))

    def test_public_quote_page_and_quote_api_carry_the_breakdown(self):
        r = APIClient().get(f'/api/v1/quotes/public/{self.quote.id}/{self.quote.token}/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body['subtotal_excl_vat'], body['vat_amount'], body['total_incl_vat']),
                         ('83193.03', '12478.95', '95671.98'))
        c = APIClient(); c.force_authenticate(self.user)
        self.assertEqual(c.get(f'/api/v1/quotes/{self.quote.id}/').json()['customer_price']['total_incl_vat'], '95671.98')

    def test_share_email_lists_price_vat_and_total(self):
        from core.services import email_service
        with mock.patch('resend.Emails.send', return_value={'id': 't'}) as send, \
                self.settings(RESEND_API_KEY='re_test', EMAIL_DELIVERY='resend'):
            email_service.send_quote_share_email(self.quote, 'https://example.test/q')
        html = send.call_args.args[0]['html']
        for text in ('Price excl. VAT', 'VAT (15%)', 'Total incl. VAT', '12 478,95', '95 671,98'):
            self.assertIn(text, html)
        self.assertNotIn('Total excl. VAT', html)


class InternationalZeroRatedTests(TestCase):
    """International transport is zero-rated: the quote shows VAT 0%, and the
    invoice the load becomes is zero-rated too, so quote and invoice agree."""

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(company_name='Intl Co', subscription_status='active')
        cls.customer = Customer.objects.create(
            company=cls.co, name='Harare Buyer', email='h@intl.test', phone='', address='', city='CPT',
            state='', zip_code='', credit_score=85, credit_score_source='MANUAL')

    def _quote(self, international):
        return Quote.objects.create(
            company=self.co, customer=self.customer, quote_number=f'QT-INTL-{int(international)}',
            pickup_location='Cape Town', delivery_location='Harare', cargo_description='20t', weight=Decimal('20000'),
            base_rate=Decimal('100000'), fuel_surcharge=Decimal('0'), toll_charges=Decimal('0'),
            driver_allowance=Decimal('0'), additional_charges=Decimal('0'), total_amount=Decimal('100000'),
            valid_until=date.today() + timedelta(days=7), status='ACCEPTED', is_international=international)

    def test_international_quote_shows_zero_rated_vat(self):
        v = quote_vat(self._quote(True))
        self.assertEqual((v['vat'], v['total']), (Decimal('0.00'), Decimal('100000.00')))
        self.assertEqual(vat_label(v), 'VAT 0% (zero-rated international transport)')
        self.assertEqual(quote_vat(self._quote(False))['vat'], Decimal('15000.00'))

    def test_international_load_is_invoiced_zero_rated(self):
        from django.utils import timezone
        from core.models import Load
        from core.services.invoicing import create_invoice_for_load
        quote = self._quote(True)
        load = Load.objects.create(
            company=self.co, customer=self.customer, quote=quote, load_number='L-INTL-1',
            is_international=quote.is_international,
            pickup_location='Cape Town', pickup_city='CPT', pickup_state='WC', pickup_zip='8000',
            pickup_date=timezone.now(), delivery_location='Harare', delivery_city='HRE', delivery_state='',
            delivery_zip='', delivery_date=timezone.now(), cargo_description='20t', weight=Decimal('20000'),
            distance=Decimal('2000'), rate=Decimal('100000'), total_amount=Decimal('100000'), status='DELIVERED')
        invoice, created = create_invoice_for_load(load)
        self.assertTrue(created)
        self.assertEqual(invoice.vat_amount, Decimal('0.00'))
        self.assertEqual(invoice.lines.first().tax_code, 'ZERO_RATED')

    def test_backfill_reads_the_saved_route(self):
        import importlib
        m = importlib.import_module('core.migrations.0142_quote_load_is_international')
        self.assertTrue(m._route_is_international({'response': {'cross_border': True}}))
        self.assertTrue(m._route_is_international({'response': {'routes': [{'country_codes': ['ZA', 'ZW']}]}}))
        self.assertFalse(m._route_is_international({'response': {'countries': ['ZA'], 'routes': [{'country_codes': ['ZA']}]}}))
        self.assertFalse(m._route_is_international({}))


class OrderVatTests(InternationalZeroRatedTests):
    """The order (Load) API carries the same breakdown as its quote."""

    def test_order_api_shows_vat_and_total(self):
        from django.utils import timezone
        from core.models import Load
        user = User.objects.create_user(username='intl_admin', email='a@intl.test', password='x')
        user.role = 'ADMIN'; user.company = self.co; user.save()
        c = APIClient(); c.force_authenticate(user)
        for intl, vat, total in ((False, '15000.00', '115000.00'), (True, '0.00', '100000.00')):
            load = Load.objects.create(
                company=self.co, customer=self.customer, load_number=f'L-VAT-{int(intl)}', is_international=intl,
                pickup_location='Cape Town', pickup_city='CPT', pickup_state='WC', pickup_zip='8000',
                pickup_date=timezone.now(), delivery_location='X', delivery_city='X', delivery_state='', delivery_zip='',
                delivery_date=timezone.now(), cargo_description='20t', weight=Decimal('20000'), distance=Decimal('100'),
                rate=Decimal('100000'), total_amount=Decimal('100000'), status='PENDING')
            p = c.get(f'/api/v1/loads/{load.id}/').json()['customer_price']
            self.assertEqual((p['vat_amount'], p['total_incl_vat']), (vat, total))


class ListTotalInclVatTests(TestCase):
    def test_list_total_equals_sum_of_rows(self):
        co = Company.objects.create(company_name='Sum Co')
        user = User.objects.create_user(username='sum_admin', email='s@sum.test', password='x')
        user.role = 'ADMIN'; user.company = co; user.save()
        cust = Customer.objects.create(company=co, name='Sum C', email='c@sum.test', phone='', address='', city='CPT',
                                       state='', zip_code='', credit_score=85, credit_score_source='MANUAL')
        for n, amt, intl in ((1, '1000.01', False), (2, '333.33', False), (3, '5000.00', True)):
            Quote.objects.create(company=co, customer=cust, quote_number=f'QT-SUM-{n}', pickup_location='A',
                                 delivery_location='B', cargo_description='x', weight=Decimal('1'), base_rate=Decimal(amt),
                                 fuel_surcharge=Decimal('0'), toll_charges=Decimal('0'), driver_allowance=Decimal('0'),
                                 additional_charges=Decimal('0'), total_amount=Decimal(amt), is_international=intl,
                                 valid_until=date.today() + timedelta(days=7), status='DRAFT')
        c = APIClient(); c.force_authenticate(user)
        body = c.get('/api/v1/quotes/?status=DRAFT').json()
        rows = sum(Decimal(r['customer_price']['total_incl_vat']) for r in body['results'])
        # 1150.01 + 383.33 + 5000.00 (zero-rated)
        self.assertEqual(Decimal(str(body['total_incl_vat'])), Decimal('6533.34'))
        self.assertEqual(rows, Decimal('6533.34'))
