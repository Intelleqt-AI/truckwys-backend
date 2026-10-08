"""Load costing assumptions: convert_to_load carries the quote's compute()
snapshot; TMS-created loads are costed from their own data or marked unknown."""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Load
from core.services.trip_costing import cost_load
from core.tests.quote_rules_fixtures import add_vehicle, official_price_now
from core.tests.trip_fixtures import (make_company, make_customer, make_key, make_load, make_user,
                                      make_vehicle_type, priced_quote)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        official_price_now()
        cls.company = make_company('Costing Co')
        cls.user = make_user('cost_u', cls.company)
        cls.customer = make_customer(cls.company, 'cost')
        cls.vt = make_vehicle_type(cls.company)
        cls.vehicle = add_vehicle(cls.company, cls.vt, plate='CC 10 GP')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)


class ConvertCarriesCostingTests(_Base):
    def test_snapshot_floor_fuel_truck_and_margin_copied(self):
        q = priced_quote(self.company, self.customer, 'LC-1', vt=self.vt)
        self.assertTrue(q.costing_snapshot.get('lines'))
        self.assertTrue(q.empty_return_included)   # 600 km one way -> empty return assumed
        resp = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        load = Load.objects.get(pk=resp.json()['id'])
        self.assertEqual(load.costing_source, 'quote')
        self.assertEqual(load.cost_floor, q.cost_floor)
        self.assertEqual(load.quoted_cost_floor, q.cost_floor)
        self.assertEqual(load.quoted_price, q.total_amount)
        self.assertEqual(load.quoted_margin_pct, q.margin_percentage)
        self.assertTrue(load.empty_return_assumed)
        self.assertEqual(load.fuel_price_used, q.fuel_price_used)
        self.assertEqual(load.fuel_litres, q.fuel_litres)
        self.assertEqual(load.priced_vehicle_type_id, self.vt.id)
        legs = {ln['leg'] for ln in load.costing_snapshot['lines']}
        self.assertEqual(legs, {'loaded', 'empty_return'})
        self.assertEqual(load.costing_inputs['vehicle_type_id'], self.vt.id)
        self.assertEqual(load.costing_inputs['toll_cost'], 800.0)
        # Server-written: a PATCH can't change them.
        self.api.patch(f'/api/v1/loads/{load.id}/', {'cost_floor': '1', 'costing_source': 'computed'},
                       format='json')
        load.refresh_from_db()
        self.assertEqual(load.cost_floor, q.cost_floor)
        self.assertEqual(load.costing_source, 'quote')

    def test_return_load_booked_quote_has_no_empty_leg(self):
        q = priced_quote(self.company, self.customer, 'LC-2', vt=self.vt, include_empty_return=False)
        resp = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json')
        load = Load.objects.get(pk=resp.json()['id'])
        self.assertFalse(load.empty_return_assumed)
        self.assertEqual({ln['leg'] for ln in load.costing_snapshot['lines']}, {'loaded'})

    def test_round_trip_type_carried(self):
        q = priced_quote(self.company, self.customer, 'LC-3', vt=self.vt, trip_type='ROUND_TRIP',
                         return_location='Johannesburg')
        resp = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json')
        load = Load.objects.get(pk=resp.json()['id'])
        self.assertEqual(load.trip_type, 'ROUND_TRIP')
        self.assertEqual(load.return_location, 'Johannesburg')
        self.assertFalse(load.empty_return_assumed)


class ComputedCostingTests(_Base):
    def test_load_with_truck_tolls_and_time_is_computed(self):
        load = make_load(self.company, self.customer, 'LC-T1', distance='600', vehicle=self.vehicle,
                         costing_inputs={'toll_cost': 800, 'duration_minutes': 420})
        fields = cost_load(load)
        self.assertEqual(fields['costing_source'], 'computed')
        self.assertIsNotNone(load.cost_floor)
        self.assertTrue(load.empty_return_assumed)
        self.assertEqual(load.priced_vehicle_type_id, self.vt.id)
        # Same inputs as the priced quote -> the same floor (one compute()).
        q = priced_quote(self.company, self.customer, 'LC-T1Q', vt=self.vt)
        self.assertEqual(load.cost_floor, q.cost_floor)

    def test_no_truck_is_unknown_never_guessed(self):
        load = make_load(self.company, self.customer, 'LC-T2', costing_inputs={'toll_cost': 800})
        self.assertEqual(cost_load(load)['costing_source'], 'unknown')
        self.assertIsNone(load.cost_floor)

    def test_unknown_tolls_mark_unknown_with_snapshot_kept(self):
        load = make_load(self.company, self.customer, 'LC-T3', vehicle=self.vehicle,
                         costing_inputs={'duration_minutes': 600})
        self.assertEqual(cost_load(load)['costing_source'], 'unknown')
        self.assertIn('tolls_unknown', load.costing_snapshot['blocking'])

    def test_tms_created_load_is_costed(self):
        make_key(self.user, 'COST-KEY')
        r = APIClient().post('/api/v1/integrations/trips/sync/', [{
            'external_id': 'C-1', 'origin': 'Johannesburg', 'destination': 'Durban', 'distance': 600,
            'rate': 30000, 'weight': 10000, 'vehicle_plate': 'CC 10 GP', 'toll_cost': 800,
            'duration_minutes': 420}], format='json', HTTP_X_API_KEY='COST-KEY')
        self.assertEqual(r.status_code, 200, r.content)
        load = Load.objects.get(pk=r.json()['load_ids'][0])
        self.assertEqual(load.costing_source, 'computed')
        self.assertIsNotNone(load.cost_floor)
        r = APIClient().post('/api/v1/integrations/fleet/sync/', {
            'action': 'create', 'load_number': 'C-2', 'pickup_location': 'A', 'delivery_location': 'B',
            'distance': 600, 'vehicle_type': self.vt.name, 'toll_cost': 0, 'duration_minutes': 400},
            format='json', HTTP_X_API_KEY='COST-KEY')
        self.assertEqual(r.status_code, 200, r.content)
        load = Load.objects.get(load_number='C-2')
        self.assertEqual(load.costing_source, 'computed')
        self.assertEqual(load.costing_inputs['vehicle_type_id'], self.vt.id)


