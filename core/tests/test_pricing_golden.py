"""Golden regression lock for the existing quote cost engines.

Owner's rule: the fuel, toll, cross-border, distance and QuoteBuilder
auto-calculation engines must not change. These tests pin their CURRENT
outputs (captured from main, API :8001, commit 4138d6b, on 2026-10-06) for ten
real lanes, so any change to those numbers fails loudly.

What is pinned
  * POST /api/v1/route/calculate/ — the WHOLE response for each scenario
    (distance, duration, fuel litres/cost, SANRAL class, every toll plaza and
    its excl./incl. VAT tariff, toll totals, cross-border fees/permit/
    weighbridge/foreign tolls and their line items, totals, every alternative
    route) against the response main's API actually returned. TomTom is mocked
    with the very route main received (rebuilt in TomTom's wire format), so
    _route/_parse_route run too. No network is touched.
  * GET /api/v1/fuel-prices/current/ — price, zone price, grade, effective date.
  * calculate_tolls_by_geometry for every SANRAL class on every captured route,
    resolve_toll_class for every shared vehicle type, and a cross-border cost
    matrix (weights, crossing counts, vehicle capacity).
  * quote_ai_pricing.compute_pricing (the builder's market check: fuel /
    tolls / driver allowance + nights away / base rate / combinations) on the
    builder's numbers for each scenario, with a fixed date and injected
    benchmark/allowance.
Reference data (toll plazas, border fees, vehicle types, the October 2026
FIASA row, company settings) is loaded from the captured fixtures, replacing
whatever migrations seeded, so the result never depends on seed drift.

The route/calculate and fuel expectations come straight from main's API.
The derived expectations (fixtures/pricing_golden/derived.json) were generated
from the same unchanged engine code by running this module once with
PRICING_GOLDEN_WRITE=1. Only regenerate when an engine change is APPROVED.

Run:
  REDIS_URL=redis://127.0.0.1:6379/15 python manage.py test core.tests.test_pricing_golden
"""
import gzip
import json
import os
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import BorderCrossingFee, Company, TollPlaza, User, VehicleType
from core.models.country_transit_rate import CountryTransitRate
from core.models.fuel_price import FuelPrice

from core.services.quote_costing import cents as qc_cents  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'pricing_golden')
ROUTES_DIR = os.path.join(FIXTURES, 'routes')
DERIVED_PATH = os.path.join(FIXTURES, 'derived.json')
WRITE = os.environ.get('PRICING_GOLDEN_WRITE') == '1'
# The day main's outputs were captured on: date-dependent rules (fuel period,
# SANRAL schedule from 1 March, allowance validity) are evaluated on it.
CAPTURE_DAY = date(2026, 10, 6)
CAPTURE_NOW = datetime(2026, 10, 6, 10, 0, tzinfo=dt_timezone.utc)

# QUOTE-RULES.md (7 Oct 2026) deliberately changed the route's FUEL figures:
# the company's own vehicle type, burn = rated × (0.70 + 0.30 × load ratio),
# the company's diesel price, cents rounding; unknown => null. Those keys are
# checked against that formula (expected_route_fuel) instead of main's
# capture; every other key is still pinned byte for byte.
ROUTE_FUEL_KEYS = ('fuel_usage_litres', 'fuel_cost_zar', 'fuel_rate_l_per_100km', 'total_cost_zar')
ROUTE_NEW_KEYS = ('distance_estimated', 'fuel_unknown_reason', 'fuel_vehicle_type_id', 'fuel_price_per_litre',
                  'fuel_price_source', 'tolls_unknown',
                  # Additive (8 Oct 2026): which border costs are not on file.
                  'border_costs_complete', 'border_costs_unknown',
                  # Additive (toll/border audit, 8 Oct 2026): the day tolls were priced for,
                  # the VAT basis, the tariff-year check and how much of the border is estimated.
                  'toll_trip_date', 'toll_vat_registered', 'toll_schedule_warning',
                  'border_costs_verified', 'border_estimate_zar', 'border_vehicle_profile')
ROUTE_OPTION_NEW_KEYS = ('toll_routes', 'toll_plazas', 'toll_summary', 'return_leg', 'return_leg_reason',
                         # each option's own border lines (the best option's equal the top level)
                         'cross_border', 'countries', 'additional_costs', 'cross_border_breakdown',
                         'border_costs_complete', 'border_costs_unknown', 'border_costs_verified',
                         'border_estimate_zar', 'border_vehicle_profile')

