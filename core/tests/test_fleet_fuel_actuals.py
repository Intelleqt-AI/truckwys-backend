"""Measured fuel use from the fleet tracker (core.services.fleet_fuel_actuals)
and its use in pricing. Every Cartrack call is a mocked fixture shaped like
Cartrack's OpenAPI spec (GET /vehicles sensors, GET /vehicles/:reg/odometer,
GET /fuel/consumed/:reg, GET /fuel/level/:reg); no network anywhere.

Golden-independent: compute() is untouched; these tests only check the
inputs it gets and the DB-side labels/warnings around it.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (Company, Customer, Driver, FleetFuelMeasurement, FleetFuelSyncRun, FuelPrice, Load, Trip,
                         Vehicle, VehicleType)
from core.services import fleet_fuel_actuals as ffa
from core.services import quote_costing as qc

SAST = ZoneInfo('Africa/Johannesburg')
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=SAST)
User = get_user_model()


def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=SAST)


class FakeCartrack:
    """Cartrack Fleet API double. Each truck drives `km_per_day` evenly; its
    fuel follows burn = rated x (0,70 + 0,30 x ratio) with ratio from its
    `segments` [(start, end, ratio)] (else `idle_ratio`). Payloads mirror the
    OpenAPI response `data` objects."""

    def __init__(self):
        self.trucks = {}
        self.calls = []

    def add(self, reg, *, rated, km_per_day=300.0, sensors=None, segments=(), idle_ratio=0.0,
            reset_windows=(), fuel_level=False, calibrated=True):
        self.trucks[reg] = dict(rated=rated, kmh=km_per_day / 24.0, segments=list(segments), idle_ratio=idle_ratio,
                                sensors=sensors if sensors is not None else (
                                    {'fuel_canbus_consumed': not fuel_level, 'fuel_canbus_level': fuel_level,
                                     'fuel_analog_level': False, 'electric_battery': False,
                                     'electric_charging': False}),
                                reset_windows=list(reset_windows), calibrated=calibrated)

    def get_vehicles(self):
        self.calls.append(('GET', '/vehicles'))
        return [{'vehicle_id': i + 1000, 'registration': reg, 'chassis_number': f'CH{i}', 'fuel_capacity': 600,
                 'sensors': t['sensors']} for i, (reg, t) in enumerate(self.trucks.items())]

    # integrals ------------------------------------------------------------
    # A segment is (start, end, ratio) at the truck's base speed, or
    # (start, end, ratio, kmh): a real long-haul run (e.g. 76 km/h for 7,5 h).
    @staticmethod
    def _seg(seg):
        return (seg[0], seg[1], seg[2], seg[3] if len(seg) > 3 else None)

    def _base_km(self, t, a, b):
        return max((b - a).total_seconds(), 0) / 3600.0 * t['kmh']

    def _km(self, t, a, b):
        km = self._base_km(t, a, b)
        for s, e, _r, kmh in map(self._seg, t['segments']):
            lo, hi = max(a, s), min(b, e)
            if hi > lo and kmh is not None:
                km += (hi - lo).total_seconds() / 3600.0 * (kmh - t['kmh'])
        return km

    def _litres(self, t, a, b):
        total, covered = 0.0, 0.0
        for s, e, r, _kmh in map(self._seg, t['segments']):
            lo, hi = max(a, s), min(b, e)
            if hi > lo:
                km = self._km(t, lo, hi)
                total += km * t['rated'] * (0.70 + 0.30 * r) / 100
                covered += km
        rest = self._km(t, a, b) - covered
        return total + rest * t['rated'] * (0.70 + 0.30 * t['idle_ratio']) / 100

    def get_odometer(self, reg, start, end):
        self.calls.append(('GET', f'/vehicles/{reg}/odometer'))
        t = self.trucks[reg]
        a, b = aware(start), aware(end)
        metres = int(round(self._km(t, a, b) * 1000))
        reset = any(s <= a < e for s, e in t['reset_windows'])
        return {'vehicle_id': 1, 'registration': reg, 'chassis_number': 'CH', 'terminal_has_changed': False,
                'terminal_serial': 'TS1', 'start_timestamp': start.strftime('%Y-%m-%d %H:%M:%S+02'),
                'end_timestamp': end.strftime('%Y-%m-%d %H:%M:%S+02'), 'start_odometer_value': 1_000_000,
                'end_odometer_value': 1_000_000 + metres, 'distance': metres, 'odometer_reset': reset,
                'current_odometer_value': 1_000_000 + metres}

    def get_fuel_consumed(self, reg, start, end):
        self.calls.append(('GET', f'/fuel/consumed/{reg}'))
        t = self.trucks[reg]
        litres = self._litres(t, aware(start), aware(end))
        base = 150_000
        return {'vehicle_id': 1, 'registration': reg, 'fuel_consumed_start': base,
                'fuel_consumed_end': base + int(round(litres)), 'fuel_consumed': int(round(litres))}

    def get_fuel_level(self, reg, start, end):
        self.calls.append(('GET', f'/fuel/level/{reg}'))
        t = self.trucks[reg]
        litres = self._litres(t, aware(start), aware(end))
        return {'vehicle_id': 1, 'registration': reg,
                'start_period': {'liters': 420.5, 'timestamp': str(start), 'accurate': True},
                'end_period': {'liters': 310.2, 'timestamp': str(end), 'accurate': True},
                'estimated_fuel_used': round(litres, 2), 'calibrated': t['calibrated']}


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------

class PureWindowTests(SimpleTestCase):
    def test_rule_constants_match_pricing(self):
        self.assertEqual((ffa.LOADED_BASE, ffa.LOADED_SLOPE), (qc.LOADED_BASE, qc.LOADED_SLOPE))

    def test_odometer_flags_and_metres(self):
        self.assertEqual(ffa.odometer_km({'distance': 18_400_000}), (18400.0, None))
        self.assertEqual(ffa.odometer_km({'distance': 5, 'odometer_reset': True})[1], 'odometer_reset')
        self.assertEqual(ffa.odometer_km({'distance': 5, 'terminal_has_changed': True})[1], 'terminal_changed')
        self.assertEqual(ffa.odometer_km({'distance': None, 'start_odometer_value': 1000,
                                          'end_odometer_value': 3000}), (2.0, None))
        self.assertEqual(ffa.odometer_km(None)[1], 'no_odometer')

    def test_fuel_can_bus_and_level_checks(self):
        self.assertEqual(ffa.fuel_litres({'fuel_consumed': 44, 'fuel_consumed_start': 15859,
                                          'fuel_consumed_end': 15903}, 'can_bus'), (44.0, None))
        self.assertEqual(ffa.fuel_litres({'fuel_consumed_start': 900, 'fuel_consumed_end': 100}, 'can_bus')[1],
                         'fuel_counter_reset')
        self.assertEqual(ffa.fuel_litres({'estimated_fuel_used': 7.5, 'calibrated': False}, 'fuel_level')[1],
                         'sensor_not_calibrated')
        self.assertEqual(ffa.fuel_litres({'estimated_fuel_used': 7.5, 'calibrated': True,
                                          'end_period': {'accurate': False}}, 'fuel_level')[1],
                         'fuel_level_provisional')
        self.assertEqual(ffa.fuel_litres({'estimated_fuel_used': 7.5, 'calibrated': True}, 'fuel_level'),
                         (7.5, None))
        self.assertEqual(ffa.fuel_litres({}, None)[1], 'no_fuel_data')

    def test_window_outliers(self):
        a = datetime(2026, 9, 1, tzinfo=SAST)
        b = a + timedelta(days=30)
        ok = ffa.assess_window(a, b, {'distance': 9_000_000}, {'fuel_consumed': 3000}, 'can_bus')
        self.assertIsNone(ok['reject'])
        self.assertAlmostEqual(ok['l_per_100km'], 33.333, places=2)
        self.assertEqual(ffa.assess_window(a, b, {'distance': 9_000_000}, {'fuel_consumed': 300}, 'can_bus')['reject'],
                         'burn_outlier')
        self.assertEqual(ffa.assess_window(a, b, {'distance': 60_000_000}, {'fuel_consumed': 20000},
                                           'can_bus')['reject'], 'distance_implausible')
        self.assertEqual(ffa.assess_window(a, b, {'distance': 10_000}, {'fuel_consumed': 3}, 'can_bus')['reject'],
                         'too_short')

    def test_rated_from_recorded_loads_inverts_the_pricing_rule(self):
        # 3 000 km on loads at ratio 0,8 and 1 000 km empty, rated 40.
        s = ffa.empty_sums()
        s.update(km=4000.0, litres=3000 * 40 * (0.7 + 0.3 * 0.8) / 100 + 1000 * 40 * 0.7 / 100,
                 loaded_km=3000.0, loaded_litres=3000 * 40 * (0.7 + 0.3 * 0.8) / 100,
                 loaded_weighted_km=3000 * (0.7 + 0.3 * 0.8), loaded_ratio_km=3000 * 0.8)
        f = ffa.figures(s)
        self.assertEqual(f['rated_method'], 'loaded_trips')
        self.assertAlmostEqual(f['rated_burn_l_per_100km'], 40.0)
        self.assertAlmostEqual(f['loaded_l_per_100km'], 37.6)
        self.assertAlmostEqual(f['other_l_per_100km'], 28.0)
        self.assertAlmostEqual(f['loaded_mean_load_ratio'], 0.8)

    def test_overall_assumes_half_load_on_unlinked_km(self):
        s = ffa.empty_sums()
        s.update(km=18400.0, litres=18400 * 34.0 / 100)
        f = ffa.figures(s)
        self.assertEqual(f['rated_method'], 'overall_assumed')
        self.assertAlmostEqual(f['rated_burn_l_per_100km'], 34.0 / 0.85)
        self.assertAlmostEqual(f['l_per_100km'], 34.0)

    def test_confidence_and_plausibility(self):
        self.assertEqual(ffa.confidence_for(1999, 'overall_assumed', 'can_bus'), 'insufficient')
        self.assertEqual(ffa.confidence_for(3000, 'overall_assumed', 'can_bus'), 'low')
        self.assertEqual(ffa.confidence_for(6000, 'overall_assumed', 'fuel_level'), 'medium')
        self.assertEqual(ffa.confidence_for(12000, 'loaded_trips', 'can_bus'), 'high')
        self.assertEqual(ffa.plausibility(15.0, 34), 'implausibly_low')
        self.assertIsNone(ffa.plausibility(15.0, 4))
        self.assertEqual(ffa.plausibility(95.0, 34), 'implausibly_high')


# ---------------------------------------------------------------------------
# Refresh + pricing (DB)
# ---------------------------------------------------------------------------

def official_rows():
    FuelPrice.objects.create(date=date(2026, 10, 7), diesel_inland=Decimal('32.7989'),
                             diesel_coastal=Decimal('31.9269'), source='FIASA', diesel_grade='50ppm',
                             effective_from=datetime(2026, 10, 7, 0, 1, tzinfo=SAST))


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        clock = patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        # No network anywhere in these tests.
        net = patch('requests.Session.request', side_effect=AssertionError('network call in a test'))
        net.start()
        self.addCleanup(net.stop)
        official_rows()
        self.company = Company.objects.create(
            company_name='Measured Haulage', margin_target_pct=Decimal('10'),
            driver_allowance_per_night=Decimal('450'), cartrack_username='api_user', cartrack_password='secret',
            cartrack_base_url='https://fleetapi-za.cartrack.com', cartrack_connected_at=NOW - timedelta(days=200))
        self.user = User.objects.create_user(username='fleetadmin', password='x', company=self.company, role='ADMIN')
        self.vt = VehicleType.objects.create(company=self.company, name='Superlink', capacity=34, max_distance=3000,
                                             base_rate=20, fuel_consumption_l_per_100km=42)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.fake = FakeCartrack()

    def truck(self, plate, vt=None, capacity='34000', **extra):
        return Vehicle.objects.create(company=self.company, plate=plate, make='Volvo', model='FH', type='Truck',
                                      capacity=Decimal(capacity), fuel_type='Diesel', status='AVAILABLE',
                                      vehicle_type=vt or self.vt, **extra)

    def refresh(self):
        return ffa.refresh_company(self.company, client=self.fake, now=NOW)

    def type_row(self, vt=None):
        return FleetFuelMeasurement.objects.get(company=self.company, scope='VEHICLE_TYPE', vehicle_type=vt or self.vt)

    PAYLOAD = {'distance_km': 568.4, 'duration_minutes': 440, 'weight': 28000, 'vehicle_type': 'Superlink',
               'toll_cost': 1043.48, 'trip_type': 'ONE_WAY'}


class RefreshTests(_Base):
    def test_overall_figure_per_truck_and_type(self):
        self.truck('CA100GP')
        self.truck('CA200GP')
        # Average burn 34 L/100km on all km (ratio 0,5 on average): rated 40.
        self.fake.add('CA100GP', rated=40.0, idle_ratio=0.5, km_per_day=220)
        self.fake.add('CA200GP', rated=40.0, idle_ratio=0.5, km_per_day=200)
        run = self.refresh()
        self.assertEqual(run.status, 'ok')
        row = self.type_row()
        self.assertTrue(row.sufficient)
        self.assertEqual(row.rated_method, 'overall_assumed')
        self.assertEqual(row.vehicles_count, 2)
        self.assertAlmostEqual(row.distance_km, 90 * 420, delta=5)
        self.assertAlmostEqual(row.l_per_100km, 34.0, delta=0.05)
        self.assertAlmostEqual(row.rated_burn_l_per_100km, 40.0, delta=0.06)
        self.assertEqual(row.confidence, 'medium')
        self.assertEqual(row.fuel_source, 'can_bus')
        self.assertEqual(row.period_end, (NOW - timedelta(hours=24)).replace(minute=0))
        self.assertEqual(FleetFuelMeasurement.objects.filter(scope='VEHICLE', company=self.company).count(), 2)
        # 1 roster + 2 trucks x 3 windows x (odometer + fuel); nothing else.
        self.assertEqual(len(self.fake.calls), 1 + 2 * 3 * 2)

    def test_recorded_loads_give_the_loaded_figure(self):
        v = self.truck('CA300GP')
        cust = Customer.objects.create(company=self.company, name='Acme', email='a@x.test')
        du = User.objects.create_user(username='drv', password='x')
        drv = Driver.objects.create(company=self.company, user=du, license_number='D1',
                                    license_expiry=date(2028, 1, 1), license_state='GP', hire_date=date(2020, 1, 1))
        segments = []
        start = ffa.period_bounds(NOW)[0] + timedelta(days=1)
        for i in range(25):           # 25 JHB-DBN runs: 7,5 h at 76 km/h (570 km), 27,2 t (ratio 0,8)
            a = start + timedelta(days=3 * i, hours=5)
            b = a + timedelta(hours=7.5)
            ld = Load.objects.create(
                company=self.company, load_number=f'L{i}', customer=cust, pickup_location='JHB', pickup_city='JHB',
                pickup_state='GP', pickup_zip='1', pickup_date=a, delivery_location='DBN', delivery_city='DBN',
                delivery_state='KZN', delivery_zip='2', delivery_date=b, cargo_description='Steel',
                weight=Decimal('27200'), rate=Decimal('30000'), total_amount=Decimal('30000'), status='DELIVERED')
            Trip.objects.create(load=ld, vehicle=v, driver=drv, origin='JHB', destination='DBN',
                                estimated_distance_km=Decimal('568'), estimated_duration_hours=Decimal('8'),
                                start_time=a, end_time=b, status='COMPLETED')
            segments.append((a, b, 0.8, 76.0))
        self.fake.add('CA300GP', rated=41.0, km_per_day=120, segments=segments, idle_ratio=0.0)
        self.refresh()
        row = self.type_row()
        self.assertEqual(row.rated_method, 'loaded_trips')
        self.assertEqual(row.windows_rejected, 0)
        self.assertAlmostEqual(row.loaded_km, 25 * 570, delta=5)
        self.assertAlmostEqual(row.loaded_mean_load_ratio, 0.8, places=3)
        self.assertAlmostEqual(row.rated_burn_l_per_100km, 41.0, delta=0.15)
        self.assertAlmostEqual(row.other_l_per_100km, 41.0 * 0.7, delta=0.15)
        self.assertEqual(row.confidence, 'high')     # 14 250 loaded km, CAN counter

    def test_quality_checks_reject_and_report(self):
        self.truck('CA400GP')
        self.truck('CA500GP')
        self.truck('CA600GP')
        self.truck('CA700GP')     # no fuel sensor
        start, _ = ffa.period_bounds(NOW)
        self.fake.add('CA400GP', rated=40.0, idle_ratio=0.5,
                      reset_windows=[(start, start + timedelta(days=30))])
        self.fake.add('CA500GP', rated=40.0, idle_ratio=0.5, fuel_level=True)
        self.fake.add('CA600GP', rated=70.0, idle_ratio=0.5)   # 59,5 average: far off the type median
        self.fake.add('CA700GP', rated=40.0, sensors={'fuel_canbus_consumed': False, 'fuel_canbus_level': False,
                                                      'fuel_analog_level': False})
        self.fake.add('ZZ999GP', rated=40.0)                     # in Cartrack, not in TruckWys
        run = self.refresh()
        self.assertEqual(run.summary['unmatched'], ['ZZ999GP'])
        self.assertEqual(run.summary['no_fuel_sensor'], ['CA700GP'])
        v4 = FleetFuelMeasurement.objects.get(scope='VEHICLE', vehicle__plate='CA400GP')
        self.assertEqual(v4.windows_used, 2)
        self.assertEqual(v4.rejections[0]['reason'], 'odometer_reset')
        row = self.type_row()
        self.assertEqual(row.vehicles_count, 2)                  # CA600GP left out as an outlier
        self.assertIn('differs_from_type', [r['reason'] for r in row.rejections])
        out = next(r for r in row.rejections if r['reason'] == 'differs_from_type')
        self.assertRegex(out['detail'], r'^average \d+,\d L/100 km vs type median \d+,\d$')   # SA format
        self.assertEqual(row.fuel_source, 'mixed')
        self.assertAlmostEqual(row.rated_burn_l_per_100km, 40.0, delta=0.1)

    def test_uncalibrated_level_sensor_gives_no_figure(self):
        self.truck('CA800GP')
        self.fake.add('CA800GP', rated=40.0, fuel_level=True, calibrated=False)
        self.refresh()
        row = self.type_row()
        self.assertFalse(row.sufficient)
        self.assertEqual(row.confidence, 'insufficient')

    def test_too_little_distance_is_insufficient(self):
        self.truck('CA900GP')
        self.fake.add('CA900GP', rated=40.0, km_per_day=20)    # 1 800 km in 90 days
        self.refresh()
        self.assertFalse(self.type_row().sufficient)

    def test_implausible_figure_rejected(self):
        self.truck('CB100GP')
        self.fake.add('CB100GP', rated=17.0, idle_ratio=0.5)   # 14,5 average on a 34 t truck
        self.refresh()
        row = self.type_row()
        self.assertEqual(row.confidence, 'rejected')
        self.assertFalse(row.sufficient)

    def test_not_connected_skips_without_calls(self):
        self.company.cartrack_connected_at = None
        self.company.save()
        run = self.refresh()
        self.assertEqual(run.status, 'skipped')
        self.assertEqual(self.fake.calls, [])

    def test_ctrlfleet_only_explains_why(self):
        self.company.cartrack_connected_at = None
        self.company.ctrlfleet_api_key = 'k'
        self.company.ctrlfleet_connected_at = NOW
        self.company.save()
        run = self.refresh()
        self.assertEqual(run.status, 'skipped')
        self.assertIn('CtrlFleet', run.message)

    def test_api_failure_on_roster_fails_the_run(self):
        from core.integrations.cartrack import CartrackAPIError
        with patch.object(self.fake, 'get_vehicles', side_effect=CartrackAPIError('401')):
            run = self.refresh()
        self.assertEqual(run.status, 'failed')

    def test_refresh_keeps_admin_choice_and_retires_vanished_types(self):
        self.truck('CC100GP')
        self.fake.add('CC100GP', rated=40.0, idle_ratio=0.5)
        self.refresh()
        row = self.type_row()
        row.burn_mode = 'CONFIGURED'
        row.save()
        self.refresh()
        self.assertEqual(self.type_row().burn_mode, 'CONFIGURED')
        Vehicle.objects.filter(plate='CC100GP').update(vehicle_type=None)
        self.refresh()
        self.assertFalse(self.type_row().sufficient)

    def test_task_respects_connection(self):
        from core.tasks import refresh_fleet_fuel_actuals
        other = Company.objects.create(company_name='Not connected', cartrack_username='u', cartrack_password='p')
        with patch('core.services.fleet_fuel_actuals.refresh_company') as m:
            m.return_value.status = 'ok'
            out = refresh_fleet_fuel_actuals()
        self.assertEqual([c.args[0].id for c in m.call_args_list], [self.company.id])
        self.assertNotIn(other.id, out)


class PricingTests(_Base):
    def measured(self, rated=40.2, km=18400.0, **over):
        values = dict(company=self.company, scope='VEHICLE_TYPE', vehicle_type=self.vt, provider='cartrack',
                      fuel_source='can_bus', distance_km=km, litres=km * rated * 0.85 / 100,
                      l_per_100km=rated * 0.85, rated_burn_l_per_100km=rated, rated_method='overall_assumed',
                      vehicles_count=3, confidence='high', sufficient=True, computed_at=NOW - timedelta(days=2),
                      period_start=NOW - timedelta(days=91), period_end=NOW - timedelta(days=1))
        values.update(over)
        return FleetFuelMeasurement.objects.create(**values)

    def costing(self, **over):
        return qc.costing_for_payload({**self.PAYLOAD, **over}, self.company)

    def test_measured_figure_is_the_rated_burn(self):
        self.measured()
        c = self.costing()
        self.assertEqual(c['inputs']['vehicle']['rated_burn_l_per_100km'], 40.2)
        rb = c['resolution']['rated_burn']
        self.assertEqual(rb['source'], 'measured')
        self.assertEqual(rb['label'], 'Measured by Cartrack: 40,2 L/100 km over 18 400 km (90 days)')
        self.assertEqual(rb['configured'], 42.0)
        # compute() itself is unchanged: same output as feeding it the figure directly.
        direct = qc.compute({**c['inputs'], 'vehicle': {**c['inputs']['vehicle']}})
        self.assertEqual(direct['lines'], c['lines'])
        self.assertAlmostEqual(c['vehicle']['burn_loaded_l_per_100km'], 40.2 * (0.7 + 0.3 * 28 / 34))

    def test_configured_when_no_or_unusable_measurement(self):
        self.assertEqual(self.costing()['resolution']['rated_burn']['source'], 'configured')
        self.assertEqual(self.costing()['resolution']['rated_burn']['label'],
                         'Your figure: 42,0 L/100 km (vehicle type settings)')
        row = self.measured(sufficient=False, confidence='insufficient', distance_km=1500)
        self.assertEqual(self.costing()['inputs']['vehicle']['rated_burn_l_per_100km'], 42.0)
        row.sufficient, row.confidence, row.computed_at = True, 'high', NOW - timedelta(days=40)
        row.save()
        self.assertEqual(self.costing()['resolution']['rated_burn']['source'], 'configured')   # stale
        row.computed_at = NOW
        row.save()
        self.company.cartrack_connected_at = None
        self.company.save()
        self.assertEqual(self.costing()['resolution']['rated_burn']['source'], 'configured')   # disconnected

    def test_overrides(self):
        row = self.measured()
        c = self.costing(use_configured_burn=True)
        self.assertEqual(c['inputs']['vehicle']['rated_burn_l_per_100km'], 42.0)
        self.assertEqual(c['resolution']['rated_burn']['chosen_by'], 'quote')
        row.burn_mode = 'CONFIGURED'
        row.save()
        c = self.costing()
        self.assertEqual(c['resolution']['rated_burn']['source'], 'configured')
        self.assertIn('measured 40,2 L/100 km', c['resolution']['rated_burn']['label'])

    def test_shared_default_type_is_labelled_standard_estimate(self):
        shared = VehicleType.objects.create(company=None, name='Tri-axle', capacity=30, max_distance=3000,
                                            base_rate=20, fuel_consumption_l_per_100km=38)
        burn = qc.resolve_rated_burn(self.company, shared)
        self.assertEqual(burn['source'], 'standard')
        self.assertTrue(burn['label'].startswith('Standard estimate: 38,0 L/100 km'))

    def test_differs_warning_when_configured_prices(self):
        self.measured(rated=34.0, burn_mode='CONFIGURED')
        c = self.costing()
        w = next(w for w in c['warnings'] if w['code'] == 'truck_burn_differs_measured')
        self.assertEqual(w['severity'], 'warn')
        self.assertEqual(w['detail'], '42,0 set, 34,0 L/100 km measured.')
        self.assertEqual(w['actions'][0], {'id': 'use_measured_burn', 'label': 'Use measured figure'})
        self.assertTrue(c['can_send'])

    def test_suspect_truck_uses_measured_data(self):
        self.vt.fuel_consumption_l_per_100km = Decimal('12')
        self.vt.save()
        # Typed 12 L/100 km on a 34 t truck: measured figure takes over, no suspect warning.
        self.measured(rated=40.2)
        self.assertNotIn('truck_burn_suspect', [w['code'] for w in self.costing()['warnings']])
        # Admin pinned the typed figure: the suspect warning names the measured one.
        FleetFuelMeasurement.objects.filter(company=self.company).update(burn_mode='CONFIGURED')
        w = next(w for w in self.costing()['warnings'] if w['code'] == 'truck_burn_suspect')
        self.assertEqual(w['detail'], '12 L/100 km is low for a 34 t truck; Cartrack measured 40,2 L/100 km.')
        self.assertEqual(w['actions'][0]['id'], 'use_measured_burn')
        self.assertNotIn('truck_burn_differs_measured', [w['code'] for w in self.costing()['warnings']])

    def test_pricing_analysis_fuel_line_carries_the_label(self):
        from core.services.pricing_analysis import build_cost_floor
        self.measured()
        floor, costing = build_cost_floor(dict(self.PAYLOAD), company=self.company)
        fuel = next(ln for ln in floor['lines'] if ln['key'] == 'fuel')
        self.assertEqual(fuel['burn_source'], 'measured')
        self.assertIn({'label': 'Truck fuel use', 'source': 'measured',
                       'value': 'Measured by Cartrack: 40,2 L/100 km over 18 400 km (90 days)'}, fuel['details'])

    def test_saved_quote_keeps_use_configured_flag(self):
        self.assertIn('use_configured_burn', qc.COSTING_INPUT_KEYS)


class FleetFuelApiTests(_Base):
    URL = '/api/v1/fleet/fuel-actuals/'

    def setUp(self):
        super().setUp()
        self.truck('CD100GP')
        self.fake.add('CD100GP', rated=40.0, idle_ratio=0.5)

    def test_get_shows_measured_vs_configured(self):
        self.refresh()
        body = self.api.get(self.URL).json()
        self.assertEqual(body['connection'], {'provider': 'cartrack', 'reason': None, 'can_measure': True})
        self.assertEqual(body['last_run']['status'], 'ok')
        vt = next(r for r in body['vehicle_types'] if r['id'] == self.vt.id)
        self.assertEqual(vt['configured_l_per_100km'], 42.0)
        self.assertEqual(vt['in_use']['source'], 'measured')
        self.assertAlmostEqual(vt['measured']['rated_burn_l_per_100km'], 40.0, delta=0.1)
        self.assertTrue(vt['can_use_measured'])
        self.assertEqual(body['vehicles'][0]['plate'], 'CD100GP')
        self.assertEqual(body['vehicles'][0]['measured']['vehicles_count'], 1)
        # The vehicle-types API shows it too.
        rows = self.api.get('/api/v1/vehicle-types/').json()
        row = next(r for r in rows if r['id'] == self.vt.id)
        self.assertEqual(row['fuel_use_in_use']['source'], 'measured')

    def test_burn_mode(self):
        url = f'/api/v1/fleet/fuel-actuals/vehicle-types/{self.vt.id}/burn-mode/'
        r = self.api.post(url, {'mode': 'MEASURED'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['code'], 'not_measured')
        r = self.api.post(url, {'mode': 'CONFIGURED'}, format='json')     # pin before any measurement
        self.assertEqual(r.status_code, 200, r.content)
        self.refresh()
        self.assertEqual(qc.resolve_rated_burn(self.company, self.vt)['source'], 'configured')
        r = self.api.post(url, {'mode': 'MEASURED'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['in_use']['source'], 'measured')
        self.assertEqual(self.type_row().burn_mode_set_by, self.user)
        self.assertEqual(self.api.post(url, {'mode': 'x'}, format='json').status_code, 400)

    def test_admin_only_and_tenant_scoped(self):
        staff = User.objects.create_user(username='disp', password='x', company=self.company, role='DISPATCHER')
        api = APIClient()
        api.force_authenticate(staff)
        url = f'/api/v1/fleet/fuel-actuals/vehicle-types/{self.vt.id}/burn-mode/'
        self.assertEqual(api.post(url, {'mode': 'CONFIGURED'}, format='json').status_code, 403)
        self.assertEqual(api.get(self.URL).status_code, 200)
        other = Company.objects.create(company_name='Other')
        theirs = VehicleType.objects.create(company=other, name='Theirs', capacity=34, max_distance=1, base_rate=1)
        r = self.api.post(f'/api/v1/fleet/fuel-actuals/vehicle-types/{theirs.id}/burn-mode/', {'mode': 'AUTO'},
                          format='json')
        self.assertEqual(r.status_code, 404)

    def test_refresh_queues_once_and_never_calls_the_tracker_in_the_request(self):
        with patch('core.tasks.refresh_fleet_fuel_actuals.delay') as delay:
            r1 = self.api.post('/api/v1/fleet/fuel-actuals/refresh/')
            r2 = self.api.post('/api/v1/fleet/fuel-actuals/refresh/')
        self.assertEqual((r1.status_code, r2.status_code), (202, 429))
        self.assertEqual(r2.json()['error'], 'You can refresh again at 09:15.')     # 15 min from the start, SAST
        self.assertEqual(r2.json()['code'], 'refresh_cooldown')
        delay.assert_called_once_with(company_id=self.company.id)
        self.assertEqual(self.fake.calls, [])
        body = self.api.get(self.URL).json()
        self.assertTrue(body['refresh_queued'])
        self.assertEqual(body['refresh_next_at'], '2026-10-07T09:15:00+02:00')

    def test_cooldown_holds_after_the_run_and_ends_at_15_minutes(self):
        from core.tasks import refresh_fleet_fuel_actuals
        with patch('core.tasks.refresh_fleet_fuel_actuals.delay') as delay:
            self.assertEqual(self.api.post('/api/v1/fleet/fuel-actuals/refresh/').status_code, 202)
        with patch.object(ffa, 'refresh_company', side_effect=lambda c: FleetFuelSyncRun.objects.create(
                company=c, status='ok', finished_at=NOW + timedelta(minutes=3))):
            refresh_fleet_fuel_actuals(company_id=self.company.id)
        body = self.api.get(self.URL).json()
        self.assertFalse(body['refresh_queued'])                       # done
        self.assertIsNotNone(body['refresh_next_at'])                  # but still cooling down
        with patch('core.tasks.refresh_fleet_fuel_actuals.delay') as delay:
            self.assertEqual(self.api.post('/api/v1/fleet/fuel-actuals/refresh/').status_code, 429)
            later = NOW + timedelta(minutes=15, seconds=1)
            with patch('django.utils.timezone.now', return_value=later):
                self.assertEqual(self.api.post('/api/v1/fleet/fuel-actuals/refresh/').status_code, 202)
        delay.assert_called_once()

    def test_burn_mode_needs_an_object_body(self):
        url = f'/api/v1/fleet/fuel-actuals/vehicle-types/{self.vt.id}/burn-mode/'
        for body in ([1], 'MEASURED', 7):
            r = self.api.post(url, body, format='json')
            self.assertEqual(r.status_code, 400, body)
            self.assertEqual(r.json()['code'], 'invalid_body')

    def test_refresh_needs_a_tracker(self):
        self.company.cartrack_connected_at = None
        self.company.save()
        r = self.api.post('/api/v1/fleet/fuel-actuals/refresh/')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'No fleet tracker connected.')


class SnapshotAndSuggestionTests(_Base):
    measured = PricingTests.measured

    def setUp(self):
        super().setUp()
        from core.tests.quote_rules_fixtures import add_vehicle
        add_vehicle(self.company, self.vt)
        self.customer = Customer.objects.create(company=self.company, name='Acme', email='a@x.test', phone='',
                                                address='', city='', state='', zip_code='')

    def create_quote(self):
        from core.models import Quote
        p = {'customer': self.customer.id, 'pickup_location': 'Johannesburg', 'delivery_location': 'Durban',
             'origin': 'JHB', 'destination': 'DBN', 'cargo_description': 'Steel', 'weight': '28000',
             'distance': '568.4', 'vehicle_type': 'Superlink', 'estimated_duration_minutes': 440,
             'base_rate': '20000', 'fuel_surcharge': '6500', 'toll_charges': '1043.48', 'driver_allowance': '0',
             'total_amount': '36000', 'valid_until': str(date(2026, 11, 7))}
        r = self.api.post('/api/v1/quotes/', p, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return Quote.objects.get(id=r.json()['id'])

    def test_snapshot_records_the_burn_and_reopen_explains_a_remeasure(self):
        row = self.measured(rated=40.2)
        q = self.create_quote()
        snap = q.costing_snapshot['rated_burn']
        self.assertEqual(snap['value'], 40.2)
        self.assertEqual(snap['source'], 'measured')
        self.assertEqual(snap['label'], 'Measured by Cartrack: 40,2 L/100 km over 18 400 km (90 days)')
        self.assertEqual(snap['measured_at'], timezone.localtime(row.computed_at).isoformat())
        self.assertNotIn('rejections', str(q.costing_snapshot['resolution']['rated_burn']))
        # Weekly refresh re-measures the type lower.
        row.rated_burn_l_per_100km = 38.9
        row.save()
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'quote_id': q.id}, format='json').json()
        csp = body['changes_since_priced']
        self.assertTrue(csp['changed'])
        self.assertLess(csp['delta_zar'], 0)
        self.assertEqual(csp['fuel_use_change']['text'], 'Fuel use updated from Cartrack (40,2 → 38,9 L/100 km).')
        self.assertTrue(csp['notice'].endswith('Fuel use updated from Cartrack (40,2 → 38,9 L/100 km).'))
        self.assertTrue(csp['notice'].startswith('Costs down R '))
        self.assertEqual(body['snapshot']['rated_burn']['value'], 40.2)

    def test_reopen_without_burn_change_has_no_fuel_use_note(self):
        self.measured(rated=40.2)
        q = self.create_quote()
        body = self.api.post('/api/v1/quotes/cost-breakdown/', {'quote_id': q.id}, format='json').json()
        self.assertIsNone(body['changes_since_priced']['fuel_use_change'])

    def test_burn_change_wording(self):
        m = {'value': 40.2, 'source': 'measured'}
        c = {'value': 42.0, 'source': 'configured'}
        self.assertEqual(qc.burn_change(c, m)['text'], 'Fuel use now measured by Cartrack (42,0 → 40,2 L/100 km).')
        self.assertEqual(qc.burn_change(m, c)['text'], 'Fuel use now from your figure (40,2 → 42,0 L/100 km).')
        self.assertEqual(qc.burn_change(c, {'value': 40.0, 'source': 'configured'})['text'],
                         'Truck fuel use changed (42,0 → 40,0 L/100 km).')
        self.assertIsNone(qc.burn_change(m, {'value': 40.2, 'source': 'measured'}))
        self.assertIsNone(qc.burn_change(None, m))

    def test_suggestion_tie_break_uses_burn_in_use(self):
        from core.tests.quote_rules_fixtures import add_vehicle
        other = VehicleType.objects.create(company=self.company, name='Interlink', capacity=34, max_distance=3000,
                                           base_rate=20, fuel_consumption_l_per_100km=40)
        add_vehicle(self.company, other)
        self.assertEqual(qc.suggest_vehicle(self.company, 28000).id, other.id)      # typed 40 < 42
        self.measured(rated=36.5)                                                     # Superlink measured 36,5
        self.assertEqual(qc.suggest_vehicle(self.company, 28000).id, self.vt.id)


class StackIntegrationTests(_Base):
    """Measured burn through the rest of the stack: tonnage quotes (cost per
    tonne on the burn in use, per truck) and trip economics (the job keeps the
    burn it was costed on; the fuel cost group says which)."""
    measured = PricingTests.measured
    create_quote = SnapshotAndSuggestionTests.create_quote

    def setUp(self):
        super().setUp()
        from core.tests.quote_rules_fixtures import add_vehicle
        add_vehicle(self.company, self.vt)
        self.taut = VehicleType.objects.create(company=self.company, name='Tautliner', capacity=30, max_distance=3000,
                                               base_rate=20, fuel_consumption_l_per_100km=40)
        add_vehicle(self.company, self.taut)
        self.customer = Customer.objects.create(company=self.company, name='Acme', email='a@x.test', phone='',
                                                address='', city='', state='', zip_code='')

    def tonnage(self, **over):
        return qc.costing_for_payload({'pricing_basis': 'per_tonne', 'one_way_distance_km': 568.4,
                                       'duration_minutes': 440, 'toll_cost': 1043.48, 'cargo_description': 'Steel',
                                       'tonnes_per_load': 30, **over}, self.company)

    def test_cost_per_tonne_uses_each_trucks_burn_in_use(self):
        before = {r['vehicle_type_id']: r for r in self.tonnage()['tonnage']['trucks']}
        self.assertEqual(before[self.vt.id]['burn_source'], 'configured')
        self.measured(rated=36.0)
        out = self.tonnage()
        rows = {r['vehicle_type_id']: r for r in out['tonnage']['trucks']}
        sl, tl = rows[self.vt.id], rows[self.taut.id]
        self.assertEqual(sl['burn_source'], 'measured')
        self.assertEqual(sl['burn_label'], 'Measured by Cartrack: 36,0 L/100 km over 18 400 km (90 days)')
        self.assertEqual(tl['burn_source'], 'configured')
        self.assertLess(sl['cost_per_tonne'], before[self.vt.id]['cost_per_tonne'])
        self.assertEqual(tl['cost_per_tonne'], before[self.taut.id]['cost_per_tonne'])
        sl_input = next(t for t in out['inputs']['trucks'] if t['vehicle']['id'] == self.vt.id)
        self.assertEqual(sl_input['vehicle']['rated_burn_l_per_100km'], 36.0)
        # The basis truck's burn is the quote's.
        basis = out['tonnage']['basis_vehicle_type_id']
        self.assertEqual(out['resolution']['rated_burn']['source'], rows[basis]['burn_source'])

    def test_chosen_truck_pinned_to_configured_and_differs_warning(self):
        self.measured(rated=34.0)
        out = self.tonnage(vehicle_type_id=self.vt.id, use_configured_burn=True)
        self.assertEqual(out['resolution']['rated_burn']['source'], 'configured')
        self.assertEqual(out['resolution']['rated_burn']['chosen_by'], 'quote')
        self.assertIn('truck_burn_differs_measured', [w['code'] for w in out['warnings']])

    def test_job_keeps_the_burn_it_was_quoted_on(self):
        from core.models import Load
        from core.services import trip_economics as te
        self.measured(rated=40.2)
        q = self.create_quote()
        r = self.api.post(f'/api/v1/quotes/{q.id}/convert_to_load/', {}, format='json')
        self.assertIn(r.status_code, (200, 201), r.content)
        load = Load.objects.get(id=r.json()['id'])
        self.assertEqual(load.costing_snapshot['rated_burn']['value'], 40.2)
        self.assertEqual(load.costing_snapshot['rated_burn']['source'], 'measured')
        row = te.economics_rows(self.company, [load])[load.id]
        fuel = next(g for g in row['cost_groups'] if g['group'] == 'fuel')
        self.assertEqual(fuel['rated_burn']['label'], 'Measured by Cartrack: 40,2 L/100 km over 18 400 km (90 days)')
        self.assertEqual(fuel['basis'], 'estimate')

    def test_job_costed_from_its_own_data_records_the_burn(self):
        from core.services.trip_costing import computed_fields
        self.measured(rated=40.2)
        fields = computed_fields(qc.costing_for_payload(self.PAYLOAD, self.company), NOW)
        snap = fields['costing_snapshot']
        self.assertEqual(snap['rated_burn']['source'], 'measured')
        self.assertNotIn('rejections', str(snap['resolution']['rated_burn']))


class FuelClauseConsistencyTests(_Base):
    """Follow-ups fuel price clause: its litres (Quote.fuel_litres) come from the
    same costing as costing_snapshot.rated_burn, so the clause adjusts exactly
    the litres the quote was priced on (measured or typed figure), and a later
    re-measure never changes a quote's clause litres."""
    measured = PricingTests.measured
    create_quote = SnapshotAndSuggestionTests.create_quote

    def setUp(self):
        super().setUp()
        from core.tests.quote_rules_fixtures import add_vehicle
        add_vehicle(self.company, self.vt)
        self.customer = Customer.objects.create(company=self.company, name='Acme', email='a@x.test', phone='',
                                                address='', city='', state='', zip_code='')

    def litres_at(self, quote, burn):
        """compute() litres for the saved quote's inputs at a given rated burn."""
        c = qc.costing_for_quote(quote)
        return qc.compute({**c['inputs'], 'vehicle': {**c['inputs']['vehicle'], 'rated_burn_l_per_100km': burn}})[
            'litres']['total']

    def test_clause_litres_are_the_litres_of_the_burn_priced_on(self):
        from core.services.fuel_surcharge import _terms_from_snapshot
        row = self.measured(rated=40.2)
        q = self.create_quote()
        snap = q.costing_snapshot['rated_burn']
        self.assertEqual((snap['source'], round(snap['value'], 1)), ('measured', 40.2))
        self.assertAlmostEqual(float(q.fuel_litres), self.litres_at(q, snap['value']), places=2)
        self.assertNotAlmostEqual(float(q.fuel_litres), self.litres_at(q, 42.0), places=0)
        terms = _terms_from_snapshot(q, 5)
        if terms is not None:                              # official price on record
            self.assertAlmostEqual(terms['litres'], float(q.fuel_litres), places=3)
        # The weekly refresh re-measures: the saved quote keeps its litres and burn.
        row.rated_burn_l_per_100km = 36.0
        row.save()
        q.refresh_from_db()
        self.assertEqual(round(q.costing_snapshot['rated_burn']['value'], 1), 40.2)
        self.assertAlmostEqual(float(q.fuel_litres), self.litres_at(q, 40.2), places=2)

    def test_quote_on_my_figure_clause_uses_the_typed_burn_litres(self):
        self.measured(rated=40.2)
        q = self.create_quote()
        q.costing_inputs = {**(q.costing_inputs or {}), 'use_configured_burn': True}
        q.save(update_fields=['costing_inputs'])
        from core.services.quote_snapshot import snapshot_fields
        for k, v in snapshot_fields(qc.costing_for_quote(q), timezone.now()).items():
            setattr(q, k, v)
        q.save()
        self.assertEqual(q.costing_snapshot['rated_burn']['source'], 'configured')
        self.assertAlmostEqual(float(q.fuel_litres), self.litres_at(q, 42.0), places=2)


