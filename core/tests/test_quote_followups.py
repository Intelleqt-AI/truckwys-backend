"""Quote follow-ups (Oct 2026): fuel price clause + invoice adjustment, fuel
change alert, expiry / no-answer nudges + customer reminder, weekly margin
email, pricing setup flag. Time-frozen in SAST; no email leaves the process
(followup_emails.deliver is patched)."""
import importlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import (Company, Customer, FuelChangeAlert, FuelPrice, Invoice, Load, Notification, Quote,
                         QuoteAutomationSettings, QuoteFollowUp, QuoteFuelClause, VehicleType,
                         WeeklyMarginReport)

SAST = ZoneInfo('Africa/Johannesburg')
User = get_user_model()


def sast(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=SAST)


SEPT, OCT, NOV = Decimal('29.5551'), Decimal('32.7989'), Decimal('30.0000')


def official_rows(nov=False):
    FuelPrice.objects.create(date=date(2026, 9, 2), diesel_inland=SEPT, diesel_coastal=Decimal('28.6831'),
                             petrol_95=Decimal('27.1000'), petrol_95_coastal=Decimal('26.9000'),
                             source='FIASA', diesel_grade='50ppm', effective_from=sast(2026, 9, 2, 0, 1))
    FuelPrice.objects.create(date=date(2026, 10, 7), diesel_inland=OCT, diesel_coastal=Decimal('31.9269'),
                             petrol_95=Decimal('30.2500'), petrol_95_coastal=Decimal('30.0500'),
                             source='FIASA', diesel_grade='50ppm', effective_from=sast(2026, 10, 7, 0, 1))
    if nov:
        FuelPrice.objects.create(date=date(2026, 11, 4), diesel_inland=NOV, diesel_coastal=Decimal('29.2000'),
                                 petrol_95=Decimal('29.0000'), petrol_95_coastal=Decimal('28.8000'),
                                 source='FIASA', diesel_grade='50ppm', effective_from=sast(2026, 11, 4, 0, 1))


