"""Toll class, VAT and fallback-flag tests for the route calculator.

Covers docs/backend-changes/2026-09-toll-class-vat.md:
  * SANRAL class comes from an explicit VehicleType.sanral_toll_class, with the
    name-based guess only as a fallback (H2).
  * The toll amount that enters a quote is VAT-exclusive (H4).
  * A straight-line (TomTom down) route or a failed toll calculation is
    flagged instead of silently returning R0 (H3).

The plazas and tariffs used here are the real 2026 rows seeded by migration
0070, and the vehicle types are the shared defaults seeded by 0109. TomTom is
never called: RouteCalculatorView._route is patched.
"""
import math
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, TollPlaza, VehicleType
from core.services import toll_calculator as tc

User = get_user_model()

JHB = (-26.2041, 28.0473)
DBN = (-29.8587, 31.0218)
CPT = (-33.9249, 18.4241)

# Waypoints through the seeded plaza coordinates (same as the pricing review's
# worked_routes.py), densified to 0.5 km so the geofence sees a road-like line.
JHB_DBN = [JHB, (-26.52, 28.35), (-26.66393, 28.38992), (-27.04049, 28.62624), (-28.25, 29.13),
           (-28.46233, 29.56156), (-29.21802, 30.00396), (-29.60, 30.38), (-29.82302, 30.80276), DBN]
JHB_CPT = [JHB, (-26.41711, 27.88075), (-26.85645, 27.6353), (-27.65, 27.23), (-28.79878, 26.69057),
           (-29.12, 26.21), (-30.72, 25.10), (-32.35, 22.58), (-33.20, 20.86), (-33.65, 19.44),
           (-33.74268, 19.01986), CPT]

# Published 2026 SANRAL/N3TC totals (VAT inclusive) by SANRAL class.
PUBLISHED = {
    'JHB-DBN': {1: Decimal('347.50'), 2: Decimal('632.00'), 3: Decimal('912.00'), 4: Decimal('1274.00')},
    'JHB-CPT': {1: Decimal('252.00'), 2: Decimal('562.00'), 3: Decimal('775.00'), 4: Decimal('1115.00')},
}


def densify(wps, step_km=0.5):
    out = []
    for a, b in zip(wps, wps[1:]):
        d = math.hypot((b[0] - a[0]) * 111, (b[1] - a[1]) * 111 * math.cos(math.radians(a[0])))
        n = max(1, int(d / step_km))
        for i in range(n):
            t = i / n
            out.append({'lat': a[0] + t * (b[0] - a[0]), 'lon': a[1] + t * (b[1] - a[1])})
    out.append({'lat': wps[-1][0], 'lon': wps[-1][1]})
    return out


def fake_route(wps, distance_km):
    return [{
        'distance_km': distance_km, 'duration_min': 360.0, 'duration_minutes': 360,
        'traffic_delay_minutes': 0, 'no_traffic_minutes': 360, 'historic_minutes': 360,
        'live_minutes': 360, 'departure_time': None, 'arrival_time': None,
        'sections': [], 'geometry': densify(wps),
    }]


def excl(amount):
    return (Decimal(amount) / Decimal('1.15')).quantize(Decimal('0.01'))


