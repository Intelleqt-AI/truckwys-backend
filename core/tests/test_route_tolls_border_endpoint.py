"""/route/calculate/ and /quotes/cost-breakdown/ after the second toll/border
audit pass: route countries from real crossings, VAT basis by company,
no-geometry tolls are unknown, the way back on its own route, route options
with their own plazas and totals, the tariff-year check.

Routes are real TomTom truck routes (fixtures/toll_routes_2026.json); TomTom
itself is mocked.
"""
import json
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company

ROUTES = json.loads((Path(__file__).parent / 'fixtures' / 'toll_routes_2026.json').read_text())


def tomtom(name, *, sections=True):
    r = ROUTES[name]
    geom = [{'lat': a, 'lon': b} for a, b in r['geometry']]
    secs = ([{'type': 'COUNTRY', **s} for s in r.get('country_sections') or []] if sections else [])
    return {'distance_km': r['km'], 'duration_min': 400.0, 'duration_minutes': 400,
            'traffic_delay_minutes': 0, 'no_traffic_minutes': 400, 'historic_minutes': 400,
            'live_minutes': 400, 'departure_time': None, 'arrival_time': None,
            'sections': secs, 'geometry': geom}


class _Base(TestCase):
    vat_registered = True

    def setUp(self):
        self.company = Company.objects.create(company_name='Route Co', vat_registered=self.vat_registered)
        user = get_user_model().objects.create_user(username='routeco', email='r@example.com', password='x')
        user.company = self.company
        user.save()
        self.client = APIClient()
        self.client.force_authenticate(user=user)
        fuel = mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=RuntimeError('no feed'))
        fuel.start()
        self.addCleanup(fuel.stop)

    def calc(self, routes, *, dest=None, dest_country='ZA', side_effect=None, **extra):
        first = routes[0]
        o = first['geometry'][0] if first['geometry'] else {'lat': -26.2041, 'lon': 28.0473}
        d = dest or first['geometry'][-1]
        patch = (mock.patch('core.views.RouteCalculatorView._route', side_effect=side_effect) if side_effect
                 else mock.patch('core.views.RouteCalculatorView._route', return_value=routes))
        with patch as m:
            resp = self.client.post('/api/v1/route/calculate/', {
                'origin': 'A', 'destination': 'B', 'origin_lat': o['lat'], 'origin_lon': o['lon'],
                'origin_country': 'ZA', 'dest_lat': d['lat'], 'dest_lon': d['lon'], 'dest_country': dest_country,
                'vehicle_type': 'Interlink', 'weight_kg': 30000, **extra,
            }, format='json', HTTP_X_TW_QUOTE_RULES='1')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.route_calls = m.call_count
        return resp.json()


class PhantomCrossingTests(_Base):
    def test_delivery_at_the_sa_beitbridge_post_is_domestic(self):
        # TomTom's route runs ~3.7 km over the bridge into Zimbabwe for an
        # address on the SA side (-22.2235, 29.99). The geocoder says ZA.
        data = self.calc([tomtom('JHB-BEITBRIDGE-POST')], dest={'lat': -22.2235, 'lon': 29.99})
        self.assertFalse(data.get('cross_border', False))
        self.assertNotIn('additional_costs', data)

    def test_delivery_in_zimbabwe_is_cross_border(self):
        data = self.calc([tomtom('JHB-BEITBRIDGE-POST')], dest={'lat': -22.2235, 'lon': 29.99}, dest_country='ZW')
        self.assertEqual(data['countries'], ['SA', 'ZW'])
        self.assertIn('border_costs_verified', data)
        self.assertGreater(data['border_estimate_zar'], 0)   # the clearing agent at least


class VatBasisTests(_Base):
    def test_vat_vendor_costs_tolls_excl_vat(self):
        data = self.calc([tomtom('JHB-CPT')], trip_date='2026-10-08')
        self.assertEqual(data['toll_cost_incl_vat_zar'], 732.0)
        self.assertEqual(data['toll_cost_zar'], round(sum(round(t / 1.15, 2) for t in (126, 275, 331)), 2))
        self.assertFalse(data['toll_cost_includes_vat'])


