"""TMS updates: upsert by external_id (company-scoped), audit trail, re-cost,
never touch invoices (flag instead), link returns by external id."""
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import ActivityEvent, Invoice, Load
from core.tests.quote_rules_fixtures import add_vehicle, official_price_now
from core.tests.trip_fixtures import (make_company, make_customer, make_key, make_load, make_user,
                                      make_vehicle_type, priced_quote)

TRIPS = '/api/v1/integrations/trips/sync/'
SYNC = '/api/v1/integrations/fleet/sync/'


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        official_price_now()
        cls.co = make_company('TMS Co')
        cls.other = make_company('TMS Other')
        cls.user = make_user('tms_u', cls.co)
        cls.user_o = make_user('tms_o', cls.other)
        cls.cust = make_customer(cls.co, 'tms')
        make_key(cls.user, 'TMS-KEY')
        make_key(cls.user_o, 'TMS-KEY-O')
        cls.vt = make_vehicle_type(cls.co)
        cls.truck = add_vehicle(cls.co, cls.vt, plate='TM 01 GP')

    def trips(self, records, key='TMS-KEY'):
        r = APIClient().post(TRIPS, records, format='json', HTTP_X_API_KEY=key)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def rec(self, **kw):
        base = {'external_id': 'T-100', 'origin': 'Johannesburg', 'destination': 'Durban', 'distance': 600,
                'rate': 30000, 'weight': 10000, 'vehicle_plate': 'TM 01 GP', 'toll_cost': 800,
                'duration_minutes': 420, 'pickup_date': '2026-11-02', 'delivery_date': '2026-11-03'}
        base.update(kw)
        return base