# Toll/border audit (8 Oct 2026, docs: TOLL-BORDER-AUDIT.md), APPROVED changes:
#  * each toll_breakdown entry gains plaza_type / operator / country /
#    tariff_effective_from, and the list is in DRIVING order (it was route
#    code, then km) — the plazas and amounts themselves are unchanged here,
#    because these goldens load main's own 31-plaza reference table;
#  * border, permit and foreign-road charges come from the per-component
#    schedule (core/services/border_schedule.py): each line has a code,
#    source, as-of date and verified flag; no weighbridge fees; Mozambique's
#    flat country toll is gone (TRAC/REVIMO plazas are TollPlaza rows, not in
#    this reference table); C-BRTA class by gross mass, one permit per
#    country served. Only the (code, amount) of each line is pinned here;
#    the line texts are tested in test_border_schedule_2026.
#  * new response keys (VAT basis, tariff-year check, border estimate,
#    per-option toll plazas/summary) are additive and not pinned here.
# The expected values below were derived from those rules, not copied.
TOLL_KEYS_PINNED = ('plaza', 'route', 'location_km', 'tariff', 'tariff_excl_vat', 'tariff_incl_vat')
# Border lines since the per-component schedule (core/services/border_schedule.py,
# second audit pass): worked out by hand from the published tariffs, at the
# fallback exchange rates the tests run on (core/services/fx.FALLBACK, no
# live fetch in tests), for a Class 4 truck with no gross mass / axle layout
# on its type (so 56 000 kg / 3+2+2 assumed, every line an estimate), the
# reference company's 24 crossings a year.
USD = 16.6391          # fx.FALLBACK['USD']
BWP = 1.1675           # fx.FALLBACK['BWP']
PERMIT_C2 = round(9041 / 24, 2)                       # C-BRTA Class 2, one country
ZW_KM_S10 = 582.9                                     # km in Zimbabwe, measured off the captured route
AUDIT_2026_10 = {
    's04_jhb_gaborone_semi': [                        # Botswana P975 one way (54 501-56 500 kg band)
        ('bw_single_trip_permit', round(975 * BWP, 2)), ('sa_cbrta_permit', PERMIT_C2)],
    's05_jhb_maputo_semi': [                          # unverified Mozambique figure, unchanged
        ('mz_insurance_inspection', 473.29), ('sa_cbrta_permit', PERMIT_C2)],
    's10_jhb_harare_beitbridge_semi': [
        ('zw_border_access_toll', round(375 * USD, 2)),   # Zimborders "Abnormal" (GCM >= 56 000 kg)
        ('zw_clearing_agent', 2005.0),                    # agent estimate
        ('sa_cbrta_permit', PERMIT_C2),
        ('zw_transit_fee', round(10 * 6 * USD, 2)),       # US$10 per 100 km or part: 6 x 100 km
        ('zw_toll_gates', round(20 * 4 * USD, 2)),        # ~4 gates (582.9 / 145) x US$20
    ],
}


def audit_additional_costs(lines):
    border = round(sum(a for c, a in lines if not c.startswith(('zw_transit', 'zw_toll'))), 2)
    tolls = round(sum(a for c, a in lines if c.startswith(('zw_transit', 'zw_toll'))), 2)
    return {'border_fees': border, 'weighbridge_fees': 0, 'non_sa_tolls': tolls}


def _pinned(breakdown):
    return sorted(({k: b.get(k) for k in TOLL_KEYS_PINNED if k in b} for b in breakdown or []),
                  key=lambda b: (b.get('route') or '', b.get('plaza') or ''))


def apply_audit_2026_10(key, exp):
    """main's captured response, moved by the approved audit changes only."""
    lines = AUDIT_2026_10.get(key)
    if lines is None:
        return exp
    exp['additional_costs'] = audit_additional_costs(lines)
    exp['cross_border_breakdown'] = [{'code': c, 'amount': a} for c, a in lines]
    return exp


with open(os.path.join(FIXTURES, 'reference_data.json')) as _f:
    REF = json.load(_f)
SCENARIO_KEYS = sorted(n[:-len('.json.gz')] for n in os.listdir(ROUTES_DIR) if n.endswith('.json.gz'))


