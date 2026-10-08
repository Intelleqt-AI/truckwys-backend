"""One-tap booking: accepted quote -> job (+ costing) -> invoice preview, with
the return-load link offered at that moment."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Invoice, Load, Quote
from core.tests.quote_rules_fixtures import official_price_now
from core.tests.trip_fixtures import (make_company, make_customer, make_load, make_user, make_vehicle_type,
                                      priced_quote)


class BookingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        official_price_now()
        cls.co = make_company('Book Co')
        cls.other = make_company('Book Other')
        cls.user = make_user('book_u', cls.co)
        cls.cust = make_customer(cls.co, 'book')
        cls.cust_o = make_customer(cls.other, 'book-o')
        cls.vt = make_vehicle_type(cls.co)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def book(self, q, body=None):
        return self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', body or {}, format='json')

    def test_book_returns_job_costing_invoice_preview_and_candidates(self):
        outbound = make_load(self.co, self.cust, 'BK-OUT', pickup='Johannesburg', delivery='Durban',
                             pickup_in_days=0, days=1)
        q = priced_quote(self.co, self.cust, 'BK-1', vt=self.vt, origin='Durban', destination='Johannesburg')
        r = self.book(q, {'pickup_date': (date.today() + timedelta(days=2)).isoformat()})
        self.assertEqual(r.status_code, 201, r.content)
        b = r.json()['booking']
        self.assertTrue(b['created'])
        self.assertEqual(b['costing']['source'], 'quote')
        self.assertEqual([c['load_id'] for c in b['outbound_candidates']], [outbound.id])
        prev = b['invoice_preview']
        self.assertEqual(prev['state'], 'on_delivery')
        self.assertEqual(prev['subtotal'], float(q.total_amount))
        self.assertEqual(prev['billing_basis'], 'per_load')
        self.assertFalse(b['economics']['pair'])
        # The preview is exactly what delivery raises.
        load = Load.objects.get(pk=r.json()['id'])
        load.status = 'DELIVERED'
        load.save()
        inv = Invoice.objects.get(load=load)
        self.assertEqual((float(inv.subtotal), float(inv.vat_amount), float(inv.total_amount)),
                         (prev['subtotal'], prev['vat_amount'], prev['total']))

    def test_idempotent(self):
        q = priced_quote(self.co, self.cust, 'BK-2', vt=self.vt)
        first = self.book(q)
        second = self.book(q)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()['id'], first.json()['id'])
        self.assertTrue(second.json()['booking']['already_converted'])
        self.assertEqual(Load.objects.filter(quote=q).count(), 1)

    def test_book_as_return_of_an_outbound(self):
        outbound = make_load(self.co, self.cust, 'BK-OUT2', pickup='Johannesburg', delivery='Durban')
        q = priced_quote(self.co, self.cust, 'BK-3', vt=self.vt, origin='Durban', destination='Johannesburg')
        r = self.book(q, {'return_of_load_id': outbound.id})
        self.assertEqual(r.status_code, 201, r.content)
        b = r.json()['booking']
        self.assertTrue(b['return_link']['linked'])
        self.assertEqual(b['is_return_of'], outbound.id)
        self.assertTrue(b['economics']['pair'])
        self.assertEqual(Load.objects.get(pk=r.json()['id']).return_link_source, 'convert')

    def test_link_later_on_repeat_call(self):
        outbound = make_load(self.co, self.cust, 'BK-OUT3', pickup='Johannesburg', delivery='Durban')
        q = priced_quote(self.co, self.cust, 'BK-4', vt=self.vt, origin='Durban', destination='Johannesburg')
        self.book(q)
        r = self.book(q, {'return_of_load_id': outbound.id})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['booking']['return_link']['linked'])

    def test_impossible_or_foreign_link_books_nothing(self):
        foreign = make_load(self.other, self.cust_o, 'BK-FOR')
        q = priced_quote(self.co, self.cust, 'BK-5', vt=self.vt)
        self.assertEqual(self.book(q, {'return_of_load_id': foreign.id}).status_code, 404)
        rt = make_load(self.co, self.cust, 'BK-RT', trip_type='ROUND_TRIP')
        r = self.book(q, {'return_of_load_id': rt.id})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['code'], 'round_trip')
        self.assertFalse(Load.objects.filter(quote=q).exists())

    def test_expect_return_flag(self):
        q = priced_quote(self.co, self.cust, 'BK-6', vt=self.vt)
        r = self.book(q, {'expect_return': True})
        self.assertTrue(r.json()['booking']['expecting_return'])
        self.assertTrue(Load.objects.get(pk=r.json()['id']).expecting_return)

    def test_declined_and_blocked_draft_cannot_be_booked(self):
        q = priced_quote(self.co, self.cust, 'BK-7', vt=self.vt, status='DECLINED')
        r = self.book(q)
        self.assertEqual((r.status_code, r.json()['code']), (409, 'quote_not_bookable'))
        draft = priced_quote(self.co, self.cust, 'BK-8', vt=self.vt, status='DRAFT', tolls='0')
        Quote.objects.filter(pk=draft.pk).update(costing_inputs={'tolls_unknown': True,
                                                                 'vehicle_type_id': self.vt.id})
        r = self.book(draft)
        self.assertEqual((r.status_code, r.json()['code']), (400, 'quote_send_blocked'))
        self.assertFalse(Load.objects.filter(quote=draft).exists())
        ok = priced_quote(self.co, self.cust, 'BK-9', vt=self.vt, status='DRAFT')
        self.assertEqual(self.book(ok).status_code, 201)


class BookingPreviewTests(BookingTests):
    def test_preview_matches_booking_and_creates_nothing(self):
        outbound = make_load(self.co, self.cust, 'BP-OUT', pickup='Johannesburg', delivery='Durban',
                             pickup_in_days=0, days=1)
        q = priced_quote(self.co, self.cust, 'BP-1', vt=self.vt, origin='Durban', destination='Johannesburg')
        day = (date.today() + timedelta(days=2)).isoformat()
        r = self.api.get(f'/api/v1/quotes/{q.id}/booking-preview/?pickup_date={day}')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body['preview'])
        self.assertTrue(body['can_book'])
        self.assertFalse(Load.objects.filter(quote=q).exists())
        self.assertEqual([c['load_id'] for c in body['booking']['outbound_candidates']], [outbound.id])
        booked = self.book(q, {'pickup_date': day}).json()['booking']
        for key in ('outbound_candidates', 'return_candidates'):
            self.assertEqual(body['booking'][key], booked[key], key)
        prev, real = body['booking']['invoice_preview'], booked['invoice_preview']
        self.assertEqual((prev['subtotal'], prev['vat_amount'], prev['total'], prev['state']),
                         (real['subtotal'], real['vat_amount'], real['total'], real['state']))
        self.assertEqual(prev['lines'][0]['description'], 'Transport (Durban → Johannesburg)')
        self.assertEqual(body['booking']['costing']['cost_floor'], booked['costing']['cost_floor'])
        again = self.api.get(f'/api/v1/quotes/{q.id}/booking-preview/').json()
        self.assertFalse(again['preview'])
        self.assertEqual(again['load_id'], booked and Load.objects.get(quote=q).id)

    def test_preview_says_when_booking_would_be_refused(self):
        q = priced_quote(self.co, self.cust, 'BP-2', vt=self.vt, status='DECLINED')
        body = self.api.get(f'/api/v1/quotes/{q.id}/booking-preview/').json()
        self.assertFalse(body['can_book'])
        self.assertEqual(body['blocked']['code'], 'quote_not_bookable')

    def test_preview_of_other_company_quote_is_404(self):
        other_user = make_user('book_o', self.other)
        q = priced_quote(self.co, self.cust, 'BP-3', vt=self.vt)
        c = APIClient()
        c.force_authenticate(other_user)
        self.assertEqual(c.get(f'/api/v1/quotes/{q.id}/booking-preview/').status_code, 404)