class RefreshRobustnessTests(_Base):
    """Verifier round: real trip speeds, partial outages, tracker errors."""

    def jhb_dbn_trips(self, v, n=10, kmh=76.0, hours=7.5):
        cust = Customer.objects.create(company=self.company, name='Acme', email='a@x.test')
        du = User.objects.create_user(username=f'drv{v.id}', password='x')
        drv = Driver.objects.create(company=self.company, user=du, license_number=f'D{v.id}',
                                    license_expiry=date(2028, 1, 1), license_state='GP', hire_date=date(2020, 1, 1))
        start = ffa.period_bounds(NOW)[0] + timedelta(days=1)
        segs = []
        for i in range(n):
            a = start + timedelta(days=4 * i, hours=5)
            b = a + timedelta(hours=hours)
            ld = Load.objects.create(
                company=self.company, load_number=f'J{v.id}-{i}', customer=cust, pickup_location='JHB',
                pickup_city='JHB', pickup_state='GP', pickup_zip='1', pickup_date=a, delivery_location='DBN',
                delivery_city='DBN', delivery_state='KZN', delivery_zip='2', delivery_date=b,
                cargo_description='Steel', weight=Decimal('34000'), rate=Decimal('30000'),
                total_amount=Decimal('30000'), status='DELIVERED')
            Trip.objects.create(load=ld, vehicle=v, driver=drv, origin='JHB', destination='DBN',
                                estimated_distance_km=Decimal('570'), estimated_duration_hours=Decimal('7.5'),
                                start_time=a, end_time=b, status='COMPLETED')
            segs.append((a, b, 1.0, kmh))
        return segs

    def test_speed_cap_by_window_length(self):
        a = NOW
        self.assertGreater(ffa.max_plausible_km(a, a + timedelta(hours=7.5)), 570)      # JHB-DBN at 76 km/h
        self.assertLess(ffa.max_plausible_km(a, a + timedelta(hours=2)), 570)           # 285 km/h: no
        self.assertEqual(ffa.max_plausible_km(a, a + timedelta(days=30)), 30 * ffa.MAX_KM_PER_DAY)
        w = ffa.assess_window(a, a + timedelta(hours=7.5), {'distance': 570_000},
                              {'fuel_consumed': 240}, 'can_bus', min_km=ffa.MIN_TRIP_KM)
        self.assertIsNone(w['reject'])

    def test_ten_jhb_dbn_trips_are_accepted(self):
        v = self.truck('CJ100GP')
        segs = self.jhb_dbn_trips(v)
        self.fake.add('CJ100GP', rated=42.0, km_per_day=150, segments=segs, idle_ratio=0.0)
        self.refresh()
        row = FleetFuelMeasurement.objects.get(scope='VEHICLE', vehicle=v)
        self.assertNotIn('distance_implausible', [r['reason'] for r in row.rejections])
        self.assertAlmostEqual(row.loaded_km, 10 * 570, delta=5)
        self.assertAlmostEqual(row.loaded_l_per_100km, 42.0, delta=0.2)    # full loads burn the rated figure

    def _good_then(self, failing):
        self.truck('CK100GP')
        self.truck('CK200GP')
        self.fake.add('CK100GP', rated=40.0, idle_ratio=0.5)
        self.fake.add('CK200GP', rated=40.0, idle_ratio=0.5)
        self.refresh()
        before = self.type_row()
        self.assertTrue(ffa.usable(before, self.company, NOW)[0])
        truck_before = FleetFuelMeasurement.objects.get(scope='VEHICLE', vehicle__plate='CK100GP')
        with failing():
            run = self.refresh()
        return before, truck_before, run

    def test_all_500_refresh_keeps_the_good_figure(self):
        from core.integrations.cartrack import CartrackAPIError
        err = CartrackAPIError('Cartrack GET /vehicles/CK100GP/odometer failed: 500')

        def failing():
            from contextlib import ExitStack
            st = ExitStack()
            st.enter_context(patch.object(self.fake, 'get_odometer', side_effect=err))
            st.enter_context(patch.object(self.fake, 'get_fuel_consumed', side_effect=err))
            return st
        before, truck_before, run = self._good_then(failing)
        self.assertEqual(run.status, 'partial')
        self.assertEqual(run.message, "Cartrack didn't answer for 2 trucks; their last measured figures are kept.")
        self.assertNotIn('500', run.message)
        after = self.type_row()
        self.assertEqual(after.rated_burn_l_per_100km, before.rated_burn_l_per_100km)
        self.assertEqual(after.computed_at, before.computed_at)
        self.assertTrue(ffa.usable(after, self.company, NOW)[0])
        truck_after = FleetFuelMeasurement.objects.get(scope='VEHICLE', vehicle__plate='CK100GP')
        self.assertEqual((truck_after.distance_km, truck_after.computed_at),
                         (truck_before.distance_km, truck_before.computed_at))
        # Pricing still uses it.
        self.assertEqual(qc.resolve_rated_burn(self.company, self.vt)['source'], 'measured')

    def test_timeouts_and_bad_json_are_tracker_errors(self):
        import requests

        def failing():
            from contextlib import ExitStack
            st = ExitStack()
            st.enter_context(patch.object(self.fake, 'get_odometer', side_effect=requests.Timeout('slow')))
            st.enter_context(patch.object(self.fake, 'get_fuel_consumed',
                                          side_effect=ValueError('Expecting value: line 1 column 1')))
            return st
        before, _, run = self._good_then(failing)
        self.assertEqual(run.status, 'partial')
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(self.type_row().rated_burn_l_per_100km, before.rated_burn_l_per_100km)

    def test_roster_failure_is_plain_words_and_finished(self):
        import requests
        with patch.object(self.fake, 'get_vehicles', side_effect=requests.ConnectionError('reset by peer')):
            run = self.refresh()
        self.assertEqual((run.status, run.message), ('failed', "Cartrack didn't answer. Your last measured figures are kept."))
        self.assertIsNotNone(run.finished_at)
        self.assertIn('reset by peer', run.summary['error'])

    def test_crash_mid_loop_finishes_the_run_and_keeps_rows(self):
        self.truck('CK100GP')
        self.fake.add('CK100GP', rated=40.0, idle_ratio=0.5)
        self.refresh()
        before = self.type_row()
        with patch.object(ffa, 'measure_vehicle', side_effect=RuntimeError('boom')):
            run = self.refresh()
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIsNotNone(run.finished_at)
        self.assertNotIn('boom', run.message)
        self.assertEqual(self.type_row().computed_at, before.computed_at)