def load_route_fixture(key):
    with gzip.open(os.path.join(ROUTES_DIR, f'{key}.json.gz'), 'rt') as f:
        return json.load(f)


def _dec(v):
    return None if v is None else Decimal(str(v))


def _aware(s):
    if not s:
        return None
    dt = datetime.fromisoformat(str(s))
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def _jsonable(v):
    """Round-trip through JSON the way an API response would (dates -> ISO)."""
    return json.loads(json.dumps(v, default=lambda o: o.isoformat() if hasattr(o, 'isoformat') else str(o)))


def load_reference_data():
    """Replace the engines' reference tables with exactly what main had."""
    TollPlaza.objects.all().delete()
    BorderCrossingFee.objects.all().delete()
    CountryTransitRate.objects.all().delete()
    VehicleType.objects.all().delete()
    FuelPrice.objects.all().delete()
    for p in REF['toll_plazas']:
        TollPlaza.objects.create(
            name=p['name'], route=p['route'], direction=p['direction'], location_km=_dec(p['location_km']),
            tariff_class_2=_dec(p['tariff_class_2']), tariff_class_3=_dec(p['tariff_class_3']),
            tariff_class_4=_dec(p['tariff_class_4']), tariff_class_5=_dec(p['tariff_class_5']),
            tariff_year=p['tariff_year'], is_active=bool(p['is_active']), lat=_dec(p['lat']), lng=_dec(p['lng']),
            radius_meters=p['radius_meters'], tariff_effective_from=p['tariff_effective_from'],
            tariff_source_name=p['tariff_source_name'] or '', tariff_source_url=p['tariff_source_url'] or '',
            tariff_verified_at=p['tariff_verified_at'])
    for b in REF['border_crossing_fees']:
        BorderCrossingFee.objects.create(
            from_country=b['from_country'], to_country=b['to_country'], fee_zar=_dec(b['fee_zar']),
            notes=b['notes'] or '', is_active=bool(b['is_active']), max_weight_kg=b['max_weight_kg'],
            min_weight_kg=b['min_weight_kg'])
    for r in REF['country_transit_rates']:   # empty on main: the hardcoded fallbacks apply
        CountryTransitRate.objects.create(**{k: (_dec(v) if isinstance(v, float) else v) for k, v in r.items()})
    for v in REF['vehicle_types']:
        VehicleType.objects.create(
            id=v['id'], company=None, name=v['name'], description=v['description'] or '',
            capacity=_dec(v['capacity']), max_distance=_dec(v['max_distance']), base_rate=_dec(v['base_rate']),
            fuel_consumption_l_per_100km=_dec(v['fuel_consumption_l_per_100km']),
            fuel_consumption_sensitivity_pct=_dec(v['fuel_consumption_sensitivity_pct']),
            fuel_type=v['fuel_type'], sanral_toll_class=v['sanral_toll_class'], active=bool(v['active']))
    fp = REF['fuel_price']
    months = {date(2026, 10, 1), timezone.localdate().replace(day=1)}
    for month in months:
        # The captured October row, plus the same figures as "this month" when
        # the suite runs later, so fetch_fuel_prices() never scrapes.
        FuelPrice.objects.create(
            date=month, diesel_inland=_dec(fp['diesel_inland']), diesel_coastal=_dec(fp['diesel_coastal']),
            petrol_93=_dec(fp['petrol_93']), petrol_95=_dec(fp['petrol_95']), source=fp['source'],
            diesel_500ppm_inland=_dec(fp['diesel_500ppm_inland']),
            diesel_500ppm_coastal=_dec(fp['diesel_500ppm_coastal']), diesel_grade=fp['diesel_grade'],
            effective_from=_aware(fp['effective_from']), fetched_at=timezone.now())


def make_company(name='Golden Freight (fictional)'):
    c = REF['company']
    return Company.objects.create(
        company_name=name, fuel_zone=c['fuel_zone'], fuel_price_per_litre=_dec(c['fuel_price_per_litre']),
        default_base_rate_per_km=_dec(c['default_base_rate_per_km']),
        default_toll_rate_per_km=_dec(c['default_toll_rate_per_km']),
        allow_cross_border=c['allow_cross_border'],
        cross_border_crossings_per_year=c['cross_border_crossings_per_year'])


