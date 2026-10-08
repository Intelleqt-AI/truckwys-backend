"""Toll plaza reference data: every SANRAL / concession plaza (mainline AND
ramp) with its booth positions, plus the Mozambican plazas on the routes SA
fleets run to Maputo.

FROZEN DATA — migration 0161 loads it. Do not edit a figure in place; add the
next year's schedule as a new tariff date (and a new migration) instead.

Sources
-------
* 2026/27 tariffs, effective 1 March 2026: Government Gazettes 54087
  (concession roads: N3TC, Bakwena, TRAC) and 54088 (SANRAL-operated roads),
  published 5 Feb 2026. Explanatory poster:
  https://www.nra.co.za/uploads/17/SANRAL%20Toll%20Tariff%202026%20A3%20Poster%20v2.pdf
  Gazette pages: https://www.n3tc.co.za/wp-content/uploads/2026/02/Government_Gazette_2026-2027.pdf
* 2025/26 tariffs, effective 1 March 2025: GG 52072 (concessions) and
  GG 52073 (SANRAL roads), 7 Feb 2025:
  https://www.nra.co.za/uploads/17/20250207%20Government%20gazette%20Vol716%20No52072%207-2%20Transport%20-%20SANRAL%20Concessionairs%20Toll%20Tariffs%2020250301.pdf
  https://www.nra.co.za/uploads/17/20250207%20Goverment%20Gazette%20Vol716%20No52073%207-2%20Transport%20-%20SANRAL%20CTROM%20Toll%20Tariffs%2020250301_0.pdf
* Booth positions: OpenStreetMap barrier=toll_booth nodes (ids in
  ``osm_nodes``; © OpenStreetMap contributors, ODbL), read 8 Oct 2026, each
  checked against the way it sits on (motorway = mainline booth,
  motorway_link/trunk_link = ramp booth).
* ``through_points``: two points on the mainline about 1.2 km either side of a
  ramp, taken from a TomTom truck route that drives straight past it
  (8 Oct 2026). A route that passes both did not use the ramp.
* Mozambique: TRAC (https://tracn4.co.za/toll-plazas-toll-fees/) and REVIMO
  (https://www.revimo.co.mz/Tarifas.php) publish in meticais only. Converted at
  MZN_ZAR below (the rate migration 0112 priced Mozambique at, 16 Sep 2026).

Class columns are SANRAL classes 1–4 (light; 2 axles; 3–4 axles; 5+ axles).
All SA amounts VAT inclusive, as published.

Directional ramp names — which booth is "(N)" or "(S)" — follow the booths'
position relative to the mainline plaza (Wikipedia junction tables and the
N3TC page agree for Mooi). Where both directions cost the same (Grasmere,
Othongathi) it makes no difference; for Mtunzini, Oribi and Gosforth it is the
best reading of the gazette names and is flagged in docs/audit.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

D = Decimal
T2026 = date(2026, 3, 1)
T2025 = date(2025, 3, 1)

SOURCE_2026 = ('https://www.nra.co.za/uploads/17/SANRAL%20Toll%20Tariff%202026%20A3%20Poster%20v2.pdf',
               'SANRAL toll tariffs from 1 March 2026 (GG 54087 & 54088)')
SOURCE_2025 = ('https://www.nra.co.za/uploads/17/20250207%20Goverment%20Gazette%20Vol716%20No52073%207-2%20'
               'Transport%20-%20SANRAL%20CTROM%20Toll%20Tariffs%2020250301_0.pdf',
               'SANRAL toll tariffs from 1 March 2025 (GG 52072 & 52073)')

MAINLINE_RADIUS_M = 150   # mainline booths: the route drives through the plaza itself
RAMP_RADIUS_M = 30        # ramp booths sit 15–200 m off the mainline: tight on purpose

PLAZAS = [
    {
        'route': 'N1', 'name': 'Huguenot', 'plaza_type': 'mainline',
        'plaza_group': 'Huguenot', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions (Huguenot Tunnel)',
        'points': [[-33.742853, 19.019804], [-33.742681, 19.019858]],
        'osm_nodes': ['n60855113', 'n26924001'],
        'through_points': [],
        'tariffs': {
            T2026: (D('54.50'), D('151.00'), D('236.00'), D('383.00')),  # GG 54088
            T2025: (D('53.00'), D('146.00'), D('229.00'), D('371.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Vaal', 'plaza_type': 'mainline',
        'plaza_group': 'Vaal', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.856449, 27.635298], [-26.856387, 27.635086]],
        'osm_nodes': ['n30112889', 'n30112890'],
        'through_points': [],
        'tariffs': {
            T2026: (D('91.50'), D('172.00'), D('207.00'), D('275.00')),  # GG 54088
            T2025: (D('89.00'), D('167.00'), D('200.00'), D('267.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Grasmere', 'plaza_type': 'mainline',
        'plaza_group': 'Grasmere', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.411617, 27.884137], [-26.41151, 27.883949]],
        'osm_nodes': ['n670648219', 'n670648218'],
        'through_points': [],
        'tariffs': {
            T2026: (D('27.50'), D('82.00'), D('96.00'), D('126.00')),  # GG 54088
            T2025: (D('27.00'), D('80.00'), D('92.00'), D('122.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Grasmere Ramp (N)', 'plaza_type': 'ramp',
        'plaza_group': 'Grasmere', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'R558 ramps at the plaza',
        'points': [[-26.411762, 27.884367], [-26.411373, 27.883737]],
        'osm_nodes': ['n929296697', 'n929296654'],
        'through_points': [[-26.40158, 27.89162], [-26.42063, 27.87732]],
        'tariffs': {
            T2026: (D('14.00'), D('41.00'), D('48.00'), D('63.00')),  # GG 54088
            T2025: (D('14.00'), D('40.00'), D('46.00'), D('61.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Grasmere Ramp (S)', 'plaza_type': 'ramp',
        'plaza_group': 'Grasmere', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'R558 ramps south of the plaza',
        'points': [[-26.417108, 27.880746], [-26.41561, 27.879818]],
        'osm_nodes': ['n670648203', 'n670648216'],
        'through_points': [[-26.40579, 27.88868], [-26.42612, 27.87307]],
        'tariffs': {
            T2026: (D('14.00'), D('41.00'), D('48.00'), D('63.00')),  # GG 54088
            T2025: (D('14.00'), D('40.00'), D('46.00'), D('61.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Verkeerdevlei', 'plaza_type': 'mainline',
        'plaza_group': 'Verkeerdevlei', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-28.798781, 26.690572]],
        'osm_nodes': ['n267033752'],
        'through_points': [],
        'tariffs': {
            T2026: (D('78.50'), D('157.00'), D('236.00'), D('331.00')),  # GG 54088
            T2025: (D('76.00'), D('152.00'), D('229.00'), D('321.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Stormvoël', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Stormvoël Rd (M8) ramps',
        'points': [[-25.712799, 28.26635], [-25.712443, 28.265023]],
        'osm_nodes': ['n914799996', 'n914799997'],
        'through_points': [[-25.72496, 28.26506], [-25.70364, 28.27017]],
        'tariffs': {
            T2026: (D('12.50'), D('31.00'), D('36.00'), D('44.00')),  # GG 54087
            T2025: (D('12.00'), D('30.50'), D('35.00'), D('42.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Zambesi', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Zambesi Dr (R513) ramps',
        'points': [[-25.686369, 28.282431], [-25.68608, 28.280603]],
        'osm_nodes': ['n914802415', 'n914802401'],
        'through_points': [[-25.69703, 28.28085], [-25.67409, 28.27434]],
        'tariffs': {
            T2026: (D('15.00'), D('38.00'), D('44.00'), D('53.00')),  # GG 54087
            T2025: (D('14.50'), D('36.00'), D('42.00'), D('51.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Pumulani', 'plaza_type': 'mainline',
        'plaza_group': 'Pumulani', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.639437, 28.275682], [-25.639403, 28.275276]],
        'osm_nodes': ['n801449280', 'n329299805'],
        'through_points': [],
        'tariffs': {
            T2026: (D('16.50'), D('41.00'), D('47.00'), D('57.00')),  # GG 54087
            T2025: (D('16.00'), D('40.00'), D('46.00'), D('55.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Wallmansthal', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Wallmansthal ramps',
        'points': [[-25.580129, 28.282486], [-25.579947, 28.280703]],
        'osm_nodes': ['n914900516', 'n914900426'],
        'through_points': [[-25.59394, 28.28011], [-25.56656, 28.2829]],
        'tariffs': {
            T2026: (D('7.50'), D('19.00'), D('22.50'), D('26.00')),  # GG 54087
            T2025: (D('7.20'), D('18.00'), D('22.00'), D('25.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Murrayhill', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Murrayhill ramps',
        'points': [[-25.504205, 28.288609], [-25.503435, 28.287008]],
        'osm_nodes': ['n914980808', 'n914980805'],
        'through_points': [[-25.51463, 28.28493], [-25.48828, 28.29092]],
        'tariffs': {
            T2026: (D('15.00'), D('38.00'), D('45.00'), D('52.00')),  # GG 54087
            T2025: (D('14.50'), D('36.00'), D('44.00'), D('50.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Hammanskraal', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'R101 Hammanskraal ramps',
        'points': [[-25.404137, 28.297155], [-25.404039, 28.298753]],
        'osm_nodes': ['n419201986', 'n526923677'],
        'through_points': [[-25.41631, 28.29704], [-25.38681, 28.29908]],
        'tariffs': {
            T2026: (D('35.00'), D('120.00'), D('130.00'), D('150.00')),  # GG 54087
            T2025: (D('34.00'), D('116.00'), D('126.00'), D('145.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Carousel', 'plaza_type': 'mainline',
        'plaza_group': 'Carousel', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.324968, 28.297705], [-25.323082, 28.297999]],
        'osm_nodes': ['n60122252', 'n914992884'],
        'through_points': [],
        'tariffs': {
            T2026: (D('75.00'), D('202.00'), D('224.00'), D('258.00')),  # GG 54087
            T2025: (D('73.00'), D('196.00'), D('216.00'), D('249.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Maubane', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Maubane ramps',
        'points': [[-25.28231, 28.299188], [-25.281828, 28.297247]],
        'osm_nodes': ['n915014121', 'n915014111'],
        'through_points': [[-25.29418, 28.29791], [-25.26855, 28.29914]],
        'tariffs': {
            T2026: (D('33.00'), D('88.00'), D('97.00'), D('112.00')),  # GG 54087
            T2025: (D('31.50'), D('85.00'), D('94.00'), D('108.00')),  # GG 52072
        },
    },
    {
        'route': 'N1', 'name': 'Kranskop', 'plaza_type': 'mainline',
        'plaza_group': 'Kranskop', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-24.781662, 28.471642], [-24.781575, 28.471457]],
        'osm_nodes': ['n919125095', 'n919125076'],
        'through_points': [],
        'tariffs': {
            T2026: (D('61.50'), D('157.00'), D('210.00'), D('257.00')),  # GG 54088
            T2025: (D('60.00'), D('152.00'), D('203.00'), D('249.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Kranskop Ramp', 'plaza_type': 'ramp',
        'plaza_group': 'Kranskop', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'R33 Modimolle ramps at the plaza',
        'points': [[-24.781703, 28.471751], [-24.781493, 28.471311]],
        'osm_nodes': ['n4119700473', 'n919125127'],
        'through_points': [[-24.79001, 28.46259], [-24.77149, 28.47805]],
        'tariffs': {
            T2026: (D('17.00'), D('46.00'), D('54.00'), D('81.00')),  # GG 54088
            T2025: (D('16.00'), D('44.00'), D('52.00'), D('78.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Nyl', 'plaza_type': 'mainline',
        'plaza_group': 'Nyl', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-24.289925, 28.979588], [-24.289836, 28.979454]],
        'osm_nodes': ['n823723544', 'n823723796'],
        'through_points': [],
        'tariffs': {
            T2026: (D('79.50'), D('149.00'), D('180.00'), D('241.00')),  # GG 54088
            T2025: (D('77.00'), D('144.00'), D('174.00'), D('233.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Nyl Ramp', 'plaza_type': 'ramp',
        'plaza_group': 'Nyl', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Mookgophong ramps at the plaza',
        'points': [[-24.290013, 28.979688], [-24.289711, 28.979275]],
        'osm_nodes': ['n823723673', 'n823723532'],
        'through_points': [[-24.29992, 28.97062], [-24.27979, 28.98823]],
        'tariffs': {
            T2026: (D('24.50'), D('46.00'), D('54.00'), D('69.00')),  # GG 54088
            T2025: (D('24.00'), D('44.00'), D('52.00'), D('67.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Sebetiela', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'R101 Mokopane ramps',
        'points': [[-24.16778, 29.085452], [-24.1677, 29.0855]],
        'osm_nodes': ['n823723797', 'n919873910'],
        'through_points': [],
        'tariffs': {
            T2026: (D('24.50'), D('46.00'), D('58.00'), D('77.00')),  # GG 54088
            T2025: (D('24.00'), D('44.00'), D('56.00'), D('74.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Capricorn', 'plaza_type': 'mainline',
        'plaza_group': 'Capricorn', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-23.3669, 29.775024]],
        'osm_nodes': ['n529831939'],
        'through_points': [],
        'tariffs': {
            T2026: (D('63.50'), D('175.00'), D('205.00'), D('256.00')),  # GG 54088
            T2025: (D('62.00'), D('170.00'), D('198.00'), D('248.00')),  # GG 52073
        },
    },
    {
        'route': 'N1', 'name': 'Baobab', 'plaza_type': 'mainline',
        'plaza_group': 'Baobab', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-22.647131, 29.918103]],
        'osm_nodes': ['n13988550593'],
        'through_points': [],
        'tariffs': {
            T2026: (D('61.50'), D('168.00'), D('231.00'), D('278.00')),  # GG 54088
            T2025: (D('60.00'), D('163.00'), D('224.00'), D('269.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Tsitsikamma', 'plaza_type': 'mainline',
        'plaza_group': 'Tsitsikamma', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Mainline and ramp, same tariff',
        'points': [[-33.950376, 23.623239], [-33.94573, 23.619133], [-33.945255, 23.620038]],
        'osm_nodes': ['n39552657', 'n944650506', 'n3095966007'],
        'through_points': [],
        'tariffs': {
            T2026: (D('73.00'), D('183.00'), D('438.00'), D('619.00')),  # GG 54088
            T2025: (D('71.00'), D('178.00'), D('424.00'), D('600.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Izotsha', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Izotsha ramps (R61)',
        'points': [[-30.801082, 30.401593], [-30.798696, 30.40102]],
        'osm_nodes': ['n699693814', 'n734039235'],
        'through_points': [[-30.7887, 30.40788], [-30.81073, 30.39855]],
        'tariffs': {
            T2026: (D('12.50'), D('23.00'), D('31.00'), D('54.00')),  # GG 54088
            T2025: (D('12.00'), D('22.00'), D('30.00'), D('52.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Oribi', 'plaza_type': 'mainline',
        'plaza_group': 'Oribi', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'R61 South Coast toll road, both directions',
        'points': [[-30.748327, 30.433585], [-30.748319, 30.43341]],
        'osm_nodes': ['n734064749', 'n734064746'],
        'through_points': [],
        'tariffs': {
            T2026: (D('41.00'), D('73.00'), D('100.00'), D('162.00')),  # GG 54088
            T2025: (D('40.00'), D('70.00'), D('96.00'), D('157.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Oribi Ramp (S)', 'plaza_type': 'ramp',
        'plaza_group': 'Oribi', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'N2/R61 ramps south of the plaza',
        'points': [[-30.754447, 30.432877], [-30.754344, 30.432007]],
        'osm_nodes': ['n734064744', 'n734039163'],
        'through_points': [[-30.74206, 30.43439], [-30.76671, 30.42437]],
        'tariffs': {
            T2026: (D('18.50'), D('34.00'), D('46.00'), D('73.00')),  # GG 54088
            T2025: (D('18.00'), D('33.00'), D('44.00'), D('70.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Oribi Ramp (N)', 'plaza_type': 'ramp',
        'plaza_group': 'Oribi', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'N2/R61 ramps at the plaza',
        'points': [[-30.748336, 30.433751], [-30.748315, 30.433297]],
        'osm_nodes': ['n3096479400', 'n3096479399'],
        'through_points': [[-30.73671, 30.43587], [-30.75965, 30.43051]],
        'tariffs': {
            T2026: (D('22.00'), D('38.00'), D('54.00'), D('100.00')),  # GG 54088
            T2025: (D('21.00'), D('37.00'), D('52.00'), D('96.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Umtentweni', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Umtentweni ramps',
        'points': [[-30.700489, 30.446244], [-30.700451, 30.444696]],
        'osm_nodes': ['n734064745', 'n734064747'],
        'through_points': [[-30.69145, 30.45076], [-30.71198, 30.44187]],
        'tariffs': {
            T2026: (D('17.50'), D('31.00'), D('42.00'), D('69.00')),  # GG 54088
            T2025: (D('17.00'), D('30.00'), D('41.00'), D('67.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'King Shaka Airport', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'King Shaka Airport ramp',
        'points': [[-29.633648, 31.120698]],
        'osm_nodes': ['n569895252'],
        'through_points': [],
        'tariffs': {
            T2026: (D('8.50'), D('17.00'), D('26.00'), D('34.00')),  # GG 54088
            T2025: (D('8.00'), D('16.00'), D('25.00'), D('33.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Othongathi', 'plaza_type': 'mainline',
        'plaza_group': 'Othongathi', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-29.588214, 31.141158], [-29.58814, 31.140912]],
        'osm_nodes': ['n566862840', 'n566862843'],
        'through_points': [],
        'tariffs': {
            T2026: (D('15.50'), D('32.00'), D('42.00'), D('62.00')),  # GG 54088
            T2025: (D('15.00'), D('31.00'), D('41.00'), D('59.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Othongathi Ramp', 'plaza_type': 'ramp',
        'plaza_group': 'Othongathi', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'M43 oThongathi ramps (S&N)',
        'points': [[-29.58828, 31.141376], [-29.588098, 31.14068], [-29.582269, 31.145001], [-29.581248, 31.14373]],
        'osm_nodes': ['n578217341', 'n578217289', 'n578217611', 'n578217606'],
        'through_points': [[-29.59708, 31.13871], [-29.5752, 31.14983]],
        'tariffs': {
            T2026: (D('7.50'), D('17.00'), D('21.00'), D('31.00')),  # GG 54088
            T2025: (D('7.50'), D('17.00'), D('20.00'), D('30.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Mvoti', 'plaza_type': 'mainline',
        'plaza_group': 'Mvoti', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-29.403464, 31.28555], [-29.403355, 31.285325]],
        'osm_nodes': ['n761130379', 'n761130391'],
        'through_points': [],
        'tariffs': {
            T2026: (D('18.50'), D('52.00'), D('70.00'), D('104.00')),  # GG 54088
            T2025: (D('18.00'), D('50.00'), D('67.00'), D('101.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Mandini', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Mandini ramps',
        'points': [[-29.185402, 31.481534], [-29.184103, 31.48085]],
        'osm_nodes': ['n836592081', 'n836581828'],
        'through_points': [[-29.18981, 31.46955], [-29.18091, 31.49373]],
        'tariffs': {
            T2026: (D('10.00'), D('19.00'), D('23.00'), D('31.00')),  # GG 54088
            T2025: (D('10.00'), D('19.00'), D('22.00'), D('30.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Dokodweni', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'R66 Gingindlovu ramps',
        'points': [[-29.079386, 31.614203], [-29.078039, 31.613106]],
        'osm_nodes': ['n269459443', 'n589014401'],
        'through_points': [[-29.08815, 31.6069], [-29.06963, 31.62041]],
        'tariffs': {
            T2026: (D('27.00'), D('53.00'), D('62.00'), D('84.00')),  # GG 54088
            T2025: (D('26.00'), D('52.00'), D('59.00'), D('81.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Mtunzini', 'plaza_type': 'mainline',
        'plaza_group': 'Mtunzini', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-28.957315, 31.737757]],
        'osm_nodes': ['n836856678'],
        'through_points': [],
        'tariffs': {
            T2026: (D('63.50'), D('122.00'), D('146.00'), D('217.00')),  # GG 54088
            T2025: (D('62.00'), D('118.00'), D('141.00'), D('210.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Mtunzini Ramp (S)', 'plaza_type': 'ramp',
        'plaza_group': 'Mtunzini', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Mtunzini ramps at the plaza (to/from the south)',
        'points': [[-28.957429, 31.737974], [-28.957206, 31.737565]],
        'osm_nodes': ['n836856703', 'n800541182'],
        'through_points': [[-28.96703, 31.73085], [-28.94641, 31.74461]],
        'tariffs': {
            T2026: (D('53.00'), D('99.00'), D('119.00'), D('172.00')),  # GG 54088
            T2025: (D('51.00'), D('96.00'), D('115.00'), D('166.00')),  # GG 52073
        },
    },
    {
        'route': 'N2', 'name': 'Mtunzini Ramp (N)', 'plaza_type': 'ramp',
        'plaza_group': 'Mtunzini', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Mtunzini ramp plaza (to/from the north)',
        'points': [[-28.952328, 31.741863], [-28.952019, 31.74045]],
        'osm_nodes': ['n800541350', 'n800541245'],
        'through_points': [[-28.9613, 31.7349], [-28.94156, 31.74733]],
        'tariffs': {
            T2026: (D('11.50'), D('23.00'), D('27.00'), D('45.00')),  # GG 54088
            T2025: (D('11.00'), D('22.00'), D('26.00'), D('43.00')),  # GG 52073
        },
    },
    {
        'route': 'N3', 'name': 'Mariannhill', 'plaza_type': 'mainline',
        'plaza_group': 'Mariannhill', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-29.823294, 30.802874], [-29.823118, 30.802803], [-29.823015, 30.802755], [-29.822755, 30.802646]],
        'osm_nodes': ['n60177753', 'n829529249', 'n21631331', 'n829529209'],
        'through_points': [],
        'tariffs': {
            T2026: (D('16.50'), D('30.00'), D('37.00'), D('57.00')),  # GG 54088
            T2025: (D('16.00'), D('29.00'), D('35.00'), D('55.00')),  # GG 52073
        },
    },
    {
        'route': 'N3', 'name': 'Mooi', 'plaza_type': 'mainline',
        'plaza_group': 'Mooi', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-29.218086, 30.003481], [-29.218049, 30.003748]],
        'osm_nodes': ['n447966738', 'n447966739'],
        'through_points': [],
        'tariffs': {
            T2026: (D('70.00'), D('171.00'), D('240.00'), D('324.00')),  # GG 54087
            T2025: (D('67.00'), D('165.00'), D('231.00'), D('313.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'Mooi Ramp (S)', 'plaza_type': 'ramp',
        'plaza_group': 'Mooi', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'Mooi River ramp plaza south of the mainline plaza',
        'points': [[-29.221669, 30.004974], [-29.221694, 30.003675]],
        'osm_nodes': ['n815650220', 'n815650310'],
        'through_points': [[-29.2099, 30.00248], [-29.23167, 30.00765]],
        'tariffs': {
            T2026: (D('49.00'), D('119.00'), D('168.00'), D('227.00')),  # GG 54087
            T2025: (D('47.00'), D('115.00'), D('162.00'), D('219.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'Mooi Ramp (N)', 'plaza_type': 'ramp',
        'plaza_group': 'Mooi', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'Mooi River ramps at the mainline plaza',
        'points': [[-29.218126, 30.003213], [-29.218017, 30.003956]],
        'osm_nodes': ['n447966740', 'n248701522'],
        'through_points': [[-29.20669, 30.00194], [-29.22864, 30.0057]],
        'tariffs': {
            T2026: (D('21.00'), D('51.00'), D('72.00'), D('97.00')),  # GG 54087
            T2025: (D('20.00'), D('49.00'), D('69.00'), D('94.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'Treverton', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'Treverton ramps',
        'points': [[-29.193296, 29.993041], [-29.192692, 29.994124]],
        'osm_nodes': ['n815650197', 'n815583899'],
        'through_points': [[-29.18548, 29.98599], [-29.2026, 30.00114]],
        'tariffs': {
            T2026: (D('21.00'), D('51.00'), D('72.00'), D('97.00')),  # GG 54087
            T2025: (D('20.00'), D('49.00'), D('69.00'), D('94.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'Bergville', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'N11/R616 Ladysmith/Bergville ramps',
        'points': [[-28.588704, 29.608324], [-28.588567, 29.609759]],
        'osm_nodes': ['n671785437', 'n671785442'],
        'through_points': [[-28.57774, 29.60785], [-28.60238, 29.61075]],
        'tariffs': {
            T2026: (D('30.00'), D('35.00'), D('65.00'), D('100.00')),  # GG 54087
            T2025: (D('29.00'), D('34.00'), D('63.00'), D('96.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'Tugela', 'plaza_type': 'mainline',
        'plaza_group': 'Tugela', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-28.462332, 29.561564], [-28.462221, 29.561747]],
        'osm_nodes': ['n257893328', 'n813893021'],
        'through_points': [],
        'tariffs': {
            T2026: (D('100.00'), D('165.00'), D('260.00'), D('359.00')),  # GG 54087
            T2025: (D('96.00'), D('159.00'), D('251.00'), D('347.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'Tugela East', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'R103 east of the Tugela plaza',
        'points': [[-28.458095, 29.569971]],
        'osm_nodes': ['n671775188'],
        'through_points': [],
        'tariffs': {
            T2026: (D('62.00'), D('102.00'), D('152.00'), D('211.00')),  # GG 54087
            T2025: (D('60.00'), D('99.00'), D('147.00'), D('204.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'Wilge', 'plaza_type': 'mainline',
        'plaza_group': 'Wilge', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-27.040606, 28.625987], [-27.040485, 28.626236]],
        'osm_nodes': ['n257913957', 'n21630360'],
        'through_points': [],
        'tariffs': {
            T2026: (D('94.00'), D('161.00'), D('215.00'), D('304.00')),  # GG 54087
            T2025: (D('90.00'), D('155.00'), D('207.00'), D('294.00')),  # GG 52072
        },
    },
    {
        'route': 'N3', 'name': 'De Hoek', 'plaza_type': 'mainline',
        'plaza_group': 'De Hoek', 'operator': 'N3TC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.663989, 28.389561], [-26.663962, 28.389731], [-26.663933, 28.389924], [-26.663917, 28.39005]],
        'osm_nodes': ['n804139445', 'n257895609', 'n21630299', 'n804139439'],
        'through_points': [],
        'tariffs': {
            T2026: (D('67.00'), D('105.00'), D('160.00'), D('230.00')),  # GG 54087
            T2025: (D('65.00'), D('101.00'), D('154.00'), D('222.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Pelindaba', 'plaza_type': 'mainline',
        'plaza_group': 'Pelindaba', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions (Magalies toll route)',
        'points': [[-25.778044, 27.959813], [-25.777999, 27.959806], [-25.777865, 27.959782], [-25.77782, 27.959773]],
        'osm_nodes': ['n132541563', 'n791663784', 'n791663819', 'n132541453'],
        'through_points': [],
        'tariffs': {
            T2026: (D('8.00'), D('15.00'), D('21.00'), D('27.00')),  # GG 54088
            T2025: (D('8.00'), D('15.00'), D('20.00'), D('26.00')),  # GG 52073
        },
    },
    {
        'route': 'N4', 'name': 'Quagga', 'plaza_type': 'mainline',
        'plaza_group': 'Quagga', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.749318, 28.114889], [-25.749231, 28.114878]],
        'osm_nodes': ['n48279216', 'n791838155'],
        'through_points': [],
        'tariffs': {
            T2026: (D('6.50'), D('11.00'), D('16.00'), D('21.00')),  # GG 54088
            T2025: (D('6.00'), D('11.00'), D('15.00'), D('20.00')),  # GG 52073
        },
    },
    {
        'route': 'N4', 'name': 'Swartruggens', 'plaza_type': 'mainline',
        'plaza_group': 'Swartruggens', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.660835, 26.605046]],
        'osm_nodes': ['n905028996'],
        'through_points': [],
        'tariffs': {
            T2026: (D('103.00'), D('258.00'), D('313.00'), D('368.00')),  # GG 54087
            T2025: (D('99.00'), D('249.00'), D('302.00'), D('355.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Kroondal', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'R104 Kroondal ramps',
        'points': [[-25.730335, 27.309179], [-25.728422, 27.308771]],
        'osm_nodes': ['n1404838607', 'n209674496'],
        'through_points': [[-25.73427, 27.32428], [-25.72214, 27.29543]],
        'tariffs': {
            T2026: (D('20.00'), D('48.00'), D('54.00'), D('64.00')),  # GG 54087
            T2025: (D('19.50'), D('47.00'), D('52.00'), D('62.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Marikana', 'plaza_type': 'mainline',
        'plaza_group': 'Marikana', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.747462, 27.397292]],
        'osm_nodes': ['n209674351'],
        'through_points': [],
        'tariffs': {
            T2026: (D('30.00'), D('72.00'), D('81.00'), D('96.00')),  # GG 54087
            T2025: (D('29.00'), D('70.00'), D('79.00'), D('93.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Buffelspoort', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'R104 Buffelspoort ramps',
        'points': [[-25.752426, 27.492188], [-25.751244, 27.492583]],
        'osm_nodes': ['n874181537', 'n874181501'],
        'through_points': [[-25.74934, 27.5048], [-25.75207, 27.48034]],
        'tariffs': {
            T2026: (D('20.00'), D('48.00'), D('54.00'), D('64.00')),  # GG 54087
            T2025: (D('19.50'), D('47.00'), D('52.00'), D('62.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Brits', 'plaza_type': 'mainline',
        'plaza_group': 'Brits', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.650135, 27.921547], [-25.649953, 27.921551]],
        'osm_nodes': ['n921923001', 'n921922982'],
        'through_points': [],
        'tariffs': {
            T2026: (D('20.00'), D('70.00'), D('77.00'), D('90.00')),  # GG 54087
            T2025: (D('19.50'), D('68.00'), D('74.00'), D('87.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'K99', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Dr Swanepoel Rd (Doornpoort) ramps',
        'points': [[-25.644922, 28.246627], [-25.642025, 28.24276]],
        'osm_nodes': ['n914847368', 'n914847580'],
        'through_points': [],
        'tariffs': {
            T2026: (D('20.00'), D('50.00'), D('58.00'), D('70.00')),  # GG 54087
            T2025: (D('19.50'), D('49.00'), D('56.00'), D('68.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Doornpoort', 'plaza_type': 'mainline',
        'plaza_group': 'Doornpoort', 'operator': 'Bakwena', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.643429, 28.253726], [-25.643178, 28.253722]],
        'osm_nodes': ['n576245586', 'n914847500'],
        'through_points': [],
        'tariffs': {
            T2026: (D('20.00'), D('50.00'), D('58.00'), D('70.00')),  # GG 54087
            T2025: (D('19.50'), D('49.00'), D('56.00'), D('68.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Donkerhoek', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'R964 Donkerhoek ramps',
        'points': [[-25.773681, 28.433009], [-25.771952, 28.432528]],
        'osm_nodes': ['n3381092758', 'n3381092759'],
        'through_points': [[-25.77098, 28.41936], [-25.77555, 28.4509]],
        'tariffs': {
            T2026: (D('17.00'), D('24.00'), D('34.00'), D('66.00')),  # GG 54087
            T2025: (D('16.00'), D('23.00'), D('33.00'), D('64.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Cullinan', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'R515 Cullinan/Rayton ramps',
        'points': [[-25.795553, 28.516437], [-25.798776, 28.515731]],
        'osm_nodes': ['n60954009', 'n746754012'],
        'through_points': [],
        'tariffs': {
            T2026: (D('21.00'), D('34.00'), D('51.00'), D('86.00')),  # GG 54087
            T2025: (D('20.00'), D('33.00'), D('49.00'), D('83.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Diamond Hill', 'plaza_type': 'mainline',
        'plaza_group': 'Diamond Hill', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.798169, 28.550378], [-25.797929, 28.550418]],
        'osm_nodes': ['n29996902', 'n29992058'],
        'through_points': [],
        'tariffs': {
            T2026: (D('51.00'), D('70.00'), D('133.00'), D('220.00')),  # GG 54087
            T2025: (D('49.00'), D('68.00'), D('128.00'), D('213.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Valtaki East', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'Valtaki ramps, to/from the east',
        'points': [[-25.801716, 28.618243], [-25.797614, 28.617717]],
        'osm_nodes': ['n5329407432', 'n5329407428'],
        'through_points': [],
        'tariffs': {
            T2026: (D('39.00'), D('55.00'), D('81.00'), D('183.00')),  # GG 54087
            T2025: (D('38.00'), D('53.00'), D('78.00'), D('177.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Ekandustria East', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'Ekandustria ramps, to/from the east',
        'points': [[-25.809542, 28.697067], [-25.8062, 28.697812]],
        'osm_nodes': ['n5329438350', 'n5329438349'],
        'through_points': [],
        'tariffs': {
            T2026: (D('31.00'), D('47.00'), D('65.00'), D('130.00')),  # GG 54087
            T2025: (D('30.00'), D('45.00'), D('63.00'), D('126.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Middelburg', 'plaza_type': 'mainline',
        'plaza_group': 'Middelburg', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.866093, 29.363962], [-25.865924, 29.363821]],
        'osm_nodes': ['n32976019', 'n32805991'],
        'through_points': [],
        'tariffs': {
            T2026: (D('84.00'), D('182.00'), D('277.00'), D('365.00')),  # GG 54087
            T2025: (D('81.00'), D('176.00'), D('268.00'), D('352.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Machadodorp', 'plaza_type': 'mainline',
        'plaza_group': 'Machadodorp', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.627876, 30.258389], [-25.627775, 30.258219]],
        'osm_nodes': ['n13828506765', 'n32807703'],
        'through_points': [],
        'tariffs': {
            T2026: (D('126.00'), D('350.00'), D('510.00'), D('729.00')),  # GG 54087
            T2025: (D('122.00'), D('338.00'), D('493.00'), D('704.00')),  # GG 52072
        },
    },
    {
        'route': 'N4', 'name': 'Nkomazi', 'plaza_type': 'mainline',
        'plaza_group': 'Nkomazi', 'operator': 'TRAC', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-25.536261, 31.344423]],
        'osm_nodes': ['n760531986'],
        'through_points': [],
        'tariffs': {
            T2026: (D('95.00'), D('193.00'), D('281.00'), D('405.00')),  # GG 54087
            T2025: (D('92.00'), D('187.00'), D('271.00'), D('391.00')),  # GG 52072
        },
    },
    {
        'route': 'N17', 'name': 'Gosforth', 'plaza_type': 'mainline',
        'plaza_group': 'Gosforth', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.248472, 28.158343], [-26.248144, 28.158272]],
        'osm_nodes': ['n264556739', 'n264556791'],
        'through_points': [],
        'tariffs': {
            T2026: (D('17.00'), D('46.00'), D('50.00'), D('69.00')),  # GG 54088
            T2025: (D('16.00'), D('44.00'), D('48.00'), D('67.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Gosforth Ramp (W)', 'plaza_type': 'ramp',
        'plaza_group': 'Gosforth', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Ramps west of the plaza',
        'points': [[-26.251417, 28.140093], [-26.251264, 28.142931]],
        'osm_nodes': ['n288950188', 'n59748999'],
        'through_points': [[-26.24559, 28.13124], [-26.24902, 28.15344]],
        'tariffs': {
            T2026: (D('9.50'), D('19.00'), D('25.00'), D('33.00')),  # GG 54088
            T2025: (D('9.00'), D('19.00'), D('24.00'), D('31.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Gosforth Ramp (E)', 'plaza_type': 'ramp',
        'plaza_group': 'Gosforth', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Ramps at the plaza',
        'points': [[-26.248716, 28.158391], [-26.247988, 28.158241]],
        'osm_nodes': ['n312353939', 'n312354373'],
        'through_points': [[-26.25122, 28.14431], [-26.25215, 28.16895]],
        'tariffs': {
            T2026: (D('7.50'), D('29.00'), D('31.00'), D('42.00')),  # GG 54088
            T2025: (D('7.50'), D('28.00'), D('30.00'), D('41.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Dalpark', 'plaza_type': 'mainline',
        'plaza_group': 'Dalpark', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.256418, 28.327614], [-26.25618, 28.327671]],
        'osm_nodes': ['n339251302', 'n339251285'],
        'through_points': [],
        'tariffs': {
            T2026: (D('15.50'), D('32.00'), D('42.00'), D('58.00')),  # GG 54088
            T2025: (D('15.00'), D('31.00'), D('41.00'), D('56.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Denne', 'plaza_type': 'ramp',
        'plaza_group': '', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Denne ramps',
        'points': [[-26.267513, 28.360712], [-26.267481, 28.361914], [-26.267464, 28.360739], [-26.267445, 28.361938]],
        'osm_nodes': ['n303167368', 'n3408234095', 'n3408234094', 'n256772010'],
        'through_points': [[-26.26295, 28.35119], [-26.27325, 28.3726]],
        'tariffs': {
            T2026: (D('13.50'), D('27.00'), D('35.00'), D('46.00')),  # GG 54088
            T2025: (D('13.00'), D('26.00'), D('33.00'), D('44.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Leandra', 'plaza_type': 'mainline',
        'plaza_group': 'Leandra', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.398529, 28.949248]],
        'osm_nodes': ['n1454250267'],
        'through_points': [],
        'tariffs': {
            T2026: (D('50.50'), D('127.00'), D('190.00'), D('253.00')),  # GG 54088
            T2025: (D('49.00'), D('123.00'), D('184.00'), D('244.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Leandra Ramp', 'plaza_type': 'ramp',
        'plaza_group': 'Leandra', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'R50 Leandra ramps',
        'points': [[-26.397314, 28.937178], [-26.395229, 28.936437]],
        'osm_nodes': ['n3115582341', 'n3115582340'],
        'through_points': [[-26.39431, 28.92612], [-26.39875, 28.95037]],
        'tariffs': {
            T2026: (D('30.50'), D('77.00'), D('113.00'), D('152.00')),  # GG 54088
            T2025: (D('29.00'), D('74.00'), D('110.00'), D('147.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Trichardt', 'plaza_type': 'mainline',
        'plaza_group': 'Trichardt', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.483678, 29.326082]],
        'osm_nodes': ['n560637274'],
        'through_points': [],
        'tariffs': {
            T2026: (D('25.00'), D('63.00'), D('96.00'), D('127.00')),  # GG 54088
            T2025: (D('24.00'), D('61.00'), D('93.00'), D('122.00')),  # GG 52073
        },
    },
    {
        'route': 'N17', 'name': 'Ermelo', 'plaza_type': 'mainline',
        'plaza_group': 'Ermelo', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-26.505561, 29.865973]],
        'osm_nodes': ['n2727939242'],
        'through_points': [],
        'tariffs': {
            T2026: (D('45.00'), D('114.00'), D('170.00'), D('226.00')),  # GG 54088
            T2025: (D('44.00'), D('110.00'), D('164.00'), D('219.00')),  # GG 52073
        },
    },
    {
        'route': 'R30', 'name': 'Brandfort', 'plaza_type': 'mainline',
        'plaza_group': 'Brandfort', 'operator': 'SANRAL', 'country': 'ZA',
        'direction': 'Both directions',
        'points': [[-28.902209, 26.337948], [-28.902138, 26.337781]],
        'osm_nodes': ['n3095937462', 'n7288154357'],
        'through_points': [],
        'tariffs': {
            T2026: (D('62.50'), D('125.00'), D('188.00'), D('265.00')),  # GG 54088
            T2025: (D('61.00'), D('121.00'), D('182.00'), D('256.00')),  # GG 52073
        },
    },]

# ---------------------------------------------------------------------------
# Mozambique (not SANRAL). Tariffs published in MZN; stored in ZAR.
# ---------------------------------------------------------------------------
MZN_ZAR = D('0.2548')


def _mzn(*amounts):
    return tuple((D(a) * MZN_ZAR).quantize(D('0.01'), rounding=ROUND_HALF_UP) for a in amounts)


TRAC_MZ_SOURCE = ('https://tracn4.co.za/toll-plazas-toll-fees/', 'TRAC N4 Mozambique tariffs (MZN), page eff. 1 March 2026')
REVIMO_SOURCE = ('https://www.revimo.co.mz/Tarifas.php', 'REVIMO toll tariffs (MZN)')

MZ_PLAZAS = [
    {'route': 'EN4', 'name': 'Moamba', 'plaza_type': 'mainline', 'plaza_group': 'Moamba', 'operator': 'TRAC',
     'country': 'MZ', 'direction': 'Both directions', 'points': [[-25.64459, 32.21797]], 'osm_nodes': ['n1840724886'],
     'through_points': [], 'mzn': (240, 600, 1200, 1800), 'effective_from': T2026, 'source': TRAC_MZ_SOURCE},
    {'route': 'EN4', 'name': 'Maputo', 'plaza_type': 'mainline', 'plaza_group': 'Maputo', 'operator': 'TRAC',
     'country': 'MZ', 'direction': 'Both directions (Matola)', 'points': [[-25.93090, 32.51553], [-25.93062, 32.51569]],
     'osm_nodes': ['n5868790043', 'n564926552'], 'through_points': [],
     'mzn': (30, 130, 375, 550), 'effective_from': T2026, 'source': TRAC_MZ_SOURCE},
    {'route': 'REVIMO', 'name': 'Ponte Maputo-Katembe', 'plaza_type': 'mainline', 'plaza_group': 'Katembe',
     'operator': 'REVIMO', 'country': 'MZ', 'direction': 'Both directions (bridge)',
     'points': [[-25.99583, 32.55576], [-25.99581, 32.55583]], 'osm_nodes': ['n5145417229', 'n5145417251'],
     'through_points': [], 'mzn': (100, 250, 750, 1200), 'effective_from': date(2025, 5, 15), 'source': REVIMO_SOURCE},
    # Maputo ring road (Estrada Circular): REVIMO lists Costa do Sol, Zintava,
    # Cumbeza and Matola Gare at one tariff. Only these two are on the map.
    {'route': 'REVIMO', 'name': 'Costa do Sol', 'plaza_type': 'mainline', 'plaza_group': 'Costa do Sol',
     'operator': 'REVIMO', 'country': 'MZ', 'direction': 'Estrada Circular', 'points': [[-25.87331, 32.66206]],
     'osm_nodes': ['w1133878616'], 'through_points': [], 'mzn': (30, 140, 380, 580), 'effective_from': None,
     'source': REVIMO_SOURCE},
    {'route': 'REVIMO', 'name': 'Zintava', 'plaza_type': 'mainline', 'plaza_group': 'Zintava',
     'operator': 'REVIMO', 'country': 'MZ', 'direction': 'Estrada Circular', 'points': [[-25.78057, 32.67348]],
     'osm_nodes': ['n10571095414'], 'through_points': [], 'mzn': (30, 140, 380, 580), 'effective_from': None,
     'source': REVIMO_SOURCE},
    # N200 Maputo–Ponta do Ouro (the Kosi Bay route). The booth on the map is
    # "Portagem da Belavista"; REVIMO lists two N200 plazas, Mahubo and Ponta
    # D'Ouro, both MZN 300 / 700 / 1,000 for classes 2–4. Which one this booth
    # is cannot be confirmed; Class 1 differs (130 vs 100) — the higher is used.
    {'route': 'REVIMO', 'name': 'N200 Belavista', 'plaza_type': 'mainline', 'plaza_group': 'N200 Belavista',
     'operator': 'REVIMO', 'country': 'MZ', 'direction': 'N200 Maputo–Ponta do Ouro', 'points': [[-26.37176, 32.65474]],
     'osm_nodes': ['n7712387522'], 'through_points': [], 'mzn': (130, 300, 700, 1000), 'effective_from': None,
     'source': REVIMO_SOURCE},
]
for _p in MZ_PLAZAS:
    _p['tariffs'] = {_p['effective_from']: _mzn(*_p['mzn'])}