class NonVendorVatBasisTests(_Base):
    vat_registered = False

    def test_non_vendor_costs_tolls_incl_vat(self):
        # Not VAT registered: the VAT on a toll slip cannot be reclaimed.
        data = self.calc([tomtom('JHB-CPT')], trip_date='2026-10-08')
        self.assertEqual(data['toll_cost_zar'], 732.0)
        self.assertTrue(data['toll_cost_includes_vat'])
        self.assertEqual(sum(b['tariff'] for b in data['toll_breakdown']), 732.0)


class NoGeometryTests(_Base):
    def test_no_geometry_means_tolls_unknown_not_a_guess(self):
        route = tomtom('JHB-CPT')
        route['geometry'] = []
        data = self.calc([route], dest={'lat': -33.92, 'lon': 18.42})
        self.assertTrue(data['tolls_unknown'])
        self.assertEqual(data['tolls_unavailable_reason'], 'no_geometry')
        self.assertIsNone(data['toll_cost_zar'])


class RouteOptionTests(_Base):
    def test_each_option_has_its_own_plazas_and_total(self):
        opts = [tomtom('JHB-DBN-OPTS'), tomtom('JHB-DBN-OPTS-ALT1'), tomtom('JHB-DBN-OPTS-ALT2')]
        data = self.calc(opts, trip_date='2026-10-08')
        r = data['routes']
        self.assertEqual(r[0]['toll_plazas'], ['Gosforth Ramp (W)', 'Wilge', 'Tugela', 'Mooi'])
        self.assertEqual(r[0]['toll_cost_incl_vat_zar'], 1020.0)       # 33 + 304 + 359 + 324
        self.assertTrue(r[0]['toll_summary'].startswith('Fastest · via N17/N3'))
        self.assertEqual(r[2]['toll_plazas'], ['Mariannhill'])
        self.assertEqual(r[2]['toll_cost_incl_vat_zar'], 57.0)
        self.assertIn('Alternative 2', r[2]['toll_summary'])
        # The default (selected) route is TomTom's fastest; its plazas are the toll line.
        self.assertEqual([b['plaza'] for b in data['toll_breakdown']], r[0]['toll_plazas'])


class ReturnLegTests(_Base):
    def test_the_way_back_is_priced_on_its_own_route(self):
        out, back = tomtom('PTA-LEBOMBO'), tomtom('LEBOMBO-PTA')
        data = self.calc([out], side_effect=[[out], [back]], include_return=True, trip_date='2026-10-08')
        self.assertEqual(self.route_calls, 2)
        ret = data['return_leg']
        self.assertTrue(ret['available'])
        self.assertEqual(ret['toll_plazas'], ['Nkomazi', 'Machadodorp', 'Middelburg', 'Diamond Hill'])
        self.assertEqual(ret['toll_cost_incl_vat_zar'], 1719.0)

    def test_no_second_routing_call_unless_asked(self):
        self.calc([tomtom('PTA-LEBOMBO')])
        self.assertEqual(self.route_calls, 1)

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

    def test_short_one_way_does_not_route_home(self):
        # Clients send include_return on every one-way calculation; a 46 km
        # trip is under the 300 km empty-return minimum, so no second call.
        data = self.calc([tomtom('RAMP-HAMMANSKRAAL-PTA')], include_return=True)
        self.assertEqual(self.route_calls, 1)
        self.assertIsNone(data['return_leg'])
        self.assertEqual(data['return_leg_reason'], 'below_empty_return_min_km')

    def test_company_default_off_does_not_route_home(self):
        self.company.include_empty_return_default = False
        self.company.save()
        data = self.calc([tomtom('PTA-LEBOMBO')], include_return=True)
        self.assertEqual((self.route_calls, data['return_leg'], data['return_leg_reason']),
                         (1, None, 'empty_return_default_off'))

    def test_user_toggle_and_round_trip_always_route_home(self):
        short = tomtom('RAMP-HAMMANSKRAAL-PTA')
        data = self.calc([short], side_effect=[[short], [short]], include_empty_return=True)
        self.assertEqual((self.route_calls, data['return_leg_reason']), (2, 'requested'))
        data = self.calc([short], side_effect=[[short], [short]], trip_type='ROUND_TRIP', dest={'lat': -25.75, 'lon': 28.2})
        self.assertEqual(data['return_leg_reason'], 'round_trip')
        self.assertTrue(data['return_leg']['available'])

    def test_user_toggle_off_means_a_return_load(self):
        data = self.calc([tomtom('PTA-LEBOMBO')], include_empty_return=False)
        self.assertEqual((self.route_calls, data['return_leg'], data['return_leg_reason']),
                         (1, None, 'return_load_booked'))

    def test_identical_way_back_is_cached(self):
        out, back = tomtom('PTA-LEBOMBO'), tomtom('LEBOMBO-PTA')
        self.calc([out], side_effect=[[out], [back]], include_return=True)
        self.assertEqual(self.route_calls, 2)
        data = self.calc([out], side_effect=[[out]], include_return=True)
        self.assertEqual(self.route_calls, 1)          # the way back came from the cache
        self.assertEqual(data['return_leg']['toll_cost_incl_vat_zar'], 1719.0)