class MissingPromptTests(_Base):
    def test_unknown_job_says_what_to_add_and_has_no_fallback_estimate(self):
        from core.services import trip_economics as te
        load = make_load(self.company, self.customer, 'LC-M1', costing_inputs={'toll_cost': 800})
        cost_load(load)
        est = te.estimate(load)
        self.assertEqual((est['basis'], est['estimated_cost']), ('unknown', None))
        self.assertEqual(est['missing'], [{'code': 'no_vehicle', 'prompt': 'Add the truck to cost this job'}])
        body = self.api.get(f'/api/v1/loads/{load.id}/economics/').json()
        self.assertEqual(body['legs'][0]['missing'][0]['prompt'], 'Add the truck to cost this job')
        self.assertEqual(body['costing']['missing'][0]['code'], 'no_vehicle')
        load2 = make_load(self.company, self.customer, 'LC-M2', vehicle=self.vehicle,
                          costing_inputs={'duration_minutes': 600})
        cost_load(load2)
        codes = [m['code'] for m in te.estimate(load2)['missing']]
        self.assertIn('tolls_unknown', codes)


class MigrationAndDefaultsTests(_Base):
    def test_0167_backfill_copies_trip_shape_and_inputs(self):
        import importlib
        from django.apps import apps as global_apps
        from core.models import Load
        mig = importlib.import_module('core.migrations.0168_load_costing_backfill')
        q = priced_quote(self.company, self.customer, 'LC-MIG', vt=self.vt, trip_type='ROUND_TRIP',
                         return_location='Johannesburg')
        load = make_load(self.company, self.customer, 'LC-MIG-L', quote=q)
        mig.copy_from_quotes(global_apps, None)
        load.refresh_from_db()
        self.assertEqual((load.trip_type, load.return_location, load.costing_source), ('ROUND_TRIP', 'Johannesburg',
                                                                                       'quote'))
        self.assertEqual(load.costing_inputs['vehicle_type_id'], self.vt.id)
        self.assertEqual(load.costing_inputs['toll_cost'], 800.0)

    def test_new_not_null_columns_have_database_defaults(self):
        """An older app image (rollback) can still insert loads."""
        from django.db.models import NOT_PROVIDED
        from core.models import Load
        for f in Load._meta.concrete_fields:
            if f.name in ('trip_type', 'return_location', 'return_cargo', 'costing_source', 'costing_inputs',
                          'costing_snapshot', 'fuel_price_source', 'fuel_zone', 'return_link_source',
                          'expecting_return', 'costs_closed', 'external_id', 'external_source',
                          'return_of_external_ref', 'invoice_mismatch', 'estimate_basis'):
                self.assertIsNot(f.db_default, NOT_PROVIDED, f.name)


class BackfillCommandTests(_Base):
    def test_dry_run_then_apply_is_rerunnable(self):
        from io import StringIO
        from django.core.management import call_command
        from core.models import Load
        q = priced_quote(self.company, self.customer, 'LC-BF', vt=self.vt)
        load = make_load(self.company, self.customer, 'LC-BF-L', quote=q)     # booked by an old image
        out = StringIO()
        call_command('backfill_load_costing', stdout=out)
        self.assertIn('1 converted loads have no costing', out.getvalue())
        load.refresh_from_db()
        self.assertEqual(load.costing_source, '')
        call_command('backfill_load_costing', '--apply', stdout=StringIO())
        load.refresh_from_db()
        self.assertEqual((load.costing_source, load.cost_floor, load.quoted_price), ('quote', q.cost_floor,
                                                                                    q.total_amount))
        out = StringIO()
        call_command('backfill_load_costing', '--apply', stdout=out)
        self.assertIn('0 loads back-filled', out.getvalue())

    def test_migration_backfill_unpriced_quote_copies_quoted_fields_and_cleans_types(self):
        import importlib
        from django.apps import apps as global_apps
        from core.models import Quote
        mig = importlib.import_module('core.migrations.0168_load_costing_backfill')
        q = priced_quote(self.company, self.customer, 'LC-UP', vt=self.vt)
        Quote.objects.filter(pk=q.pk).update(costing_snapshot={}, costing_inputs={
            'vehicle_type_id': self.vt.id, 'include_empty_return': 'false', 'border_cost': 'lots'})
        load = make_load(self.company, self.customer, 'LC-UP-L', quote=q)
        mig.copy_from_quotes(global_apps, None)
        load.refresh_from_db()
        self.assertEqual(load.costing_source, '')
        self.assertEqual(load.quoted_price, q.total_amount)
        self.assertEqual(load.fuel_price_used, q.fuel_price_used)
        self.assertIs(load.costing_inputs['include_empty_return'], False)
        self.assertNotIn('border_cost', load.costing_inputs)
