"""Toll coverage: real SA routes against the published 2026/27 (and 2025/26)
tariffs, worked out by hand from the gazette poster — not from the seed data.

Route geometry: TomTom truck routes fetched 8 Oct 2026, trimmed to the points
within 2.5 km of a toll booth (plus both ends) to keep the fixture small.
Expected plazas are the booths each route actually drives through; expected
amounts are the poster tariffs (VAT inclusive) summed by hand:
https://www.nra.co.za/uploads/17/SANRAL%20Toll%20Tariff%202026%20A3%20Poster%20v2.pdf
GG 52072/52073 for 2025/26. Mozambique: TRAC / REVIMO MZN tariffs at R0.2548.
"""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from core.models import TollPlaza, TollTariff
from core.services.toll_calculator import calculate_tolls_by_geometry, parse_trip_date

FIXTURE = Path(__file__).parent / 'fixtures' / 'toll_routes_2026.json'
ROUTES = json.loads(FIXTURE.read_text())
D = Decimal
OCT_2026 = date(2026, 10, 8)
FEB_2026 = date(2026, 2, 15)   # still the 2025/26 schedule

# route: (plazas in driving order, SANRAL class 3 total, class 4 total) — 2026/27
EXPECTED_2026 = {
    # N3: TomTom's truck route joins the N17 at the Gosforth west ramp and
    # reaches the N3 south of De Hoek; it leaves the N3 before Mariannhill.
    # Gosforth Ramp (W) 25/33 + Wilge 215/304 + Tugela 260/359 + Mooi 240/324
    'JHB-DBN': (['Gosforth Ramp (W)', 'Wilge', 'Tugela', 'Mooi'], '740.00', '1020.00'),
    # N1 south: Grasmere 96/126 + Vaal 207/275 + Verkeerdevlei 236/331 (Huguenot not driven)
    'JHB-CPT': (['Grasmere', 'Vaal', 'Verkeerdevlei'], '539.00', '732.00'),
    'JHB-BFN': (['Grasmere', 'Vaal', 'Verkeerdevlei'], '539.00', '732.00'),
    'JHB-MASERU': (['Grasmere', 'Vaal'], '303.00', '401.00'),
    # N1 north: Pumulani 47/57 + Carousel 224/258 + Kranskop 210/257 + Nyl 180/241
    'JHB-PLK': (['Pumulani', 'Carousel', 'Kranskop', 'Nyl'], '661.00', '813.00'),
    # + Capricorn 205/256 + Baobab 231/278
    'JHB-BEITBRIDGE': (['Pumulani', 'Carousel', 'Kranskop', 'Nyl', 'Capricorn', 'Baobab'], '1097.00', '1347.00'),
    # N4 east (TRAC): Diamond Hill 133/220 + Middelburg 277/365 + Machadodorp 510/729 + Nkomazi 281/405
    'PTA-LEBOMBO': (['Diamond Hill', 'Middelburg', 'Machadodorp', 'Nkomazi'], '1201.00', '1719.00'),
    'PTA-MBOMBELA': (['Diamond Hill', 'Middelburg', 'Machadodorp'], '920.00', '1314.00'),
    # + Moamba MZN 1200/1800 + Maputo MZN 375/550 at R0.2548
    'JHB-MAPUTO': (['Middelburg', 'Machadodorp', 'Nkomazi', 'Moamba', 'Maputo'], '1469.31', '2097.78'),
    # N2 north: Othongathi 42/62 + Mvoti 70/104 + Mtunzini 146/217; its ramps are passed, not used
    'DBN-RICHARDS_BAY': (['Othongathi', 'Mvoti', 'Mtunzini'], '258.00', '383.00'),
    # + N200 MZN 700/1000 + Maputo–Katembe bridge MZN 750/1200
    'DBN-MAPUTO': (['Othongathi', 'Mvoti', 'Mtunzini', 'N200 Belavista', 'Ponte Maputo-Katembe'], '627.46', '943.56'),
    'CPT-GQEBERHA': (['Tsitsikamma'], '438.00', '619.00'),
    'JHB-MBABANE': (['Middelburg'], '277.00', '365.00'),
    'JHB-GABORONE': ([], '0.00', '0.00'),
    'CPT-WINDHOEK': ([], '0.00', '0.00'),
    'JHB-RUSTENBURG': ([], '0.00', '0.00'),     # TomTom's route is toll-free (R512)
    # Previously missing mainline plazas
    'RUSTENBURG-ZEERUST': (['Swartruggens'], '313.00', '368.00'),
    'PTA-HARTBEESPOORT': (['Quagga', 'Pelindaba'], '37.00', '48.00'),
    'RAMP-BRITS-RUSTENBURG': (['Marikana'], '81.00', '96.00'),
    # N17: Gosforth 50/69 + Dalpark 42/58 + Leandra 190/253 + Trichardt 96/127 + Ermelo 170/226;
    # Denne ramp is passed, not used
    'JHB-ERMELO': (['Gosforth', 'Dalpark', 'Leandra', 'Trichardt', 'Ermelo'], '548.00', '733.00'),
    'GERMISTON-SPRINGS': (['Dalpark'], '42.00', '58.00'),
    # South coast: Oribi mainline (R61) 100/162; Izotsha ramp passed, not used
    'DBN-PORT_EDWARD': (['Oribi'], '100.00', '162.00'),
    # Ramp plazas actually used
    'RAMP-ENNERDALE-VEREENIGING': (['Grasmere Ramp (S)'], '48.00', '63.00'),
    'RAMP-HAMMANSKRAAL-PTA': (['Hammanskraal', 'Pumulani'], '177.00', '207.00'),
    'RAMP-MOKOPANE-PLK': (['Sebetiela'], '58.00', '77.00'),
    'RAMP-PORT_SHEPSTONE-MARGATE': (['Oribi Ramp (S)'], '46.00', '73.00'),
    'RAMP-MODIMOLLE-PLK': (['Nyl'], '180.00', '241.00'),
    'RAMP-MOOI_RIVER-PMB': (['Mooi'], '240.00', '324.00'),
    'RAMP-BALLITO-KING_SHAKA': (['Othongathi'], '42.00', '62.00'),
}