class UpsertTests(_Base):
    def test_second_sync_updates_instead_of_skipping(self):
        first = self.trips([self.rec()])
        self.assertEqual(first['created'], 1)
        load = Load.objects.get(pk=first['load_ids'][0])
        self.assertEqual((load.external_id, load.external_source), ('T-100', 'trips_sync'))
        floor_before = load.cost_floor
        second = self.trips([self.rec(rate=32000, distance=650, delivery_date='2026-11-04', status='IN_TRANSIT')])
        self.assertEqual((second['created'], second['updated'], second['skipped']), (0, 1, 0))
        load.refresh_from_db()
        self.assertEqual(load.total_amount, Decimal('32000.00'))
        self.assertEqual(load.distance, Decimal('650.00'))
        self.assertEqual(load.status, 'IN_TRANSIT')
        from django.utils import timezone
        self.assertEqual(timezone.localtime(load.delivery_date).date(), date(2026, 11, 4))
        self.assertNotEqual(load.cost_floor, floor_before)        # re-costed on the new distance
        ev = ActivityEvent.objects.filter(entity_id=load.pk, title__startswith='TRIPS_SYNC update').get()
        self.assertEqual(ev.metadata['changes']['total_amount'], [30000.0, 32000.0])
        self.assertEqual(ev.metadata['changes']['distance'], [600.0, 650.0])
        self.assertEqual(ev.company_id, self.co.id)
        self.assertEqual(sorted(second['results'][0]['changed'])[:2], ['delivery_date', 'distance'])
        third = self.trips([self.rec(rate=32000, distance=650, delivery_date='2026-11-04', status='IN_TRANSIT')])
        self.assertEqual((third['unchanged'], third['skipped'], third['updated']), (1, 1, 0))

    def test_legacy_note_id_is_adopted(self):
        legacy = make_load(self.co, self.cust, 'LEG-T', notes='ext_id:T-OLD')
        out = self.trips([self.rec(external_id='T-OLD', rate=12345)])
        self.assertEqual((out['created'], out['updated']), (0, 1))
        legacy.refresh_from_db()
        self.assertEqual((legacy.external_id, legacy.total_amount), ('T-OLD', Decimal('12345.00')))

    def test_external_id_unique_per_company_only(self):
        self.trips([self.rec()])
        other = self.trips([self.rec()], key='TMS-KEY-O')
        self.assertEqual(other['created'], 1)
        self.assertEqual(Load.objects.filter(external_id='T-100').count(), 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_load(self.co, self.cust, 'DUP', external_id='T-100')

    def test_other_company_record_never_updates_ours(self):
        mine = Load.objects.get(pk=self.trips([self.rec()])['load_ids'][0])
        self.trips([self.rec(rate=1)], key='TMS-KEY-O')
        mine.refresh_from_db()
        self.assertEqual(mine.total_amount, Decimal('30000.00'))

    def test_bad_status_is_an_error_not_a_write(self):
        self.trips([self.rec()])
        out = self.trips([self.rec(status='TELEPORTED', rate=1)])
        self.assertEqual(len(out['errors']), 1)
        self.assertEqual(Load.objects.get(external_id='T-100').total_amount, Decimal('30000.00'))


class InvoiceFlagTests(_Base):
    def test_invoiced_rate_change_flags_and_never_changes_the_invoice(self):
        load = Load.objects.get(pk=self.trips([self.rec()])['load_ids'][0])
        inv = Invoice.objects.create(company=self.co, customer=load.customer, load=load, invoice_number='INV-TMS-1',
                                     issue_date=date.today(), due_date=date.today(), subtotal=Decimal('30000'),
                                     vat_amount=Decimal('4500'), status='SENT')
        before = (inv.subtotal, inv.total_amount, inv.status)
        out = self.trips([self.rec(rate=33000)])
        inv.refresh_from_db()
        self.assertEqual((inv.subtotal, inv.total_amount, inv.status), before)
        load.refresh_from_db()
        self.assertEqual(load.invoice_mismatch['code'], 'invoice_differs_from_rate')
        self.assertEqual(load.invoice_mismatch['difference'], 3000.0)
        self.assertEqual(out['results'][0]['invoice_mismatch']['invoice_number'], inv.invoice_number)
        self.trips([self.rec(rate=30000)])
        load.refresh_from_db()
        self.assertEqual(load.invoice_mismatch, {})


class RecostTests(_Base):
    def test_quote_job_keeps_as_quoted_figures(self):
        api = APIClient()
        api.force_authenticate(self.user)
        q = priced_quote(self.co, self.cust, 'TMS-Q', vt=self.vt)
        load = Load.objects.get(pk=api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json').json()['id'])
        r = APIClient().post(SYNC, {'load_number': load.load_number, 'external_id': 'Q-EXT', 'distance': 700},
                             format='json', HTTP_X_API_KEY='TMS-KEY')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['sync']['changed'], ['distance'])
        load.refresh_from_db()
        self.assertEqual(load.external_id, 'Q-EXT')
        self.assertEqual(load.costing_source, 'computed')
        self.assertEqual(load.quoted_cost_floor, q.cost_floor)
        self.assertGreater(load.cost_floor, q.cost_floor)
        # Assigning a truck alone doesn't re-cost a quoted job.
        load2 = Load.objects.get(pk=api.post(
            f'/api/v1/quotes/{priced_quote(self.co, self.cust, "TMS-Q2", vt=self.vt).id}/convert_to_load/', {},
            format='json').json()['id'])
        APIClient().post(SYNC, {'load_number': load2.load_number, 'vehicle_plate': 'TM 01 GP'}, format='json',
                         HTTP_X_API_KEY='TMS-KEY')
        load2.refresh_from_db()
        self.assertEqual((load2.vehicle_id, load2.costing_source), (self.truck.id, 'quote'))

    def test_fleet_sync_by_external_id_and_complete(self):
        r = APIClient().post(SYNC, {'action': 'create', 'external_id': 'F-1', 'pickup_location': 'Pretoria',
                                    'delivery_location': 'Polokwane', 'total_amount': 9000}, format='json',
                             HTTP_X_API_KEY='TMS-KEY')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['sync']['created'])
        number = r.json()['load_number']
        r = APIClient().post(SYNC, {'action': 'complete', 'external_id': 'F-1'}, format='json',
                             HTTP_X_API_KEY='TMS-KEY')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Load.objects.get(load_number=number).status in ('DELIVERED', 'INVOICED'), True)


class ReturnLinkTests(_Base):
    def test_return_synced_before_outbound_links_when_it_arrives(self):
        ret = self.trips([self.rec(external_id='R-1', origin='Durban', destination='Johannesburg',
                                   pickup_date='2026-11-04', delivery_date='2026-11-05',
                                   return_of_external_id='O-1')])
        self.assertTrue(ret['results'][0]['return_link']['pending'])
        self.trips([self.rec(external_id='O-1')])
        r = Load.objects.get(external_id='R-1')
        self.assertEqual(r.return_of.external_id, 'O-1')
        self.assertEqual((r.return_link_source, r.return_of_external_ref), ('tms', ''))

    def test_link_unlink_by_external_id(self):
        self.trips([self.rec(external_id='O-2')])
        out = self.trips([self.rec(external_id='R-2', origin='Durban', destination='Johannesburg',
                                   pickup_date='2026-11-04', delivery_date='2026-11-05',
                                   return_of_external_id='O-2')])
        self.assertTrue(out['results'][0]['return_link']['linked'])
        out = self.trips([self.rec(external_id='R-2', origin='Durban', destination='Johannesburg',
                                   pickup_date='2026-11-04', delivery_date='2026-11-05',
                                   return_of_external_id=None)])
        self.assertTrue(out['results'][0]['return_link']['unlinked'])
        self.assertIsNone(Load.objects.get(external_id='R-2').return_of_id)

    def test_other_company_outbound_never_linked(self):
        self.trips([self.rec(external_id='O-3')], key='TMS-KEY-O')
        out = self.trips([self.rec(external_id='R-3', return_of_external_id='O-3')])
        self.assertTrue(out['results'][0]['return_link']['pending'])
        self.assertIsNone(Load.objects.get(company=self.co, external_id='R-3').return_of_id)