class FakeTomTom:
    """requests.get stand-in: answers calculateRoute with the captured routes."""

    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append(url)
        if '/routing/1/calculateRoute/' not in url:
            raise AssertionError(f'unexpected network call in golden test: {url}')
        resp = mock.Mock()
        resp.status_code = 200
        resp.json.return_value = json.loads(json.dumps(self.payload))
        return resp


def _no_network(*a, **k):
    raise AssertionError('fuel price scrape attempted in golden test')


class _GoldenBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_reference_data()
        cls.company = make_company()
        cls.user = User.objects.create_user(username='golden', email='golden@example.test', password='x')
        cls.user.company = cls.company
        cls.user.save()

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        p = mock.patch('core.services.fuel_price._fetch_live', side_effect=_no_network)
        p.start()
        self.addCleanup(p.stop)

    def route_calc(self, fx):
        fake = FakeTomTom(fx['tomtom_response'])
        # Evaluated on the capture day (the reference FIASA row is the price
        # in force then), with no fuel scrape possible.
        with mock.patch('core.views.http_requests.get', side_effect=fake), \
                mock.patch('core.services.fuel_price._fetch_from_fiasa', side_effect=_no_network), \
                mock.patch('django.utils.timezone.now', return_value=CAPTURE_NOW):
            resp = self.client.post('/api/v1/route/calculate/', fx['request'], format='json',
                                    HTTP_X_TW_QUOTE_RULES='1')
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        self.assertEqual(len(fake.calls), 1, 'exactly one routing call per calculation')
        return resp.json()


# ---------------------------------------------------------------------------
# 1. Route calculation — full response vs main's API
# ---------------------------------------------------------------------------

class RouteCalculateGoldenTests(_GoldenBase):
    """One test per captured lane (generated below)."""

    def check_scenario(self, key):
        fx = load_route_fixture(key)
        got = self.route_calc(fx)
        exp = fx['expected_response']
        geoms = {i: rt['geometry'] for i, rt in enumerate(got['routes'])}
        for i, rt in enumerate(got['routes']):
            rt['geometry_points'] = len(rt.pop('geometry'))
            # The geometry is echoed from TomTom unchanged.
            src = fx['tomtom_response']['routes'][i]['legs'][0]['points']
            self.assertEqual(geoms[i], [{'lat': p['latitude'], 'lon': p['longitude']} for p in src])

        exp = apply_audit_2026_10(key, json.loads(json.dumps(exp)))
        self.check_route_fuel(key, fx, got, exp)
        # The headline numbers first, so a failure names what moved.
        for k in ('source', 'distance_km', 'duration_minutes', 'toll_sanral_class', 'toll_class_source',
                  'toll_cost_zar', 'toll_cost_incl_vat_zar', 'toll_vat_zar', 'tolls_unavailable_reason',
                  'toll_routes', 'cross_border', 'countries', 'additional_costs', 'warnings'):
            self.assertEqual(got.get(k), exp.get(k), f'{key}: {k}')
        self.assertEqual(
            sorted((b['plaza'], b['route'], b['tariff_excl_vat'], b['tariff_incl_vat']) for b in got['toll_breakdown']),
            sorted((b['plaza'], b['route'], b['tariff_excl_vat'], b['tariff_incl_vat']) for b in exp['toll_breakdown']),
            f'{key}: toll plazas')
        if key in AUDIT_2026_10:
            got['cross_border_breakdown'] = [{'code': b['code'], 'amount': b['amount']}
                                             for b in got.get('cross_border_breakdown') or []]
        self.assertEqual(got.get('cross_border_breakdown'), exp.get('cross_border_breakdown'),
                         f'{key}: cross-border line items')
        for i, (g, e) in enumerate(zip(got['routes'], exp['routes'])):
            for k in ('distance_km', 'duration_minutes', 'toll_cost_zar', 'toll_breakdown',
                      'motorway_pct', 'road_type', 'terrain', 'country_codes'):
                if k == 'country_codes':   # built from a set: order is not part of the contract
                    self.assertEqual(sorted(g[k]), sorted(e[k]), f'{key}: routes[{i}].{k}')
                elif k == 'toll_breakdown':
                    self.assertEqual(_pinned(g[k]), _pinned(e[k]), f'{key}: routes[{i}].{k}')
                else:
                    self.assertEqual(g[k], e[k], f'{key}: routes[{i}].{k}')
        # And then everything else, byte for byte.
        for k in ROUTE_FUEL_KEYS + ROUTE_NEW_KEYS:
            got.pop(k, None)
            exp.pop(k, None)
        for g, e in zip(got['routes'], exp['routes']):
            for k in ROUTE_FUEL_KEYS + ROUTE_OPTION_NEW_KEYS + ('fuel_usage_litres', 'tolls_unknown'):
                g.pop(k, None)
                e.pop(k, None)
            g['country_codes'], e['country_codes'] = sorted(g['country_codes']), sorted(e['country_codes'])
            g['toll_breakdown'], e['toll_breakdown'] = _pinned(g['toll_breakdown']), _pinned(e['toll_breakdown'])
        got['toll_breakdown'], exp['toll_breakdown'] = _pinned(got['toll_breakdown']), _pinned(exp['toll_breakdown'])
        self.assertEqual(got, exp, f'{key}: full response')