def _http(status=200, body=b''):
    import requests
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.headers['Content-Type'] = 'application/json'
    return r


class EmptyAnswerAndRaceTests(_Base):
    """Re-verifier round: 200s with no reading, load-window-only outages, and
    two simultaneous "Refresh now" presses (adapted from the verifier's probes)."""
    jhb_dbn_trips = RefreshRobustnessTests.jhb_dbn_trips

    def snap(self):
        return sorted(FleetFuelMeasurement.objects.values_list(
            'scope', 'vehicle_id', 'vehicle_type_id', 'rated_burn_l_per_100km', 'distance_km', 'sufficient',
            'confidence', 'computed_at'), key=lambda r: (r[0], r[1] or 0, r[2] or 0))

    def good(self):
        for p in ('PX1GP', 'PX2GP'):
            self.truck(p)
            self.fake.add(p, rated=40.0, idle_ratio=0.5)
        self.assertEqual(self.refresh().status, 'ok')
        self.assertTrue(ffa.usable(self.type_row(), self.company, NOW)[0])
        return self.snap()

    def real_client(self, answer, roster_ok=True):
        """The real CartrackClient: /vehicles answers the fake roster, every
        other call answers `answer` (a 200 with the given body)."""
        import json
        from core.integrations.cartrack import CartrackClient
        c = CartrackClient('u', 'p', 'https://fleetapi-za.cartrack.test')
        roster = {'data': self.fake.get_vehicles()}

        def req(method, url, **kw):
            if roster_ok and url.endswith('/vehicles'):
                return _http(200, json.dumps(roster).encode())
            return answer()
        return c, req

    def test_empty_200_bodies_keep_every_row(self):
        before = self.good()
        for body in (b'', b'{"data": null}', b'[]', b'{}'):
            c, req = self.real_client(lambda b=body: _http(200, b))
            with patch.object(c._session, 'request', side_effect=req):
                run = ffa.refresh_company(self.company, client=c, now=NOW)
            self.assertEqual(run.status, 'partial', body)
            self.assertEqual(self.snap(), before, body)
            self.assertNotIn('no_odometer', str(FleetFuelMeasurement.objects.values_list('rejections', flat=True)))
            self.assertEqual(qc.resolve_rated_burn(self.company, self.vt)['source'], 'measured')

    def test_empty_vehicle_list_is_a_failed_run_and_keeps_everything(self):
        from core.integrations.cartrack import CartrackClient
        before = self.good()
        for body in (b'{"data": []}', b'[]', b'{"data": null}', b''):
            c = CartrackClient('u', 'p', 'https://fleetapi-za.cartrack.test')
            with patch.object(c._session, 'request', return_value=_http(200, body)):
                run = ffa.refresh_company(self.company, client=c, now=NOW)
            run.refresh_from_db()
            self.assertEqual((run.status, run.message),
                             ('failed', 'Cartrack sent no trucks. Your last measured figures are kept.'), body)
            self.assertIsNotNone(run.finished_at)
            self.assertEqual(self.snap(), before, body)
            self.assertNotEqual(self.type_row().note, 'No tracked trucks of this type in the last refresh.')
            self.assertEqual(qc.resolve_rated_burn(self.company, self.vt)['source'], 'measured')

    def test_load_window_failures_keep_the_loaded_figure(self):
        import requests
        v = self.truck('PT1GP')
        segs = self.jhb_dbn_trips(v, n=6)
        self.fake.add('PT1GP', rated=42.0, km_per_day=150, segments=segs, idle_ratio=0.0)
        self.refresh()
        before = self.snap()
        self.assertEqual(FleetFuelMeasurement.objects.get(scope='VEHICLE', vehicle=v).rated_method, 'loaded_trips')
        orig = self.fake.get_odometer

        def odo(reg, a, b):
            if (b - a) < timedelta(days=1):           # only the load windows fail
                raise requests.ConnectionError('flap')
            return orig(reg, a, b)
        with patch.object(self.fake, 'get_odometer', side_effect=odo):
            run = self.refresh()
        self.assertEqual(run.status, 'partial')
        self.assertEqual(self.snap(), before)
        self.assertEqual(FleetFuelMeasurement.objects.get(scope='VEHICLE', vehicle=v).rated_method, 'loaded_trips')
        self.assertEqual(self.type_row().rated_method, 'loaded_trips')

    def test_double_press_race_queues_once(self):
        """Two presses that both read 'no lock' before either claims it."""
        from core import views_fleet_fuel as vf
        url = '/api/v1/fleet/fuel-actuals/refresh/'
        real = vf.refresh_state
        calls = {'n': 0}

        def state(company, now=None):
            calls['n'] += 1
            return {'queued': False, 'next_at': None, 'next_at_dt': None} if calls['n'] <= 2 else real(company, now)
        with patch('core.tasks.refresh_fleet_fuel_actuals.delay') as d, \
                patch.object(vf, 'refresh_state', side_effect=state):
            a = self.api.post(url).status_code
            b = self.api.post(url).status_code
        self.assertEqual(sorted((a, b)), [202, 429])
        self.assertEqual(d.call_count, 1)