class _Base(TestCase):
    START = sast(2026, 10, 1, 10, 0)     # Thu, still the September price period

    def setUp(self):
        cache.clear()
        self.clock = [self.START]
        p = patch('django.utils.timezone.now', side_effect=lambda: self.clock[0])
        p.start()
        self.addCleanup(p.stop)
        self.sent_mail = []
        d = patch('core.services.followup_emails.deliver',
                  side_effect=lambda to, subject, html, text, reply_to=None:
                  self.sent_mail.append({'to': to, 'subject': subject, 'html': html, 'text': text,
                                         'reply_to': reply_to}) or True)
        d.start()
        self.addCleanup(d.stop)
        # No Celery from the request path in tests.
        q = patch('core.tasks.queue_fuel_change_alerts', return_value=True)
        q.start()
        self.addCleanup(q.stop)
        official_rows(nov=getattr(self, 'NOV', False))
        self.company = Company.objects.create(company_name='Follow Haulage', margin_target_pct=Decimal('10'),
                                              driver_allowance_per_night=Decimal('450'))
        self.user = User.objects.create_user(username='boss', password='x', email='boss@haul.test',
                                             first_name='Thabo', last_name='M', company=self.company, role='ADMIN')
        self.customer = Customer.objects.create(company=self.company, name='Acme', email='buyer@acme.test',
                                                phone='', address='', city='', state='', zip_code='')
        self.vt = VehicleType.objects.create(company=self.company, name='Superlink', capacity=34,
                                             max_distance=3000, base_rate=20, fuel_consumption_l_per_100km=42)
        from core.tests.quote_rules_fixtures import add_vehicle
        add_vehicle(self.company, self.vt)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def at(self, dt):
        self.clock[0] = dt
        cache.clear()

    def quote_payload(self, **over):
        p = {'customer': self.customer.id, 'pickup_location': 'Johannesburg', 'delivery_location': 'Durban',
             'origin': 'JHB', 'destination': 'DBN', 'cargo_description': 'Steel', 'weight': '28000',
             'distance': '568.4', 'vehicle_type': 'Superlink', 'estimated_duration_minutes': 440,
             'base_rate': '20000', 'fuel_surcharge': '6500', 'toll_charges': '1043.48', 'driver_allowance': '0',
             'total_amount': '36000', 'valid_until': str(date(2026, 11, 7))}
        p.update(over)
        return p

    def create(self, **over):
        r = self.api.post('/api/v1/quotes/', self.quote_payload(**over), format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return Quote.objects.get(id=r.json()['id'])

    def send(self, quote):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api.patch(f'/api/v1/quotes/{quote.id}/', {'status': 'SENT'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        quote.refresh_from_db()
        return quote

    def make_load(self, quote, pickup, **over):
        n = Load.objects.count() + 1
        fields = dict(company=self.company, load_number=f'L-{n}-{quote.id}', customer=self.customer, quote=quote,
                      pickup_location='Johannesburg', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
                      pickup_date=pickup, delivery_location='Durban', delivery_city='DBN', delivery_state='KZN',
                      delivery_zip='4001', delivery_date=pickup + timedelta(days=1), cargo_description='Steel',
                      weight=Decimal('28000'), distance=Decimal('568.4'), rate=quote.total_amount,
                      total_amount=quote.total_amount, status='IN_TRANSIT')
        fields.update(over)
        return Load.objects.create(**fields)


# ---------------------------------------------------------------------------
# Settings + migration
# ---------------------------------------------------------------------------

class AutomationSettingsTests(_Base):
    URL = '/api/v1/company/quote-automation/'

    def test_new_company_defaults_clause_on_no_prompt(self):
        body = self.api.get(self.URL).json()
        self.assertTrue(body['fuel_surcharge_enabled'])
        self.assertFalse(body['fuel_surcharge_prompt_pending'])
        self.assertEqual(body['fuel_surcharge_threshold_pct'], 5.0)
        self.assertEqual(body['follow_up_after_days'], 3)
        self.assertEqual(body['expiry_nudge_days'], 2)
        self.assertTrue(body['weekly_margin_email_enabled'])

    def test_backfill_existing_companies_clause_off_with_prompt(self):
        mig = importlib.import_module('core.migrations.0179_quote_followups_backfill')
        QuoteAutomationSettings.objects.all().delete()
        Company.objects.filter(pk=self.company.pk).update(fuel_price_mode='OWN')
        q = self.create()
        Quote.objects.filter(pk=q.pk).update(status='SENT')
        QuoteFollowUp.objects.all().delete()
        mig.forwards(django_apps, None)
        s = QuoteAutomationSettings.objects.get(company=self.company)
        self.assertFalse(s.fuel_surcharge_enabled)
        self.assertTrue(s.fuel_surcharge_prompt_pending)
        self.assertEqual(set(s.pricing_setup), {'driver_allowance', 'fuel_mode'})   # 10% target = default
        fu = QuoteFollowUp.objects.get(quote=q)
        self.assertEqual(fu.sent_at_source, 'estimated')
        mig.backwards(django_apps, None)
        self.assertFalse(QuoteAutomationSettings.objects.exists())

    def test_patch_validates_and_answers_prompt(self):
        QuoteAutomationSettings.objects.filter(company=self.company).update(
            fuel_surcharge_enabled=False, fuel_surcharge_prompt_pending=True)
        r = self.api.patch(self.URL, {'fuel_surcharge_threshold_pct': 30}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['message'], 'Enter a fuel price change between 1% and 25%.')
        r = self.api.patch(self.URL, {'fuel_surcharge_enabled': True, 'fuel_surcharge_threshold_pct': '7.5',
                                      'follow_up_after_days': 4}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body['fuel_surcharge_enabled'])
        self.assertFalse(body['fuel_surcharge_prompt_pending'])
        self.assertEqual(body['fuel_surcharge_threshold_pct'], 7.5)
        self.assertEqual(body['follow_up_after_days'], 4)
        self.assertIsNotNone(body['fuel_surcharge_decided_at'])

    def test_patch_admin_only(self):
        viewer = User.objects.create_user(username='v', password='x', company=self.company, role='VIEWER')
        api = APIClient()
        api.force_authenticate(viewer)
        self.assertEqual(api.patch(self.URL, {'fuel_alerts_enabled': False}, format='json').status_code, 403)
        self.assertEqual(api.get(self.URL).status_code, 200)


# ---------------------------------------------------------------------------
# 1. Fuel price clause
# ---------------------------------------------------------------------------

class FuelClauseTests(_Base):
    def test_draft_pdf_clause_official(self):
        from core.services.quote_pdf import diesel_reference_line, fuel_clause_line, generate_quote_pdf_bytes
        q = self.create()
        self.assertEqual(diesel_reference_line(q), 'Priced on diesel at R 29,56/L (official inland, 2 Sep 2026).')
        self.assertEqual(fuel_clause_line(q), 'If the official price moves more than 5% before the trip, '
                                              'the fuel part of this quote changes by the same amount.')
        self.assertTrue(generate_quote_pdf_bytes(q).startswith(b'%PDF'))

    def test_own_price_clause_names_official_basis(self):
        from core.services.quote_pdf import fuel_clause_line
        Company.objects.filter(pk=self.company.pk).update(fuel_price_mode='OWN', fuel_price_own=Decimal('28.00'))
        self.company.refresh_from_db()
        q = self.create()
        self.assertEqual(q.fuel_price_source, 'own')
        self.assertEqual(fuel_clause_line(q), 'If the official inland diesel price (R 29,56/L on 2 Sep 2026) moves '
                                              'more than 5% before the trip, the fuel part of this quote changes '
                                              'by the same amount.')

    def test_clause_off(self):
        from core.services.quote_pdf import fuel_clause_line
        QuoteAutomationSettings.objects.filter(company=self.company).update(fuel_surcharge_enabled=False)
        self.assertIsNone(fuel_clause_line(self.create()))

    def test_send_stamps_clause_and_public_view_shows_it(self):
        q = self.send(self.create())
        c = QuoteFuelClause.objects.get(quote=q)
        self.assertEqual(c.basis_price, SEPT)
        self.assertEqual(c.product, 'diesel')
        self.assertEqual(c.threshold_pct, Decimal('5.00'))
        body = APIClient().get(f'/api/v1/quotes/public/{q.id}/{q.token}/').json()
        self.assertTrue(body['fuel_clause'].startswith('If the official price moves more than 5%'))
        # Turning the setting off later does not change what the customer got.
        QuoteAutomationSettings.objects.filter(company=self.company).update(fuel_surcharge_enabled=False)
        q = Quote.objects.get(pk=q.pk)
        from core.services.quote_pdf import fuel_clause_line
        self.assertIsNotNone(fuel_clause_line(q))

    def test_adjustment_up_on_trip_date_and_invoice_line(self):
        q = self.send(self.create(pickup_date='2026-10-08'))
        litres = float(q.fuel_litres)
        self.at(sast(2026, 10, 9, 9, 0))
        body = self.api.get(f'/api/v1/quotes/{q.id}/fuel-adjustment/').json()
        self.assertTrue(body['applies'])
        self.assertEqual(body['trip_date'], '2026-10-08')
        self.assertEqual(body['trip_date_source'], 'quote_pickup')
        expected = float(Decimal(str(litres * (float(OCT) - float(SEPT)))).quantize(Decimal('0.01')))
        self.assertAlmostEqual(body['amount_zar'], expected, places=2)
        self.assertEqual(body['description'], 'Fuel price adjustment (diesel R 29,56 → R 32,80/L)')
        self.assertFalse(body['provisional'])
        # Load delivered -> auto invoice carries the adjustment line.
        load = self.make_load(q, sast(2026, 10, 8, 7, 0))
        load.status = 'DELIVERED'
        load.save()
        inv = Invoice.objects.get(load=load)
        lines = list(inv.lines.order_by('position'))
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1].revenue_type, 'FUEL_SURCHARGE')
        self.assertEqual(lines[1].description, 'Fuel price adjustment (diesel R 29,56 → R 32,80/L)')
        self.assertEqual(float(lines[1].net_amount), expected)
        self.assertEqual(float(inv.subtotal), float(q.total_amount) + expected)
        lb = self.api.get(f'/api/v1/loads/{load.id}/fuel-adjustment/').json()
        self.assertEqual(lb['invoiced']['amount_zar'], expected)

    def test_within_threshold_no_adjustment(self):
        QuoteAutomationSettings.objects.filter(company=self.company).update(
            fuel_surcharge_threshold_pct=Decimal('15'))
        q = self.send(self.create(pickup_date='2026-10-08'))
        self.at(sast(2026, 10, 9, 9, 0))
        body = self.api.get(f'/api/v1/quotes/{q.id}/fuel-adjustment/').json()
        self.assertFalse(body['applies'])
        self.assertEqual(body['reason'], 'within_threshold')
        self.assertAlmostEqual(body['change_pct'], 10.98, places=2)

    def test_trip_before_change_wednesday_uses_old_price(self):
        """Pickup Tue 6 Oct: the September price was in force (change is 00:01 Wed 7 Oct SAST)."""
        q = self.send(self.create(pickup_date='2026-10-06'))
        self.at(sast(2026, 10, 9, 9, 0))
        body = self.api.get(f'/api/v1/quotes/{q.id}/fuel-adjustment/').json()
        self.assertEqual(body['price_on_trip'], float(SEPT))
        self.assertFalse(body['applies'])

    def test_future_trip_is_provisional_and_not_invoiced(self):
        q = self.send(self.create(pickup_date='2026-10-20'))
        self.at(sast(2026, 10, 9, 9, 0))
        body = self.api.get(f'/api/v1/quotes/{q.id}/fuel-adjustment/').json()
        self.assertTrue(body['provisional'])
        from core.services.fuel_surcharge import invoice_adjustment_for_load
        load = self.make_load(q, sast(2026, 10, 20, 7, 0))
        self.assertIsNone(invoice_adjustment_for_load(load))

    def test_sent_without_clause_never_adjusted(self):
        QuoteAutomationSettings.objects.filter(company=self.company).update(fuel_surcharge_enabled=False)
        q = self.send(self.create(pickup_date='2026-10-08'))
        QuoteAutomationSettings.objects.filter(company=self.company).update(fuel_surcharge_enabled=True)
        self.at(sast(2026, 10, 9, 9, 0))
        body = self.api.get(f'/api/v1/quotes/{q.id}/fuel-adjustment/').json()
        self.assertEqual(body['reason'], 'no_clause')
        load = self.make_load(q, sast(2026, 10, 8, 7, 0))
        load.status = 'DELIVERED'
        load.save()
        self.assertEqual(Invoice.objects.get(load=load).lines.count(), 1)


class FuelClauseDownTests(_Base):
    NOV = True
    START = sast(2026, 10, 8, 10, 0)

    def test_price_down_discounts_freight_line(self):
        q = self.send(self.create(pickup_date='2026-11-10', valid_until='2026-11-30'))
        self.at(sast(2026, 11, 11, 9, 0))
        litres = float(q.fuel_litres)
        expected = float(Decimal(str(litres * (float(NOV) - float(OCT)))).quantize(Decimal('0.01')))
        self.assertLess(expected, 0)
        load = self.make_load(q, sast(2026, 11, 10, 7, 0))
        load.status = 'DELIVERED'
        load.save()
        inv = Invoice.objects.get(load=load)
        lines = list(inv.lines.all())
        self.assertEqual(len(lines), 1)
        self.assertEqual(float(lines[0].discount_amount), -expected)
        self.assertIn('less fuel price adjustment (diesel R 32,80 → R 30,00/L)', lines[0].description)
        self.assertEqual(float(inv.subtotal), round(float(q.total_amount) + expected, 2))


class PetrolClauseTests(_Base):
    def setUp(self):
        super().setUp()
        self.vt.fuel_type = 'Petrol'
        self.vt.save()

    def test_petrol_quote_adjusts_on_petrol(self):
        q = self.send(self.create(pickup_date='2026-10-08'))
        self.assertEqual(QuoteFuelClause.objects.get(quote=q).product, 'petrol_95')
        self.at(sast(2026, 10, 9, 9, 0))
        body = self.api.get(f'/api/v1/quotes/{q.id}/fuel-adjustment/').json()
        self.assertTrue(body['applies'])
        self.assertEqual(body['description'], 'Fuel price adjustment (petrol 95 R 27,10 → R 30,25/L)')


# ---------------------------------------------------------------------------
# 2. Fuel change alert
# ---------------------------------------------------------------------------

class TonnageFuelClauseTests(_Base):
    """Per-tonne quotes (tonnage branch) with the clause: each call-off load
    adjusts only its share of the quoted litres (litres per billed tonne x the
    tonnes it is billed on), on the auto-invoice, the booking preview and a
    weighbridge re-price of the draft invoice."""

    def tonnage(self, **over):
        return self.create(**{'pricing_basis': 'per_tonne', 'vehicle_type': '', 'tonnes_per_load': '30',
                              'rate_per_tonne': '1300', 'weight': '30000', 'pickup_date': '2026-10-08', **over})

    def share(self, q, tonnes):
        per_t = float(q.fuel_litres) / float(q.costing_snapshot['tonnage']['billable_tonnes'])
        litres = round(per_t * tonnes, 3)
        return float(Decimal(str(litres * (float(OCT) - float(SEPT)))).quantize(Decimal('0.01')))

    def book(self, q, **body):
        r = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {'pickup_date': '2026-10-08', **body},
                          format='json')
        self.assertIn(r.status_code, (200, 201), r.content)
        return Load.objects.get(id=r.json()['id'])

    def test_volume_contract_call_off_invoice_carries_its_share(self):
        q = self.send(self.tonnage(total_tonnes='70'))
        self.assertTrue(QuoteFuelClause.objects.filter(quote=q).exists())
        self.at(sast(2026, 10, 9, 9, 0))
        # The contract's own row: one planned load (30 t) of fuel, not all 70 t.
        qb = self.api.get(f'/api/v1/quotes/{q.id}/fuel-adjustment/').json()
        self.assertIsNone(qb['load_id'])
        self.assertAlmostEqual(qb['amount_zar'], self.share(q, 30), places=2)
        pv = self.api.get(f'/api/v1/quotes/{q.id}/booking-preview/?tonnes=30').json()
        self.assertTrue(pv['can_book'])
        load = self.book(q, tonnes=30)
        # Load endpoint: litres scaled to the 30 t this load is billed on.
        body = self.api.get(f'/api/v1/loads/{load.id}/fuel-adjustment/').json()
        self.assertTrue(body['applies'])
        self.assertAlmostEqual(body['amount_zar'], self.share(q, 30), places=2)
        self.assertLess(body['litres'], float(q.fuel_litres))
        load.status = 'DELIVERED'
        load.save()
        inv = Invoice.objects.get(load=load)
        lines = list(inv.lines.order_by('position'))
        self.assertEqual(len(lines), 2)
        self.assertEqual((lines[0].quantity, lines[0].unit_price), (Decimal('30.000'), Decimal('1300.00')))
        self.assertEqual(lines[1].revenue_type, 'FUEL_SURCHARGE')
        self.assertEqual(float(lines[1].net_amount), self.share(q, 30))
        self.assertEqual(inv.status, 'DRAFT')                       # awaiting the weighbridge
        # Weighbridge 31,24 t: the draft is re-priced and keeps the adjustment,
        # now on 31,24 t of fuel.
        r = self.api.patch(f'/api/v1/loads/{load.id}/', {'actual_tonnes': '31.24'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        lines = list(inv.lines.order_by('position'))
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].quantity, Decimal('31.240'))
        self.assertEqual(float(lines[1].net_amount), self.share(q, 31.24))
        self.assertEqual(float(inv.subtotal), round(40612.0 + self.share(q, 31.24), 2))

    def test_booking_preview_matches_the_invoice(self):
        q = self.send(self.tonnage())
        self.at(sast(2026, 10, 9, 9, 0))
        load = self.book(q)
        from core.services.invoicing import invoice_preview
        pv = invoice_preview(load)
        self.assertEqual(len(pv['lines']), 2)
        load.status = 'DELIVERED'
        load.save()
        inv = Invoice.objects.get(load=load)
        self.assertEqual(float(inv.subtotal), pv['subtotal'])

    def test_unsent_per_tonne_quote_booked_directly_has_no_adjustment(self):
        """One-tap booking from a draft: the customer never saw a clause."""
        q = self.tonnage()
        self.at(sast(2026, 10, 9, 9, 0))
        load = self.book(q)
        load.status = 'DELIVERED'
        load.save()
        self.assertEqual(Invoice.objects.get(load=load).lines.count(), 1)


    def test_costed_call_off_still_gets_exactly_its_share(self):
        """Call-offs are costed at booking (tonnage verifier fixes): the
        adjustment still follows the tonnes billed, not the costed full load."""
        q = self.send(self.tonnage(total_tonnes='70'))
        self.at(sast(2026, 10, 9, 9, 0))
        load = self.book(q, tonnes=12)
        self.assertEqual(load.costing_source, 'quote')
        # 12 t booked, billed at the 30 t planned-load minimum.
        body = self.api.get(f'/api/v1/loads/{load.id}/fuel-adjustment/').json()
        from core.services.tonnage_jobs import load_billing
        billed = load_billing(load)['billable_tonnes']
        self.assertEqual(billed, 30.0)
        self.assertAlmostEqual(body['amount_zar'], self.share(q, billed), places=2)
        load.status = 'DELIVERED'
        load.save()
        line = Invoice.objects.get(load=load).lines.get(revenue_type='FUEL_SURCHARGE')
        self.assertEqual(float(line.net_amount), self.share(q, billed))

    def test_weighed_after_invoicing_never_adds_to_the_issued_invoice(self):
        q = self.send(self.tonnage())
        self.at(sast(2026, 10, 9, 9, 0))
        load = self.book(q)
        Load.objects.filter(id=load.id).update(actual_tonnes=Decimal('30'))
        load.refresh_from_db()
        load.status = 'DELIVERED'
        load.save()
        inv = Invoice.objects.get(load=load)
        self.assertEqual(inv.lines.count(), 2)
        Invoice.objects.filter(id=inv.id).update(status='SENT')
        before = Invoice.objects.get(id=inv.id).subtotal
        # Same tonnes again: the adjustment is on the invoice, so no mismatch.
        r = self.api.patch(f'/api/v1/loads/{load.id}/', {'actual_tonnes': '30'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        load.refresh_from_db()
        self.assertFalse(load.invoice_mismatch)
        # More tonnes: flagged, the difference includes the extra fuel share,
        # and the issued invoice is untouched (still 2 lines, same subtotal).
        r = self.api.patch(f'/api/v1/loads/{load.id}/', {'actual_tonnes': '32.5'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        load.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(load.invoice_mismatch['code'], 'weighed_after_invoicing')
        expected = 1300 * 2.5 + self.share(q, 32.5) - self.share(q, 30)
        self.assertAlmostEqual(load.invoice_mismatch['difference'], expected, places=1)
        self.assertEqual(inv.subtotal, before)
        self.assertEqual(inv.lines.count(), 2)


class TonnageFuelClauseDownTests(_Base):
    NOV = True
    START = sast(2026, 10, 8, 10, 0)

    def test_down_discount_is_capped_at_the_whole_per_tonne_line(self):
        q = self.send(self.create(pricing_basis='per_tonne', vehicle_type='', tonnes_per_load='30',
                                  rate_per_tonne='1300', weight='30000', pickup_date='2026-11-10',
                                  valid_until='2026-11-30'))
        self.at(sast(2026, 11, 11, 9, 0))
        r = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {'pickup_date': '2026-11-10'}, format='json')
        load = Load.objects.get(id=r.json()['id'])
        load.status = 'DELIVERED'
        load.save()
        line = Invoice.objects.get(load=load).lines.get()
        self.assertEqual(line.quantity, Decimal('30.000'))
        self.assertGreater(line.discount_amount, 0)
        self.assertIn('less fuel price adjustment (diesel R 32,80 → R 30,00/L)', line.description)
        self.assertEqual(line.net_amount, Decimal('39000.00') - line.discount_amount)


class FuelChangeAlertTests(_Base):
    def setUp(self):
        super().setUp()
        self.thin = self.create(total_amount='45000')     # ~28% margin: stays above target
        floor = float(self.thin.cost_floor)
        # A second quote priced at ~11% margin on the September floor: under 10% after the rise.
        self.tight = self.create(total_amount=str(round(floor / 0.89, 2)))
        self.expired = self.create(valid_until='2026-10-05')
        self.declined = self.create()
        Quote.objects.filter(pk=self.declined.pk).update(status='DECLINED')

    def run_alerts(self):
        from core.services.fuel_change_alerts import run_fuel_change_alerts
        return run_fuel_change_alerts()

    def test_alert_lists_affected_quotes_once_per_period(self):
        self.at(sast(2026, 10, 7, 6, 20))
        summary = self.run_alerts()
        self.assertEqual(summary['notified'], 1)
        alert = FuelChangeAlert.objects.get(company=self.company, product='diesel')
        ids = [r["quote_id"] for r in alert.quotes]
        self.assertEqual(set(ids), {self.thin.id, self.tight.id})
        self.assertEqual(ids[0], self.tight.id)          # under target first
        self.assertEqual(alert.quotes_under_target, 1)
        n = Notification.objects.get(user=self.user, title='Diesel price up')
        self.assertEqual(n.message, 'Diesel up R 3,24/L today. 1 open quote is now under your 10% target. '
                                    'Re-price it?')
        self.assertEqual(n.link, f'/bookings/quotes?fuel_alert={alert.id}')
        row = next(r for r in alert.quotes if r['quote_id'] == self.tight.id)
        self.assertGreater(row['floor_now'], row['floor_then'])
        self.assertLess(row['margin_now'], 10)
        self.assertEqual(len(self.sent_mail), 1)
        self.assertIn(self.tight.quote_number, self.sent_mail[0]['text'])
        # Idempotent: a second run (or the morning run) adds nothing.
        self.at(sast(2026, 10, 8, 0, 10))
        self.assertEqual(self.run_alerts()['notified'], 0)
        self.assertEqual(Notification.objects.filter(user=self.user, title='Diesel price up').count(), 1)
        self.assertEqual(len(self.sent_mail), 1)
        detail = self.api.get(f'/api/v1/fuel-alerts/{alert.id}/').json()
        self.assertEqual(detail['quotes_affected'], 2)
        self.assertTrue(all(r['still_open'] for r in detail['quotes']))

    def test_before_change_minute_no_alert(self):
        self.at(sast(2026, 10, 7, 0, 0, 30))
        self.assertEqual(self.run_alerts()['changes'], 0)
        self.at(sast(2026, 10, 7, 0, 1, 30))
        self.assertEqual(self.run_alerts()['notified'], 1)

    def test_late_run_names_the_day(self):
        self.at(sast(2026, 10, 8, 6, 20))
        self.run_alerts()
        alert = FuelChangeAlert.objects.get(company=self.company, product='diesel')
        self.assertTrue(alert.message.startswith('Diesel up R 3,24/L on Wed 7 Oct.'))

    def test_company_switch_off(self):
        QuoteAutomationSettings.objects.filter(company=self.company).update(fuel_alerts_enabled=False)
        self.at(sast(2026, 10, 7, 6, 20))
        self.run_alerts()
        self.assertFalse(FuelChangeAlert.objects.exists())
        self.assertFalse(Notification.objects.filter(title='Diesel price up').exists())

    def test_email_pref_off_still_bell(self):
        self.user.notification_settings = {'email': {'fuel_alerts': False}}
        self.user.save()
        self.at(sast(2026, 10, 7, 6, 20))
        self.run_alerts()
        self.assertTrue(Notification.objects.filter(user=self.user, title='Diesel price up').exists())
        self.assertEqual(self.sent_mail, [])

    def test_quote_priced_after_change_not_listed(self):
        self.at(sast(2026, 10, 7, 5, 0))
        fresh = self.create()
        self.at(sast(2026, 10, 7, 6, 20))
        self.run_alerts()
        ids = [r['quote_id'] for r in FuelChangeAlert.objects.get(company=self.company, product='diesel').quotes]
        self.assertNotIn(fresh.id, ids)


# ---------------------------------------------------------------------------
# 3. Nudges + reminder
# ---------------------------------------------------------------------------

class NudgeTests(_Base):
    def sweep(self):
        from core.services.quote_followups import sweep_quote_nudges
        return sweep_quote_nudges()

    def nudges(self):
        return list(Notification.objects.filter(user=self.user, title__in=(
            'No answer yet', 'Quote expiring soon')).values_list('message', flat=True))

    def test_no_answer_after_three_sast_days_once(self):
        q = self.send(self.create(valid_until='2026-10-30'))
        self.at(sast(2026, 10, 3, 23, 59))
        self.sweep()
        self.assertEqual(self.nudges(), [])
        self.at(sast(2026, 10, 4, 8, 0))
        self.sweep()
        self.assertEqual(self.nudges(), [f'No answer from Acme on {q.quote_number} (sent 3 days ago). Follow up?'])
        self.sweep()
        self.assertEqual(len(self.nudges()), 1)

    def test_sast_calendar_days_not_utc(self):
        """Sent 23:30 SAST on 1 Oct (21:30 UTC): 3 SAST days by 00:30 SAST on 4 Oct (still 3 Oct in UTC)."""
        self.at(sast(2026, 10, 1, 23, 30))
        q = self.send(self.create(valid_until='2026-10-30'))
        self.at(sast(2026, 10, 4, 0, 30))
        self.sweep()
        self.assertEqual(len(self.nudges()), 1)
        self.assertIn(q.quote_number, self.nudges()[0])

    def test_expiry_nudge(self):
        q = self.send(self.create(valid_until='2026-10-09'))      # Friday
        self.at(sast(2026, 10, 2, 8, 0))
        self.sweep()
        self.assertEqual(self.nudges(), [])
        self.at(sast(2026, 10, 7, 8, 0))
        QuoteFollowUp.objects.filter(quote=q).update(no_answer_nudged_at=sast(2026, 10, 4, 8, 0))
        self.sweep()
        self.assertEqual(self.nudges(), [f'Quote {q.quote_number} for Acme expires on Fri.'])
        self.at(sast(2026, 10, 8, 8, 0))
        self.sweep()
        self.assertEqual(len(self.nudges()), 1)

    def test_combined_when_both_due(self):
        q = self.send(self.create(valid_until='2026-10-05'))
        self.at(sast(2026, 10, 4, 8, 0))
        self.sweep()
        self.assertEqual(self.nudges(), [f'No answer from Acme on {q.quote_number} (sent 3 days ago). '
                                         f'It expires tomorrow. Follow up?'])

    def test_status_change_stops_and_resend_restarts(self):
        q = self.send(self.create(valid_until='2026-10-30'))
        Quote.objects.filter(pk=q.pk).update(status='ACCEPTED')
        self.at(sast(2026, 10, 5, 8, 0))
        self.sweep()
        self.assertEqual(self.nudges(), [])
        Quote.objects.filter(pk=q.pk).update(status='DRAFT')
        q.refresh_from_db()
        self.send(q)
        fu = QuoteFollowUp.objects.get(quote=q)
        self.assertEqual(fu.sent_at, sast(2026, 10, 5, 8, 0))
        self.assertIsNone(fu.no_answer_nudged_at)

    def test_disabled(self):
        QuoteAutomationSettings.objects.filter(company=self.company).update(follow_ups_enabled=False)
        self.send(self.create(valid_until='2026-10-30'))
        self.at(sast(2026, 10, 5, 8, 0))
        self.sweep()
        self.assertEqual(self.nudges(), [])

    def test_reminder_preview_then_send(self):
        q = self.send(self.create(valid_until='2026-10-30'))
        self.at(sast(2026, 10, 4, 9, 0))
        url = f'/api/v1/quotes/{q.id}/follow-up/reminder/'
        body = self.api.get(url).json()
        self.assertTrue(body['can_send'])
        self.assertEqual(body['preview']['to'], 'buyer@acme.test')
        self.assertEqual(body['preview']['subject'], f'Reminder: quote {q.quote_number} from Follow Haulage')
        self.assertIn('valid until Fri 30 Oct 2026', body['preview']['text'])
        self.assertEqual(self.sent_mail, [])                       # preview sends nothing
        self.assertEqual(self.api.post(url, {}, format='json').status_code, 400)
        self.assertEqual(self.sent_mail, [])
        r = self.api.post(url, {'confirm': True, 'note': 'Happy to talk.'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(self.sent_mail), 1)
        self.assertEqual(self.sent_mail[0]['to'], 'buyer@acme.test')
        self.assertEqual(self.sent_mail[0]['reply_to'], 'boss@haul.test')
        self.assertIn('Happy to talk.', self.sent_mail[0]['text'])
        self.assertEqual(r.json()['reminder']['count'], 1)
        r = self.api.post(url, {'confirm': True}, format='json')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['code'], 'too_soon')
        state = self.api.get(f'/api/v1/quotes/{q.id}/follow-up/').json()
        self.assertEqual(state['days_since_sent'], 3)
        self.assertFalse(state['reminder']['can_send'])

    def test_reminder_rules(self):
        q = self.send(self.create(valid_until='2026-10-30'))
        viewer = User.objects.create_user(username='v', password='x', company=self.company, role='VIEWER')
        api = APIClient()
        api.force_authenticate(viewer)
        url = f'/api/v1/quotes/{q.id}/follow-up/reminder/'
        self.assertEqual(api.post(url, {'confirm': True}, format='json').status_code, 403)
        Customer.objects.filter(pk=self.customer.pk).update(email='')
        r = self.api.post(url, {'confirm': True}, format='json')
        self.assertEqual(r.json()['code'], 'no_customer_email')
        other = Company.objects.create(company_name='Other')
        u2 = User.objects.create_user(username='o', password='x', company=other, role='ADMIN')
        api.force_authenticate(u2)
        self.assertEqual(api.get(url).status_code, 404)
        self.assertEqual(self.sent_mail, [])


# ---------------------------------------------------------------------------
# 4. Weekly margin email
# ---------------------------------------------------------------------------

class WeeklyMarginTests(_Base):
    START = sast(2026, 10, 2, 10, 0)

    def econ(self, values):
        """Patch load_economics (the single actuals source) with {load_id: (revenue, cost|None)}."""
        def fake(company, loads):
            out = {}
            for l in loads:
                rev, cost = values.get(l.pk, (float(l.total_amount), None))
                out[l.pk] = {'load_id': l.pk, 'revenue': Decimal(str(rev)), 'revenue_basis': 'actual',
                             'cost': Decimal(str(cost)) if cost is not None else Decimal('1'),
                             'cost_basis': 'actual' if cost is not None else 'estimate'}
            return out
        p = patch('core.services.reports.load_economics', side_effect=fake)
        p.start()
        self.addCleanup(p.stop)

    def delivered(self, quote, when, **over):
        load = self.make_load(quote, when - timedelta(days=1), status='INVOICED', actual_delivered_at=when, **over)
        return load

    def test_figures_by_lane_customer_and_worst_lanes(self):
        q1 = self.create()
        q2 = self.create(total_amount='20000')
        Quote.objects.filter(pk=q1.pk).update(cost_floor=Decimal('30000'), accepted_at=sast(2026, 10, 6, 9))
        Quote.objects.filter(pk=q2.pk).update(cost_floor=Decimal('18000'), rejected_at=sast(2026, 10, 7, 9))
        q1.refresh_from_db(); q2.refresh_from_db()
        a = self.delivered(q1, sast(2026, 10, 11, 23, 30))          # Sunday night: last week
        b = self.delivered(q2, sast(2026, 10, 8, 12), pickup_city='CPT', delivery_city='PE')
        late = self.delivered(q1, sast(2026, 10, 12, 0, 10))       # Monday: this week, not last
        self.econ({a.pk: (36000, 31000), b.pk: (20000, 22000), late.pk: (36000, 1000)})
        from core.services.margin_review import weekly_margin_figures
        fig = weekly_margin_figures(self.company, sast(2026, 10, 12, 7, 0))
        self.assertEqual(fig['week'], {'start': '2026-10-05', 'end': '2026-10-11'})
        wk = fig['last_week']
        self.assertEqual(wk['totals']['loads'], 2)
        self.assertEqual(wk['totals']['actual_margin_zar'], 3000.0)        # 5000 − 2000
        lanes = {l['name']: l for l in wk['by_lane']}
        self.assertEqual(lanes['JHB → DBN']['quoted_margin_pct'], 16.7)
        self.assertEqual(lanes['JHB → DBN']['actual_margin_pct'], 13.9)
        self.assertEqual(lanes['CPT → PE']['actual_margin_pct'], -10.0)
        self.assertEqual([l['name'] for l in fig['worst_lanes']], ['CPT → PE'])
        self.assertEqual(fig['quotes']['last_week']['won'], 1)
        self.assertEqual(fig['quotes']['last_week']['lost'], 1)
        self.assertEqual(fig['quotes']['last_week']['avg_quoted_margin_won_pct'], 16.7)
        self.assertEqual(wk['by_customer'][0]['name'], 'Acme')

    def test_estimated_costs_are_not_actuals(self):
        q = self.create()
        self.delivered(q, sast(2026, 10, 8, 12))
        self.econ({})
        from core.services.margin_review import weekly_margin_figures
        fig = weekly_margin_figures(self.company, sast(2026, 10, 12, 7, 0))
        self.assertEqual(fig['last_week']['totals']['actual_loads'], 0)
        self.assertIsNone(fig['last_week']['totals']['actual_margin_pct'])
        self.assertFalse(fig['last_week']['enough_actuals'])

    def test_email_admins_once_per_week(self):
        q = self.create()
        Quote.objects.filter(pk=q.pk).update(cost_floor=Decimal('30000'))
        a = self.delivered(Quote.objects.get(pk=q.pk), sast(2026, 10, 8, 12))
        self.econ({a.pk: (36000, 31000)})
        User.objects.create_user(username='disp', password='x', email='d@haul.test', company=self.company,
                                 role='DISPATCHER')
        optout = User.objects.create_user(username='a2', password='x', email='a2@haul.test', company=self.company,
                                          role='ADMIN')
        optout.notification_settings = {'email': {'margin_report': False}}
        optout.save()
        from core.services.margin_review import send_weekly_margin_emails
        self.at(sast(2026, 10, 12, 7, 0))
        self.assertEqual(send_weekly_margin_emails()['emails_sent'], 1)
        self.assertEqual([m['to'] for m in self.sent_mail], ['boss@haul.test'])
        mail = self.sent_mail[0]
        self.assertEqual(mail['subject'], 'Your margins last week (5–11 Oct 2026)')
        self.assertIn('JHB → DBN', mail['text'])
        self.assertIn('Last week: 1 load delivered, quoted margin 16,7%, actual 13,9% on 1 with costs.', mail['text'])
        self.assertEqual(send_weekly_margin_emails()['emails_sent'], 0)
        self.assertEqual(WeeklyMarginReport.objects.count(), 1)

    def test_no_activity_no_email_and_honest_api(self):
        from core.services.margin_review import send_weekly_margin_emails
        self.at(sast(2026, 10, 12, 7, 0))
        self.assertEqual(send_weekly_margin_emails()['skipped_no_activity'], 1)
        self.assertEqual(self.sent_mail, [])
        body = self.api.get('/api/v1/reports/weekly-margin/').json()
        self.assertFalse(body['last_week']['enough_data'])
        self.assertEqual(body['worst_lanes'], [])

    def test_company_opt_out(self):
        QuoteAutomationSettings.objects.filter(company=self.company).update(weekly_margin_email_enabled=False)
        from core.services.margin_review import send_weekly_margin_emails
        self.at(sast(2026, 10, 12, 7, 0))
        self.assertEqual(send_weekly_margin_emails()['emails_sent'], 0)
        self.assertFalse(WeeklyMarginReport.objects.exists())


# ---------------------------------------------------------------------------
# 5. Pricing setup
# ---------------------------------------------------------------------------

class PricingSetupTests(_Base):
    URL = '/api/v1/company/pricing-setup/'

    def test_flags_change_and_confirm(self):
        body = self.api.get(self.URL).json()
        self.assertTrue(body['needs_setup'])
        self.assertEqual(set(body['unset']), {'target_margin', 'operating_cost', 'driver_allowance', 'fuel_mode'})
        items = {i['key']: i for i in body['items']}
        self.assertEqual(items['target_margin']['display'], '10%')
        self.assertEqual(items['driver_allowance']['rate_source'], 'company_setting')
        self.company.margin_target_pct = Decimal('15')
        self.company.save()
        body = self.api.get(self.URL).json()
        self.assertNotIn('target_margin', body['unset'])
        self.assertEqual({i['key']: i for i in body['items']}['target_margin']['how'], 'changed')
        r = self.api.post(self.URL, {'action': 'confirm', 'keys': ['operating_cost', 'fuel_mode']}, format='json')
        self.assertEqual(set(r.json()['unset']), {'driver_allowance'})
        r = self.api.post(self.URL, {'action': 'confirm', 'keys': ['nope']}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.api.post(self.URL, {'action': 'confirm'}, format='json')
        self.assertFalse(r.json()['needs_setup'])

    def test_unchanged_save_is_not_a_decision_and_dismiss(self):
        self.company.company_name = 'Renamed'
        self.company.save()
        self.assertEqual(len(self.api.get(self.URL).json()['unset']), 4)
        r = self.api.post(self.URL, {'action': 'dismiss'}, format='json')
        self.assertFalse(r.json()['needs_setup'])
        self.assertIsNotNone(r.json()['dismissed_at'])

    def test_no_allowance_on_record_text(self):
        Company.objects.filter(pk=self.company.pk).update(driver_allowance_per_night=None)
        self.company.refresh_from_db()
        with patch('core.services.quote_ai_pricing.stored_allowance', return_value=None):
            items = {i['key']: i for i in self.api.get(self.URL).json()['items']}
        self.assertEqual(items['driver_allowance']['default_text'],
                         'No approved allowance is on record yet. Enter what you pay your drivers per night away.')
        self.assertIsNone(items['driver_allowance']['rate_in_use'])

    def test_approved_nbcrfli_allowance_text(self):
        from core.models import VerifiedRate
        Company.objects.filter(pk=self.company.pk).update(driver_allowance_per_night=None)
        self.company.refresh_from_db()
        VerifiedRate.objects.create(kind=VerifiedRate.KIND_DRIVER_ALLOWANCE, key='nbcrfli', value=Decimal('243.63'),
                                    unit='per_night',
                                    effective_from=date(2026, 3, 1), status=VerifiedRate.STATUS_APPROVED)
        items = {i['key']: i for i in self.api.get(self.URL).json()['items']}
        self.assertEqual(items['driver_allowance']['default_text'],
                         'We use the approved NBCRFLI allowance of R 243,63 a night from 1 Mar 2026.')
        self.assertEqual(items['driver_allowance']['rate_source'], 'approved_allowance')