def expected_route_fuel(fx, km):
    """(litres, fuel R) by QUOTE-RULES §4 for the request's vehicle type and
    weight on the reference diesel price, or (None, None)."""
    from core.services import quote_costing as qc
    req = fx['request']
    vt = VehicleType.objects.filter(name__iexact=req.get('vehicle_type') or '').first()
    if vt is None:
        return None, None
    raw = req.get('weight_kg') or req.get('weight')
    load = float(raw) if raw not in (None, '') else None
    cap = qc.capacity_tonnes(vt.capacity)
    ratio = min((load / 1000) / cap, 1) if (cap and load is not None) else 1
    burn = float(vt.fuel_consumption_l_per_100km) * (0.70 + 0.30 * ratio)
    litres = km * burn / 100
    return litres, qc.cents(litres * float(REF['fuel_price']['diesel_inland']))


def _check_route_fuel(self, key, fx, got, exp):
    litres, fuel = expected_route_fuel(fx, got['distance_km'])
    self.assertEqual(got['fuel_cost_zar'], fuel, f'{key}: fuel_cost_zar')
    self.assertEqual(got['fuel_usage_litres'], round(litres, 2) if litres is not None else None, key)
    self.assertEqual(got['tolls_unknown'], got['tolls_unavailable_reason'] is not None, key)
    extras = sum((got.get('additional_costs') or {}).values())
    if fuel is not None and got['toll_cost_zar'] is not None:
        self.assertEqual(got['total_cost_zar'], round(fuel + got['toll_cost_zar'] + extras, 2), key)
    else:
        self.assertIsNone(got['total_cost_zar'], key)
    for i, rt in enumerate(got['routes']):
        r_litres, r_fuel = expected_route_fuel(fx, rt['distance_km'])
        self.assertEqual(rt['fuel_cost_zar'], r_fuel, f'{key}: routes[{i}].fuel_cost_zar')


RouteCalculateGoldenTests.check_route_fuel = _check_route_fuel


def _make_route_test(key):
    def test(self):
        self.check_scenario(key)
    test.__name__ = f'test_{key}'
    return test


for _key in SCENARIO_KEYS:
    setattr(RouteCalculateGoldenTests, f'test_{_key}', _make_route_test(_key))


# ---------------------------------------------------------------------------
# 2. Fuel price retrieval
# ---------------------------------------------------------------------------

class FuelPriceGoldenTests(_GoldenBase):
    def test_current_fuel_price_endpoint(self):
        with mock.patch('django.utils.timezone.now', return_value=CAPTURE_NOW):
            resp = self.client.get('/api/v1/fuel-prices/current/')
        self.assertEqual(resp.status_code, 200)
        got = resp.json()
        for k, v in REF['fuel_current_expected'].items():
            self.assertEqual(got.get(k), v, k)

    def test_route_calc_prices_fuel_at_the_inland_diesel_price(self):
        fx = load_route_fixture('s01_jhb_dbn_semi28')
        got = self.route_calc(fx)
        litres, fuel = expected_route_fuel(fx, got['distance_km'])
        self.assertEqual(got['fuel_cost_zar'], qc_cents(litres * 29.5551))
        self.assertEqual(got['fuel_price_per_litre'], 29.5551)
        self.assertEqual(got['fuel_price_source'], 'official')


# ---------------------------------------------------------------------------
# 3. Derived goldens (generated once from the unchanged engines)
# ---------------------------------------------------------------------------

def _route_geometry(fx, i=0):
    return [{'lat': p['latitude'], 'lon': p['longitude']}
            for p in fx['tomtom_response']['routes'][i]['legs'][0]['points']]