# 2025/26 schedule (GG 52072/52073), same routes.
EXPECTED_2025 = {
    # Gosforth Ramp (W) 24/31 + Wilge 207/294 + Tugela 251/347 + Mooi 231/313
    'JHB-DBN': ('713.00', '985.00'),
    # Grasmere 92/122 + Vaal 200/267 + Verkeerdevlei 229/321
    'JHB-CPT': ('521.00', '710.00'),
    # Pumulani 46/55 + Carousel 216/249 + Kranskop 203/249 + Nyl 174/233
    'JHB-PLK': ('639.00', '786.00'),
    # Diamond Hill 128/213 + Middelburg 268/352 + Machadodorp 493/704 + Nkomazi 271/391
    'PTA-LEBOMBO': ('1160.00', '1660.00'),
    'RUSTENBURG-ZEERUST': ('302.00', '355.00'),
}


def _geom(name):
    return [{'lat': a, 'lon': b} for a, b in ROUTES[name]['geometry']]


class RouteTollCoverageTests(TestCase):
    """Runs against the plazas the migrations load (no fixtures of our own)."""

    def _run(self, name, truck_type, on):
        return calculate_tolls_by_geometry(_geom(name), truck_type, trip_date=on)

    def test_2026_routes_match_the_published_tariffs(self):
        for name, (plazas, c3, c4) in EXPECTED_2026.items():
            with self.subTest(route=name):
                heavy = self._run(name, 'heavy', OCT_2026)
                combo = self._run(name, 'combination', OCT_2026)
                self.assertEqual([b.plaza_name for b in combo.breakdown], plazas)
                self.assertEqual(heavy.total_zar, D(c3))
                self.assertEqual(combo.total_zar, D(c4))

    def test_trip_before_1_march_2026_uses_the_2025_tariffs(self):
        for name, (c3, c4) in EXPECTED_2025.items():
            with self.subTest(route=name):
                self.assertEqual(self._run(name, 'heavy', FEB_2026).total_zar, D(c3))
                self.assertEqual(self._run(name, 'combination', FEB_2026).total_zar, D(c4))

    def test_tariff_change_day_is_1_march(self):
        before = self._run('JHB-CPT', 'combination', date(2026, 2, 28)).total_zar
        on = self._run('JHB-CPT', 'combination', date(2026, 3, 1)).total_zar
        self.assertEqual((before, on), (D('710.00'), D('732.00')))

    def test_breakdown_carries_type_operator_and_effective_date(self):
        res = self._run('JHB-MAPUTO', 'combination', OCT_2026)
        by = {b.plaza_name: b for b in res.breakdown}
        self.assertEqual(by['Nkomazi'].operator, 'TRAC')
        self.assertEqual(by['Nkomazi'].tariff_effective_from, date(2026, 3, 1))
        self.assertEqual(by['Moamba'].country, 'MZ')
        ramp = self._run('RAMP-ENNERDALE-VEREENIGING', 'combination', OCT_2026).breakdown[0]
        self.assertEqual(ramp.plaza_type, 'ramp')

    def test_foreign_tolls_carry_no_reclaimable_sa_vat(self):
        res = self._run('JHB-MAPUTO', 'combination', OCT_2026)
        by = {b.plaza_name: b for b in res.breakdown}
        self.assertEqual(by['Moamba'].tariff_excl_vat, by['Moamba'].tariff)      # Mozambican IVA: not an SA input
        self.assertEqual(by['Nkomazi'].tariff_excl_vat, D('352.17'))             # R405 / 1.15

    def test_no_gauteng_e_toll_is_ever_charged(self):
        # e-tolls ended 11 April 2024; no GFIP gantry may be a plaza.
        self.assertFalse(TollPlaza.objects.filter(name__icontains='e-toll').exists())
        self.assertFalse(TollPlaza.objects.filter(route__in=['N12', 'R21', 'N14']).exists())


