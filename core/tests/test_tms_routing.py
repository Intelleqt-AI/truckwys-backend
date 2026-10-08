"""TMS jobs with no route / tolls are routed once with TomTom (mocked) after
commit, then re-costed; dedupe, daily cap, re-route only on location change."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import Load
from core.services import tms_routing
from core.services.trip_costing import missing_inputs
from core.tests.quote_rules_fixtures import add_vehicle, official_price_now
from core.tests.trip_fixtures import make_company, make_customer, make_key, make_user, make_vehicle_type

TRIPS = '/api/v1/integrations/trips/sync/'


def _plaza_line():
    from core.models.toll_plaza import TollPlaza
    p = TollPlaza.objects.filter(is_active=True, lat__isnull=False, plaza_group__isnull=True).order_by('pk').first() \
        or TollPlaza.objects.filter(is_active=True, lat__isnull=False).order_by('pk').first()
    lat, lng = float(p.lat), float(p.lng)
    return [{'lat': lat - 0.05, 'lon': lng}, {'lat': lat, 'lon': lng}, {'lat': lat + 0.05, 'lon': lng}]


def fake_route(geom, km=600.0, minutes=420):
    return [{'distance_km': km, 'duration_minutes': minutes, 'duration_min': minutes, 'geometry': geom,
             'sections': []}]


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        official_price_now()
        cls.co = make_company('Route Co')
        cls.user = make_user('route_u', cls.co)
        make_customer(cls.co, 'route')
        make_key(cls.user, 'ROUTE-KEY')
        cls.vt = make_vehicle_type(cls.co)
        add_vehicle(cls.co, cls.vt, plate='RT 01 GP')

    def setUp(self):
        cache.clear()

    def sync(self, **kw):
        rec = {'external_id': 'RT-1', 'origin': 'Johannesburg', 'destination': 'Durban', 'distance': 600,
               'rate': 30000, 'weight': 10000, 'vehicle_plate': 'RT 01 GP', 'pickup_date': '2026-11-02',
               'pickup_lat': -26.2, 'pickup_lng': 28.04, 'delivery_lat': -29.85, 'delivery_lng': 31.02}
        rec.update(kw)
        with mock.patch('core.tasks.route_tms_load.apply_async') as queued:
            with self.captureOnCommitCallbacks(execute=True):
                r = APIClient().post(TRIPS, [rec], format='json', HTTP_X_API_KEY='ROUTE-KEY')
        self.assertEqual(r.status_code, 200, r.content)
        load = Load.objects.get(company=self.co, external_id=rec['external_id'])
        return load, queued


class RoutingTests(_Base):
    def test_queued_after_commit_then_routed_and_costed(self):
        load, queued = self.sync()
        self.assertEqual(queued.call_count, 1)
        self.assertEqual(queued.call_args.kwargs['args'], [load.pk])
        self.assertEqual(missing_inputs(load)[0], {'code': 'tolls_pending', 'prompt': 'Working out tolls…',
                                                   'pending': True, 'blocks': 'tolls_unknown'})
        geom = _plaza_line()
        with mock.patch('core.views.RouteCalculatorView._route', return_value=fake_route(geom)) as tomtom:
            self.assertEqual(tms_routing.route_load(load.pk), 'done')
        load.refresh_from_db()
        self.assertEqual(load.route_geometry, geom)
        self.assertEqual(load.costing_inputs['route_job']['state'], 'done')
        self.assertTrue(load.costing_inputs['route_job']['return_leg'])       # 600 km: empty return applies
        self.assertEqual(load.costing_source, 'computed')
        self.assertGreater(load.costing_inputs['toll_cost_one_way'], 0)
        self.assertGreater(load.costing_inputs['tolls_empty_return'], 0)
        self.assertEqual(load.costing_inputs['route_costs']['trip_date'], '2026-11-02')
        self.assertEqual(load.costing_inputs['duration_minutes'], 420.0)
        self.assertEqual(tomtom.call_count, 2)          # out + the way back
        self.assertEqual(missing_inputs(load), [])

    def test_dedupe_and_no_reroute_without_location_change(self):
        load, queued = self.sync()
        self.assertEqual(queued.call_count, 1)
        _, again = self.sync(rate=31000)                 # rate change: no re-route
        self.assertEqual(again.call_count, 0)
        with mock.patch('core.views.RouteCalculatorView._route', return_value=fake_route(_plaza_line())):
            tms_routing.route_load(load.pk)
        _, after = self.sync(rate=32000)
        self.assertEqual(after.call_count, 0)
        _, moved = self.sync(destination='Pietermaritzburg', delivery_lat=-29.6, delivery_lng=30.38)
        self.assertEqual(moved.call_count, 1)

    def test_own_tolls_or_geometry_never_routed(self):
        _, q1 = self.sync(external_id='RT-T', toll_cost=500)
        _, q2 = self.sync(external_id='RT-G', route_geometry=_plaza_line())
        self.assertEqual((q1.call_count, q2.call_count), (0, 0))

    def test_routing_failure_falls_back_to_the_prompt(self):
        load, _ = self.sync()
        with mock.patch('core.views.RouteCalculatorView._route', return_value=None):
            self.assertEqual(tms_routing.route_load(load.pk), 'failed')
        load.refresh_from_db()
        self.assertEqual(load.costing_inputs['route_job']['reason'], 'routing_unavailable')
        self.assertIn('tolls_unknown', [m['code'] for m in missing_inputs(load)])

    @override_settings(TMS_ROUTING_DAILY_CAP=1)
    def test_daily_cap_defers_to_tomorrow(self):
        a, _ = self.sync(external_id='RT-A')
        b, _ = self.sync(external_id='RT-B')
        with mock.patch('core.views.RouteCalculatorView._route', return_value=fake_route(_plaza_line())):
            self.assertEqual(tms_routing.route_load(a.pk), 'done')
            with mock.patch('core.tasks.route_tms_load.apply_async') as later:
                self.assertEqual(tms_routing.route_load(b.pk), 'deferred')
        self.assertGreater(later.call_args.kwargs['countdown'], 0)
        b.refresh_from_db()
        self.assertEqual(b.costing_inputs['route_job']['state'], 'deferred')
        self.assertEqual(missing_inputs(b)[0]['code'], 'tolls_pending')