def _country_sections(fx, i=0):
    return [{'type': s['sectionType'], 'start': s['startPointIndex'], 'end': s['endPointIndex'],
             'country_code': s.get('countryCode')}
            for s in fx['tomtom_response']['routes'][i]['sections'] if s['sectionType'] == 'COUNTRY']


def builder_numbers(fx, vts, company):
    """QuoteBuilder.tsx's one-way cost lines for this scenario (see the
    frontend golden script scripts/golden-quote-calcs.mjs, which pins the UI
    formula itself). Used here only as the input payload of the market check."""
    import math
    scn, resp = fx['scenario'], fx['expected_response']
    jr = lambda x: math.floor(x + 0.5)
    by_name = {v['name']: v for v in vts}
    vt = by_name.get(scn['vehicle_type'])
    weight = scn['weight_t']
    if vt is None:   # cold start: the type that burns least at this weight
        rated = [(v, float(v['capacity'])) for v in vts]
        can = [x for x in rated if x[1] >= weight]
        burn = lambda x: float(x[0]['fuel_consumption_l_per_100km']) * (
            1 + float(x[0]['fuel_consumption_sensitivity_pct']) / 100) ** (weight - x[1])
        basis = sorted(can, key=burn)[0][0] if can else sorted(rated, key=lambda x: -x[1])[0][0]
    else:
        basis = vt
    cap = float(basis['capacity'])
    cons = float(basis['fuel_consumption_l_per_100km']) * (
        1 + float(basis['fuel_consumption_sensitivity_pct']) / 100) ** (weight - cap)
    ppl = float(REF['fuel_current_expected']['zone_price'])
    dist = resp['routes'][0]['distance_km']
    rate = float(vt['base_rate']) if vt else float(company['default_base_rate_per_km'])
    ac = resp.get('additional_costs') or {}
    return {
        'distance_km': dist, 'one_way_distance_km': dist, 'legs': 1, 'trip_type': 'ONE_WAY',
        'duration_minutes': resp['routes'][0]['duration_minutes'],
        'origin': scn['origin'], 'destination': scn['destination'], 'vehicle_type': scn['vehicle_type'],
        'weight': weight * 1000, 'fuel_cost': jr(dist * cons * ppl / 100),
        'fuel_usage_litres': dist * cons / 100, 'fuel_price_used': ppl, 'fuel_consumption_l_per_100km': cons,
        'fuel_type': 'Diesel', 'fuel_zone': 'INLAND', 'toll_cost': jr(resp['routes'][0]['toll_cost_zar']),
        'driver_cost': 0, 'cross_border_cost': round(sum(ac.values()), 2) if ac else 0,
        'base_rate_per_km': rate,
        'route': {'toll_breakdown': resp['routes'][0]['toll_breakdown'],
                  'country_codes': resp['routes'][0]['country_codes'], 'cross_border': bool(resp.get('cross_border'))},
    }


# Fictional figures, used only to drive the arithmetic of the market check.
TEST_ALLOWANCE = {'id': 1, 'rate_per_night': 650.0, 'allowance_type': 'nbcrfli',
                  'label': 'NBCRFLI driver allowance', 'effective_from': date(2026, 3, 1),
                  'verified_at': date(2026, 7, 1), 'source_url': 'https://example.test/allowance',
                  'source_name': 'Golden test allowance'}