class PlazaTableCoverageTests(TestCase):
    """The table holds every plaza on the 2026/27 gazette poster."""

    POSTER_2026 = {
        # name: (class 1, 2, 3, 4) — a sample across every operator and type
        'Huguenot': ('54.50', '151.00', '236.00', '383.00'),
        'Grasmere Ramp (N)': ('14.00', '41.00', '48.00', '63.00'),
        'Hammanskraal': ('35.00', '120.00', '130.00', '150.00'),
        'Kranskop Ramp': ('17.00', '46.00', '54.00', '81.00'),
        'Tsitsikamma': ('73.00', '183.00', '438.00', '619.00'),
        'Mtunzini Ramp (S)': ('53.00', '99.00', '119.00', '172.00'),
        'Mooi Ramp (S)': ('49.00', '119.00', '168.00', '227.00'),
        'Tugela East': ('62.00', '102.00', '152.00', '211.00'),
        'Pelindaba': ('8.00', '15.00', '21.00', '27.00'),
        'Swartruggens': ('103.00', '258.00', '313.00', '368.00'),
        'K99': ('20.00', '50.00', '58.00', '70.00'),
        'Valtaki East': ('39.00', '55.00', '81.00', '183.00'),
        'Machadodorp': ('126.00', '350.00', '510.00', '729.00'),
        'Gosforth Ramp (E)': ('7.50', '29.00', '31.00', '42.00'),
        'Leandra Ramp': ('30.50', '77.00', '113.00', '152.00'),
        'Brandfort': ('62.50', '125.00', '188.00', '265.00'),
    }

    def test_every_poster_plaza_is_loaded(self):
        sa = TollPlaza.objects.filter(is_active=True, country='ZA')
        # 36 mainline rows on the poster (Tsitsikamma counted once) + 37 ramps
        self.assertEqual(sa.filter(plaza_type='mainline').count(), 36)
        self.assertEqual(sa.filter(plaza_type='ramp').count(), 37)

    def test_sample_tariffs_match_the_poster(self):
        for name, amounts in self.POSTER_2026.items():
            with self.subTest(plaza=name):
                p = TollPlaza.objects.get(name=name, country='ZA')
                got = tuple(str(p.get_tariff(c)) for c in (2, 3, 4, 5))
                self.assertEqual(got, amounts)
                self.assertEqual(p.tariff_effective_from, date(2026, 3, 1))

    def test_every_sa_plaza_has_2025_and_2026_history(self):
        for p in TollPlaza.objects.filter(is_active=True, country='ZA'):
            starts = set(TollTariff.objects.filter(plaza=p).values_list('effective_from', flat=True))
            self.assertEqual(starts, {date(2025, 3, 1), date(2026, 3, 1)}, p.name)

    def test_mainline_points_sit_on_the_mainline(self):
        # The three plazas once placed on a ramp booth now start on the motorway booth.
        self.assertEqual(TollPlaza.objects.get(name='Grasmere').lng, D('27.884137'))
        self.assertEqual(TollPlaza.objects.get(name='Gosforth').lng, D('28.158343'))
        self.assertEqual(TollPlaza.objects.get(name='Oribi').lat, D('-30.748327'))


class TripDateParsingTests(TestCase):
    def test_parses_dates_and_datetimes(self):
        self.assertEqual(parse_trip_date('2026-03-01'), date(2026, 3, 1))
        self.assertEqual(parse_trip_date('2026-03-01T08:00:00Z'), date(2026, 3, 1))
        self.assertEqual(parse_trip_date(date(2025, 4, 2)), date(2025, 4, 2))

    def test_garbage_or_missing_means_today(self):
        from django.utils import timezone
        self.assertEqual(parse_trip_date('next week'), timezone.localdate())
        self.assertEqual(parse_trip_date(None), timezone.localdate())