class RouteEngineTests(_Base):
    """TMS jobs with no toll / border figures use the toll/border engine on
    their own route (trip date, the truck's class, both legs)."""

    def _plaza(self):
        from core.models.toll_plaza import TollPlaza
        return (TollPlaza.objects.filter(is_active=True, country='SA', lat__isnull=False)
                .exclude(plaza_group__isnull=False).order_by('pk').first()
                or TollPlaza.objects.filter(is_active=True, lat__isnull=False).order_by('pk').first())

    def _line_through(self, plaza):
        lat, lng = float(plaza.lat), float(plaza.lng)
        return [{'lat': lat - 0.05, 'lon': lng}, {'lat': lat, 'lon': lng}, {'lat': lat + 0.05, 'lon': lng}]

    def test_tolls_from_route_geometry_on_trip_date(self):
        from core.services.toll_calculator import calculate_tolls_by_geometry, resolve_toll_class
        plaza = self._plaza()
        geom = self._line_through(plaza)
        rec = self.rec(external_id='G-1', toll_cost=None, route_geometry=geom, pickup_date='2026-11-02')
        rec.pop('toll_cost')
        load = Load.objects.get(pk=self.trips([rec])['load_ids'][0])
        ci = load.costing_inputs
        self.assertEqual(ci['route_costs']['trip_date'], '2026-11-02')
        self.assertIn('toll_cost_one_way', ci['route_costs']['filled'])
        cls = resolve_toll_class(self.vt.name, self.co)
        from datetime import date as _d
        expected = calculate_tolls_by_geometry(geom, cls.truck_type, trip_date=_d(2026, 11, 2))
        self.assertAlmostEqual(ci['toll_cost_one_way'], float(expected.total_excl_vat), places=2)
        self.assertEqual(load.costing_source, 'computed')
        tolls = next(ln for ln in load.costing_snapshot['lines'] if ln['key'] == 'tolls')
        self.assertAlmostEqual(tolls['amount'], float(expected.total_excl_vat), places=2)

    def test_tms_toll_figure_wins_and_no_geometry_stays_unknown(self):
        plaza = self._plaza()
        load = Load.objects.get(pk=self.trips([self.rec(external_id='G-2', toll_cost=123,
                                                        route_geometry=self._line_through(plaza))])['load_ids'][0])
        self.assertEqual(load.costing_inputs['toll_cost'], 123.0)
        self.assertNotIn('toll_cost_one_way', load.costing_inputs)
        rec = self.rec(external_id='G-3')
        rec.pop('toll_cost')
        load = Load.objects.get(pk=self.trips([rec])['load_ids'][0])
        self.assertEqual(load.costing_source, 'unknown')
        from core.services.trip_costing import missing_inputs
        # Not a prompt to enter them: the job is queued for routing.
        self.assertEqual([m['code'] for m in missing_inputs(load)], ['tolls_pending'])
        self.assertEqual(load.costing_inputs['route_job']['state'], 'pending')

    def test_round_trip_way_back_on_its_own_route(self):
        plaza = self._plaza()
        rec = self.rec(external_id='G-4', trip_type='ROUND_TRIP', route_geometry=[{'lat': -20.0, 'lon': 10.0},
                                                                                  {'lat': -20.1, 'lon': 10.1}],
                       return_route_geometry=self._line_through(plaza))
        rec.pop('toll_cost')
        load = Load.objects.get(pk=self.trips([rec])['load_ids'][0])
        ci = load.costing_inputs
        self.assertEqual(ci['toll_cost_one_way'], 0.0)
        self.assertGreater(ci['toll_cost_return'], 0)
        tolls = next(ln for ln in load.costing_snapshot['lines'] if ln['key'] == 'tolls')
        self.assertAlmostEqual(tolls['amount'], ci['toll_cost_return'], places=2)

    def test_cross_border_both_legs(self):
        rec = self.rec(external_id='G-5', destination='Gaborone, Botswana', countries=['SA', 'BW'], distance=360)
        load = Load.objects.get(pk=self.trips([rec])['load_ids'][0])
        ci = load.costing_inputs
        self.assertTrue(load.is_international)
        self.assertGreater(ci['border_cost'], 0)
        self.assertIn('border_cost_empty_return', ci)
        self.assertEqual(ci['route_costs']['countries'], ['SA', 'BW'])
        keys = {ln['key'] for ln in load.costing_snapshot['lines']}
        self.assertIn('border', keys)