def compute_derived():
    from core.services import cross_border as cb
    from core.services import quote_ai_pricing as qap
    from core.services.toll_calculator import calculate_tolls_by_geometry, resolve_toll_class

    company = Company.objects.get(company_name='Golden Freight (fictional)')
    out = {'toll_class_matrix': {}, 'cross_border_matrix': {}, 'toll_class_resolution': {}, 'market_check': {}}
    vts = REF['vehicle_types']
    for v in vts + [{'name': n} for n in ('', 'Flatbed', 'Box Truck', 'Superlink', '6x4 Rigid', 'Bakkie')]:
        r = resolve_toll_class(v['name'], company)
        out['toll_class_resolution'][v['name'] or '(blank)'] = [r.sanral_class, r.truck_type, r.source]
    for key in SCENARIO_KEYS:
        fx = load_route_fixture(key)
        geom = _route_geometry(fx)
        row = {}
        for tt in ('light', 'medium', 'heavy', 'combination'):
            res = calculate_tolls_by_geometry(geom, tt)
            row[tt] = {'plazas': [[b.plaza_name, b.route, str(b.tariff), str(b.tariff_excl_vat)] for b in res.breakdown],
                       'total_incl_vat': str(res.total_zar), 'total_excl_vat': str(res.total_excl_vat),
                       'routes_used': res.routes_used}
        out['toll_class_matrix'][key] = row
        resp = fx['expected_response']
        if resp.get('cross_border'):
            km = cb.country_distances_km(geom, _country_sections(fx))
            cases = {}
            for wt in (8000, 15000, 20000, 20001, 28000, 34000):
                for n in (None, 6, 24, 100):
                    for capkg in (0, 28000):
                        r = cb.calculate_cross_border_costs(resp['countries'], resp['distance_km'], 'Semi-Trailer Truck',
                                                            weight_kg=wt, crossings_per_year=n, country_km=km,
                                                            vehicle_capacity_kg=capkg)
                        cases[f'w{wt}_n{n}_cap{capkg}'] = r
            # Without measured kilometres (the fallback split).
            cases['no_measured_km_w28000'] = cb.calculate_cross_border_costs(
                resp['countries'], resp['distance_km'], weight_kg=28000)
            out['cross_border_matrix'][key] = {'country_km': km, 'cases': cases}
        payload = builder_numbers(fx, vts, REF['company'])
        mc = {}
        for label, allowance, bench in (
                ('as_on_main', None, {'rate': None, 'source': 'none'}),
                ('with_allowance_and_benchmark', TEST_ALLOWANCE,
                 {'rate': round(payload['fuel_cost'] + payload['toll_cost'] + payload['cross_border_cost']
                                + payload['distance_km'] * 25.0), 'source': 'platform'})):
            res = qap.compute_pricing(payload, CAPTURE_DAY, benchmark=bench, allowance=allowance, company=company)
            mc[label] = {'payload': payload, 'result': res}
        # A round trip of the same lane (legs doubles driving time, tolls, fuel).
        rt_payload = dict(payload, legs=2, trip_type='ROUND_TRIP', distance_km=payload['distance_km'] * 2,
                          fuel_cost=payload['fuel_cost'] * 2, fuel_usage_litres=payload['fuel_usage_litres'] * 2,
                          toll_cost=payload['toll_cost'] * 2, cross_border_cost=payload['cross_border_cost'] * 2)
        mc['round_trip_with_allowance'] = {'payload': rt_payload, 'result': qap.compute_pricing(
            rt_payload, CAPTURE_DAY, benchmark={'rate': None, 'source': 'none'}, allowance=TEST_ALLOWANCE,
            company=company)}
        out['market_check'][key] = mc
    return _jsonable(out)


class DerivedEngineGoldenTests(_GoldenBase):
    maxDiff = None

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.derived = compute_derived()
        if WRITE:
            with open(DERIVED_PATH, 'w') as f:
                json.dump(cls.derived, f, indent=1, ensure_ascii=False, sort_keys=True)
        with open(DERIVED_PATH) as f:
            cls.expected = json.load(f)

    def _section(self, name):
        got, exp = self.derived[name], self.expected[name]
        self.assertEqual(sorted(got), sorted(exp), f'{name}: keys')
        for k in exp:
            self.assertEqual(got[k], exp[k], f'{name}[{k}]')

    def test_toll_class_resolution(self):
        self._section('toll_class_resolution')

    def test_toll_plazas_and_amounts_for_every_sanral_class(self):
        self._section('toll_class_matrix')

    def test_cross_border_fees_permits_weighbridge_and_foreign_tolls(self):
        self._section('cross_border_matrix')

    def test_market_check_fuel_tolls_driver_nights_base_rate_and_combinations(self):
        self._section('market_check')

    def test_class_4_matrix_matches_the_route_endpoint(self):
        # The class-4 row of the matrix is what /route/calculate charged.
        for key in SCENARIO_KEYS:
            fx = load_route_fixture(key)
            if fx['expected_response']['toll_sanral_class'] != 4:
                continue
            row = self.expected['toll_class_matrix'][key]['combination']
            self.assertEqual(Decimal(row['total_excl_vat']),
                             Decimal(str(fx['expected_response']['toll_cost_zar'])).quantize(Decimal('0.01')), key)