class RampMatchingRuleTests(TestCase):
    """The rules that keep ramp plazas from being charged to through traffic."""

    def setUp(self):
        TollPlaza.objects.all().delete()
        common = dict(direction='t', location_km=D('0'), tariff_year=2026)
        self.main = TollPlaza.objects.create(
            name='Main', route='N1', lat=D('-26.0000'), lng=D('28.0000'), radius_meters=150,
            plaza_group='Site', tariff_class_2=D('10'), tariff_class_3=D('20'), tariff_class_4=D('30'),
            tariff_class_5=D('100'), **common)
        # A ramp booth 20 m east of the mainline, with mainline points 1.2 km either side.
        self.ramp = TollPlaza.objects.create(
            name='Main Ramp', route='N1', lat=D('-26.0000'), lng=D('28.0002'), radius_meters=30,
            plaza_type='ramp', plaza_group='Site', through_points=[[-25.9892, 28.0], [-26.0108, 28.0]],
            tariff_class_2=D('1'), tariff_class_3=D('2'), tariff_class_4=D('3'), tariff_class_5=D('7'), **common)

    def test_through_route_pays_the_mainline_only(self):
        line = [{'lat': -25.98, 'lon': 28.0}, {'lat': -26.02, 'lon': 28.0}]
        res = calculate_tolls_by_geometry(line, 'combination')
        self.assertEqual([b.plaza_name for b in res.breakdown], ['Main'])

    def test_route_leaving_by_the_ramp_pays_the_ramp_only(self):
        # Comes down the mainline, then turns east through the ramp booth.
        line = [{'lat': -25.98, 'lon': 28.0}, {'lat': -25.9995, 'lon': 28.0}, {'lat': -26.0, 'lon': 28.0002},
                {'lat': -26.0, 'lon': 28.02}]
        res = calculate_tolls_by_geometry(line, 'combination')
        self.assertEqual([b.plaza_name for b in res.breakdown], ['Main Ramp'])
        self.assertEqual(res.total_zar, D('7'))


class RouteEndpointTripDateTests(TestCase):
    """/route/calculate/ prices tolls on the trip's own date (trip_date or pickup_date)."""

    def setUp(self):
        from unittest import mock
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from core.models import Company
        self.company = Company.objects.create(company_name='Toll Date Co')
        user = get_user_model().objects.create_user(username='tolldate', email='td@example.com', password='x')
        user.company = self.company
        user.save()
        self.client = APIClient()
        self.client.force_authenticate(user=user)
        fuel = mock.patch('core.services.fuel_price.fetch_fuel_prices', side_effect=RuntimeError('no feed'))
        fuel.start()
        self.addCleanup(fuel.stop)

    def _calc(self, **extra):
        from unittest import mock
        geom = _geom('JHB-CPT')
        route = [{'distance_km': 1555.0, 'duration_min': 1000.0, 'duration_minutes': 1000,
                  'traffic_delay_minutes': 0, 'no_traffic_minutes': 1000, 'historic_minutes': 1000,
                  'live_minutes': 1000, 'departure_time': None, 'arrival_time': None,
                  'sections': [], 'geometry': geom}]
        with mock.patch('core.views.RouteCalculatorView._route', return_value=route):
            resp = self.client.post('/api/v1/route/calculate/', {
                'origin': 'Johannesburg', 'destination': 'Cape Town',
                'origin_lat': geom[0]['lat'], 'origin_lon': geom[0]['lon'], 'origin_country': 'ZA',
                'dest_lat': geom[-1]['lat'], 'dest_lon': geom[-1]['lon'], 'dest_country': 'ZA',
                'vehicle_type': 'Interlink', 'weight_kg': 30000, **extra,
            }, format='json', HTTP_X_TW_QUOTE_RULES='1')
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.json()

    def test_pickup_date_before_1_march_uses_2025_tariffs(self):
        data = self._calc(pickup_date='2026-02-20')
        self.assertEqual(data['toll_trip_date'], '2026-02-20')
        self.assertEqual(data['toll_cost_incl_vat_zar'], 710.0)   # 122 + 267 + 321
        self.assertEqual({b['tariff_effective_from'] for b in data['toll_breakdown']}, {'2025-03-01'})

    def test_trip_date_in_2026_27_uses_the_current_tariffs(self):
        data = self._calc(trip_date='2026-11-02')
        self.assertEqual(data['toll_cost_incl_vat_zar'], 732.0)   # 126 + 275 + 331
        self.assertEqual([b['plaza'] for b in data['toll_breakdown']], ['Grasmere', 'Vaal', 'Verkeerdevlei'])