class _RouteCalcBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(company_name='Toll Co')
        self.user = User.objects.create_user(username='tolluser', email='toll@example.com', password='x')
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)
        fuel = mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=RuntimeError('no fuel feed in tests'))
        fuel.start()
        self.addCleanup(fuel.stop)

    def calc(self, vehicle_type, wps=JHB_DBN, distance_km=568.0, routes=None):
        routes = fake_route(wps, distance_km) if routes is None else routes
        with mock.patch('core.views.RouteCalculatorView._route', return_value=routes):
            resp = self.client.post('/api/v1/route/calculate/', {
                'origin': 'Johannesburg', 'destination': 'Durban',
                'origin_lat': wps[0][0], 'origin_lon': wps[0][1], 'origin_country': 'ZA',
                'dest_lat': wps[-1][0], 'dest_lon': wps[-1][1], 'dest_country': 'ZA',
                'vehicle_type': vehicle_type, 'weight_kg': 8000,
            }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()


# ---------------------------------------------------------------------------
# H2 — SANRAL class
# ---------------------------------------------------------------------------

class SanralClassDefinitionTests(TestCase):
    def test_class_from_axles_follows_sanral_2026_definitions(self):
        # SANRAL 2026: Class 2 = 2-axle heavy, Class 3 = 3 & 4 axle heavy,
        # Class 4 = more than 4 axles.
        self.assertEqual(tc.sanral_class_for_axles(2), 2)
        self.assertEqual(tc.sanral_class_for_axles(3), 3)
        self.assertEqual(tc.sanral_class_for_axles(4), 3)
        self.assertEqual(tc.sanral_class_for_axles(5), 4)
        self.assertEqual(tc.sanral_class_for_axles(7), 4)

    def test_name_fallback_reads_axle_configuration(self):
        # A 4-axle combination is Class 3, an 8x4 rigid is Class 3, a 4x2 is Class 2.
        self.assertEqual(tc.resolve_toll_class_from_name('4x2 Flatbed').sanral_class, 2)
        self.assertEqual(tc.resolve_toll_class_from_name('8×4 Tipper').sanral_class, 3)
        self.assertEqual(tc.resolve_toll_class_from_name('6x4 rigid').sanral_class, 3)
        self.assertEqual(tc.resolve_toll_class_from_name('2-axle rigid').sanral_class, 2)
        # Combination words win over an axle count: "6x4 horse" describes the
        # tractor only, not the whole combination.
        self.assertEqual(tc.resolve_toll_class_from_name('6x4 Horse & Trailer').sanral_class, 4)
        # Light-vehicle words win over a wheel formula: a 4x4 bakkie is Class 1.
        self.assertEqual(tc.resolve_toll_class_from_name('4x4 Bakkie').sanral_class, 1)
        self.assertEqual(tc.resolve_toll_class_from_name('Interlink (34 tonnes)').sanral_class, 4)

    def test_name_fallback_unknown_defaults_to_class_4_and_says_so(self):
        res = tc.resolve_toll_class_from_name('Mystery Machine')
        self.assertEqual(res.sanral_class, 4)
        self.assertEqual(res.source, 'default')

    def test_unchanged_name_fallbacks(self):
        # Names with no axle information keep their previous class.
        for name, cls in [('Tautliner', 4), ('Flatbed Truck', 4), ('Tanker', 4),
                          ('Heavy Truck (8–16 tonnes)', 3), ('Box Truck', 2),
                          ('Medium Truck (4–8 tonnes)', 2), ('Light Delivery Vehicle (LDV)', 1),
                          ('Semi-Trailer Truck', 4)]:
            with self.subTest(name=name):
                self.assertEqual(tc.resolve_toll_class_from_name(name).sanral_class, cls)


class SeededVehicleTypeClassTests(TestCase):
    """Migration 0127 sets the class on the shared defaults whose seeded
    description states their axle configuration, and leaves the rest NULL."""

    EXPECTED = {
        'Light Delivery Vehicle (LDV)': 1,
        'Box Truck': 2,
        'Medium Truck (4–8 tonnes)': 2,
        'Rigid Truck': 2,
        'Heavy Truck (8–16 tonnes)': 3,
        'Semi-Trailer Truck': 4,
        'Semi-Truck / Horse & Trailer (30 tonnes)': 4,
        'Interlink (34 tonnes)': 4,
        'Tanker': 4,
        # Body types whose axle count the description does not state.
        'Refrigerated Truck (Reefer)': None,
        'Flatbed Truck': None,
        'Tautliner': None,
    }

    def test_shared_defaults(self):
        for name, cls in self.EXPECTED.items():
            with self.subTest(name=name):
                vt = VehicleType.objects.get(company__isnull=True, name=name)
                self.assertEqual(vt.sanral_toll_class, cls)


class RouteCalcTollClassTests(_RouteCalcBase):
    def test_rigid_truck_is_billed_class_2_not_class_3(self):
        # Reproduces H2: the seeded "Rigid Truck" is a 2-axle rigid.
        data = self.calc('Rigid Truck')
        # Pre-fix this was Class 3: R912 (and VAT-inclusive).
        self.assertEqual(Decimal(str(data['toll_cost_zar'])), excl(PUBLISHED['JHB-DBN'][2]))
        self.assertEqual(Decimal(str(data['toll_cost_incl_vat_zar'])), PUBLISHED['JHB-DBN'][2])
        self.assertEqual(data['toll_sanral_class'], 2)
        self.assertEqual(data['toll_class_source'], 'vehicle_type')

    def test_company_type_with_explicit_class_wins_over_name(self):
        # A company's own "Tautliner" that is an 8x4 rigid (4 axles) → Class 3,
        # not the Class 4 the name alone would give.
        VehicleType.objects.create(company=self.company, name='Tautliner', capacity=18, max_distance=2000,
                                   base_rate=20, sanral_toll_class=3)
        data = self.calc('Tautliner')
        self.assertEqual(Decimal(str(data['toll_cost_incl_vat_zar'])), PUBLISHED['JHB-DBN'][3])
        self.assertEqual(data['toll_class_source'], 'vehicle_type')

    def test_other_companys_class_is_not_used(self):
        other = Company.objects.create(company_name='Other Co')
        VehicleType.objects.create(company=other, name='My Rigid 4x2 Special', capacity=8, max_distance=2000,
                                   base_rate=20, sanral_toll_class=4)
        data = self.calc('My Rigid 4x2 Special')
        self.assertEqual(data['toll_sanral_class'], 2)   # from the name's 4x2, not the other tenant's row
        self.assertEqual(data['toll_class_source'], 'name_inferred')

    def test_interlink_unchanged_class_4(self):
        data = self.calc('Interlink (34 tonnes)')
        self.assertEqual(Decimal(str(data['toll_cost_incl_vat_zar'])), PUBLISHED['JHB-DBN'][4])
        self.assertEqual(data['toll_sanral_class'], 4)


# ---------------------------------------------------------------------------
# H4 — VAT
# ---------------------------------------------------------------------------

class TollVatTests(_RouteCalcBase):
    def test_excl_vat_rounding(self):
        self.assertEqual(tc.tariff_excl_vat(Decimal('304.00')), Decimal('264.35'))
        self.assertEqual(tc.tariff_excl_vat(Decimal('57.00')), Decimal('49.57'))
        self.assertEqual(tc.tariff_excl_vat(Decimal('230.00')), Decimal('200.00'))
        self.assertEqual(tc.tariff_excl_vat(Decimal('16.50')), Decimal('14.35'))  # 14.3478 → 14.35
        self.assertEqual(tc.tariff_excl_vat(Decimal('0')), Decimal('0.00'))

    def test_toll_cost_entering_quote_is_vat_exclusive(self):
        # Reproduces H4: R1,274 of VAT-inclusive tariffs must enter the
        # excl.-VAT quote as R1,107.83, so invoice VAT is charged once.
        data = self.calc('Interlink (34 tonnes)')
        self.assertEqual(Decimal(str(data['toll_cost_zar'])), Decimal('1107.83'))
        self.assertEqual(Decimal(str(data['toll_cost_incl_vat_zar'])), Decimal('1274.00'))
        self.assertEqual(Decimal(str(data['toll_vat_zar'])), Decimal('166.17'))
        self.assertIs(data['toll_cost_includes_vat'], False)
        self.assertEqual(data['toll_vat_rate'], 0.15)
        # Breakdown sums to the quoted toll line; the published tariff is kept alongside.
        self.assertEqual(sum(Decimal(str(b['tariff'])) for b in data['toll_breakdown']), Decimal('1107.83'))
        self.assertEqual(sum(Decimal(str(b['tariff_incl_vat'])) for b in data['toll_breakdown']), Decimal('1274.00'))
        # Route option row and totals use the same VAT-exclusive figure.
        self.assertEqual(Decimal(str(data['routes'][0]['toll_cost_zar'])), Decimal('1107.83'))
        self.assertAlmostEqual(data['total_cost_zar'], round(data['fuel_cost_zar'] + 1107.83, 2), places=2)

    def test_jhb_cpt_excl_vat(self):
        data = self.calc('Interlink (34 tonnes)', wps=JHB_CPT, distance_km=1398.0)
        self.assertEqual(Decimal(str(data['toll_cost_incl_vat_zar'])), PUBLISHED['JHB-CPT'][4])
        expected = sum(excl(b['tariff_incl_vat']) for b in data['toll_breakdown'])
        self.assertEqual(Decimal(str(data['toll_cost_zar'])), expected)

    def test_invoice_from_quote_total_charges_vat_once_on_tolls(self):
        """A load carries quote.total_amount as its subtotal; the invoice adds
        15% once. With VAT-exclusive tolls in that subtotal, the customer's
        VAT on the toll portion equals SANRAL's own VAT on the tariff."""
        from core.models import Customer, Load
        from core.services.invoicing import create_invoice_for_load
        from django.utils import timezone
        customer = Customer.objects.create(company=self.company, name='Cust', email='c@test.com',
                                           phone='082', address='x')
        tolls = tc.tariff_excl_vat(Decimal('1274.00'))
        now = timezone.now()
        load = Load.objects.create(
            company=self.company, load_number='LOAD-TOLL-1', customer=customer,
            pickup_location='JHB', pickup_city='JHB', pickup_state='GP', pickup_zip='2000',
            pickup_date=now, delivery_location='DBN', delivery_city='DBN', delivery_state='KZN',
            delivery_zip='4000', delivery_date=now, cargo_description='Freight',
            weight=Decimal('1000.00'), distance=Decimal('568.00'), rate=Decimal('0'),
            total_amount=tolls, status='ASSIGNED',
        )
        invoice, created = create_invoice_for_load(load)
        self.assertTrue(created)
        self.assertEqual(invoice.total_amount, Decimal('1274.00'))   # 1107.83 + 166.17


# ---------------------------------------------------------------------------
# H3 — fallback flags
# ---------------------------------------------------------------------------

class TollFallbackFlagTests(_RouteCalcBase):
    def test_tomtom_down_straight_line_is_flagged_not_silent_zero(self):
        # Reproduces H3: _route returns None → 2-point straight-line geometry.
        with mock.patch('core.views.RouteCalculatorView._route', return_value=None):
            resp = self.client.post('/api/v1/route/calculate/', {
                'origin': 'Johannesburg', 'destination': 'Durban',
                'origin_lat': JHB[0], 'origin_lon': JHB[1], 'origin_country': 'ZA',
                'dest_lat': DBN[0], 'dest_lon': DBN[1], 'dest_country': 'ZA',
                'vehicle_type': 'Interlink (34 tonnes)',
            }, format='json')
        data = resp.json()
        self.assertEqual(data['source'], 'estimated')
        self.assertEqual(data['toll_source'], 'estimated')
        self.assertIs(data['tolls_unavailable'], True)
        self.assertIs(data['tolls_estimated'], True)
        self.assertEqual(data['tolls_unavailable_reason'], 'routing_unavailable')
        self.assertTrue(data['toll_warning'])
        self.assertEqual(data['toll_cost_zar'], 0)
        self.assertIs(data['routes'][0]['tolls_unavailable'], True)

    def test_toll_calculator_exception_is_flagged(self):
        with mock.patch('core.services.toll_calculator.calculate_tolls_by_geometry', side_effect=RuntimeError('boom')):
            data = self.calc('Interlink (34 tonnes)')
        self.assertIs(data['tolls_unavailable'], True)
        self.assertEqual(data['tolls_unavailable_reason'], 'toll_calculation_failed')
        self.assertEqual(data['toll_cost_zar'], 0)

    def test_no_plazas_is_flagged(self):
        TollPlaza.objects.all().delete()
        data = self.calc('Interlink (34 tonnes)')
        self.assertIs(data['tolls_unavailable'], True)
        self.assertEqual(data['tolls_unavailable_reason'], 'no_toll_data')

    def test_normal_route_is_not_flagged(self):
        data = self.calc('Interlink (34 tonnes)')
        self.assertEqual(data['toll_source'], 'geofence')
        self.assertIs(data['tolls_unavailable'], False)
        self.assertIs(data['tolls_estimated'], False)
        self.assertIsNone(data['tolls_unavailable_reason'])
        self.assertIsNone(data['toll_warning'])

    def test_route_with_no_toll_plazas_is_a_real_zero_not_unavailable(self):
        # Pretoria ↔ Johannesburg on the N1/N14 has no seeded plazas: a
        # genuine R0, which must not be flagged as unavailable.
        wps = [(-25.7479, 28.2293), (-25.95, 28.15), (-26.2041, 28.0473)]
        data = self.calc('Interlink (34 tonnes)', wps=wps, distance_km=58.0)
        self.assertEqual(data['toll_cost_zar'], 0)
        self.assertIs(data['tolls_unavailable'], False)


# ---------------------------------------------------------------------------
# sanral_toll_class plumbing: API, copy-on-write, admin, migration rule
# ---------------------------------------------------------------------------

class SanralTollClassApiTests(_RouteCalcBase):
    def test_tenant_can_set_class_on_own_type_and_invalid_is_rejected(self):
        resp = self.client.post('/api/v1/vehicle-types/', {
            'name': 'Our 8x4 Tipper', 'capacity': 20, 'max_distance': 1000, 'base_rate': 20,
            'sanral_toll_class': 3}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['sanral_toll_class'], 3)
        bad = self.client.post('/api/v1/vehicle-types/', {
            'name': 'Bad', 'capacity': 5, 'max_distance': 100, 'base_rate': 10,
            'sanral_toll_class': 7}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_copy_on_write_clone_keeps_the_class(self):
        shared = VehicleType.objects.get(company__isnull=True, name='Rigid Truck')
        resp = self.client.patch(f'/api/v1/vehicle-types/{shared.id}/', {'base_rate': 19}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        clone = VehicleType.objects.get(company=self.company, name='Rigid Truck')
        self.assertEqual(clone.sanral_toll_class, 2)

    def test_superuser_admin_patch_validates_class(self):
        admin = User.objects.create_superuser(username='root', email='root@example.com', password='x')
        self.client.force_authenticate(user=admin)
        vt = VehicleType.objects.get(company__isnull=True, name='Tautliner')
        bad = self.client.patch(f'/api/v1/admin/vehicle-types/{vt.id}/', {'sanral_toll_class': 9}, format='json')
        self.assertEqual(bad.status_code, 400)
        ok = self.client.patch(f'/api/v1/admin/vehicle-types/{vt.id}/', {'sanral_toll_class': 4}, format='json')
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()['sanral_toll_class'], 4)
        cleared = self.client.patch(f'/api/v1/admin/vehicle-types/{vt.id}/', {'sanral_toll_class': None}, format='json')
        self.assertEqual(cleared.status_code, 200, cleared.content)
        vt.refresh_from_db()
        self.assertIsNone(vt.sanral_toll_class)


class MigrationRuleTests(TestCase):
    """The data step only sets rows that still carry the seeded payload and
    never overwrites an existing class."""

    def test_rule(self):
        import importlib
        from django.apps import apps
        mig = importlib.import_module('core.migrations.0129_vehicletype_sanral_toll_class')
        co = Company.objects.create(company_name='Mig Co')
        same = VehicleType.objects.create(company=co, name='Rigid Truck', capacity=Decimal('8'),
                                          max_distance=1, base_rate=1)
        edited = VehicleType.objects.create(company=Company.objects.create(company_name='Mig Co 2'),
                                            name='Rigid Truck', capacity=Decimal('14'), max_distance=1, base_rate=1)
        preset = VehicleType.objects.create(company=Company.objects.create(company_name='Mig Co 3'),
                                            name='Rigid Truck', capacity=Decimal('8'), max_distance=1, base_rate=1,
                                            sanral_toll_class=3)
        mig.set_known_classes(apps, None)
        for vt, expected in ((same, 2), (edited, None), (preset, 3)):
            vt.refresh_from_db()
            self.assertEqual(vt.sanral_toll_class, expected)