class TariffYearTests(_Base):
    def test_trip_after_the_published_schedule_warns(self):
        data = self.calc([tomtom('JHB-CPT')], pickup_date='2027-03-02')
        w = data['toll_schedule_warning']
        self.assertEqual(w['code'], 'toll_tariffs_not_published')
        self.assertEqual(w['schedule_ends'], '2027-02-28')

    def test_trip_inside_the_schedule_does_not(self):
        self.assertIsNone(self.calc([tomtom('JHB-CPT')], pickup_date='2027-02-28')['toll_schedule_warning'])

    def test_cost_breakdown_warns_too(self):
        resp = self.client.post('/api/v1/quotes/cost-breakdown/', {
            'trip_type': 'ONE_WAY', 'distance_km': 1400, 'toll_cost_one_way': 636.52,
            'pickup_date': '2027-03-10'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertIn('toll_tariffs_not_published', [w['code'] for w in resp.json()['warnings']])


class CostingReturnLegTests(TestCase):
    def test_round_trip_uses_the_way_backs_own_tolls(self):
        from core.services.quote_costing import compute
        out = compute({'trip_type': 'ROUND_TRIP', 'distance_km': 425,
                       'tolls': {'one_way': 1494.78, 'return_leg': 1177.39}})
        tolls = next(ln for ln in out['lines'] if ln['key'] == 'tolls')
        self.assertEqual(tolls['amount'], 2672.17)

    def test_empty_return_border_uses_the_return_figure(self):
        from core.services.quote_costing import compute
        out = compute({'trip_type': 'ONE_WAY', 'distance_km': 900, 'international': True, 'border_cost': 5000,
                       'border_cost_empty_return': 1200, 'include_empty_return': True,
                       'tolls': {'one_way': 0}})
        back = next(ln for ln in out['lines'] if ln['key'] == 'border_return')
        self.assertEqual(back['amount'], 1200.0)


class WeighbridgeFeeNotEditableTests(TestCase):
    def test_admin_cannot_set_a_weighbridge_fee(self):
        from decimal import Decimal
        from core.models import CountryTransitRate
        admin = get_user_model().objects.create_superuser(username='su', email='su@example.com', password='x')
        client = APIClient()
        client.force_authenticate(user=admin)
        resp = client.post('/api/v1/admin/transit-rates/', {'country_code': 'AO', 'country_name': 'Angola',
                                                             'weighbridge_fee_zar': 300}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content[:200])
        self.assertNotIn('weighbridge_fee_zar', resp.json())
        rid = resp.json()['id']
        client.patch(f'/api/v1/admin/transit-rates/{rid}/', {'weighbridge_fee_zar': 500}, format='json')
        self.assertEqual(CountryTransitRate.objects.get(id=rid).weighbridge_fee_zar, Decimal('0'))
