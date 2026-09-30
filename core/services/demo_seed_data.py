"""Fixed, FICTIONAL reference data for the shared public demo company.

Everything here is invented for product screenshots and the public "Open the
demo" login. None of it describes a real business or person:

  - Company, customers, vendors: made-up names, deliberately checked against a
    denylist of real South African brands (see core/tests/test_demo_company_seed.py).
  - Registration / VAT / bank numbers: obviously-dummy digit runs.
  - E-mail addresses: subdomains of example.com, which RFC 2606 reserves — a
    stray e-mail can never reach a real inbox (example.co.za is NOT reserved).
  - Phone numbers: the 555 01xx range, never a live number.
  - Plates: standard GP/ZN/WC formats, never personalised plates.
  - VINs / licence numbers: DEMO-prefixed so they can never collide with a real
    tenant's row (both fields are globally unique).

Lanes, distances, SANRAL plaza names and diesel prices ARE real-world data —
that is what makes the demo truthful. Tolls are summed from the TollPlaza table
(falling back to the same 2026 tariff poster seed_toll_data loads); diesel comes
from stored FuelPrice rows (falling back to the fuel service's monthly table).
"""
from decimal import Decimal as D

DEMO_COMPANY_PROFILE = {
    'company_name': 'Karoo Line Logistics (Pty) Ltd',
    # Obviously-dummy identifiers: a real CIPC number is never all zeros and a
    # real VAT number is never 4000000000.
    'registration_number': '2011/000000/07',
    'vat_number': '4000000000',
    'industry': 'logistics',
    'website': 'https://karooline.example.com',
    'description': (
        'Fictional demo company. Johannesburg-based general-freight carrier running '
        'superlinks, tautliners and tippers on the N3, N1 and N4 corridors, plus '
        'cross-border work into Maputo.'
    ),
    'address': {
        'street': '14 Wagon Wheel Road, City Deep',
        'city': 'Johannesburg',
        'province': 'Gauteng',
        'postal_code': '2049',
        'country': 'South Africa',
    },
    'contact': {
        'phone': '+27 11 555 0100',
        'email': 'ops@karooline.example.com',
        'name': 'Naledi Dube',
    },
    'bank_name': 'Demo Bank (fictional)',
    'bank_account_holder': 'Karoo Line Logistics (Pty) Ltd',
    'bank_account_number': '000000123456',
    'bank_branch_code': '000000',
    'bank_account_type': 'CHEQUE',
    'payment_reference_hint': 'DEMO ACCOUNT - fictional banking details, do not pay. Use the invoice number as reference.',
    'fuel_zone': 'INLAND',
    'default_base_rate_per_km': D('24.00'),
    'default_sla_hours': 48,
    'default_quote_validity_days': 7,
    'allow_cross_border': True,
    'cross_border_crossings_per_year': 60,
    'cipc_age_years': 14,
    'turnover_trend': 'growing',
    'fleet_size': 15,
    'province_count': 7,
    'business_type': 'fleet_operator',
    'sub_sector': 'general_freight',
    'insurance_status': 'comprehensive',
    'b_bbee_level': 2,
}

DEMO_ADMIN_NAME = ('Naledi', 'Dube')
DEMO_ADMIN_JOB_TITLE = 'Operations Manager'

# Depot — trucks idle here.
DEPOT = ('City Deep, Johannesburg', D('-26.2167'), D('28.0833'))

# capacity = payload in TONNES (the VehicleType convention). sanral_toll_class
# counts every axle on truck + trailers: a superlink / tri-axle combination is
# Class 4, a 6x4 rigid Class 3, a 2-axle box truck Class 2.
DEMO_VEHICLE_TYPES = [
    {'name': 'Superlink Tautliner', 'capacity': 34, 'max_distance': 5000, 'base_rate': 24, 'fuel_consumption_l_per_100km': 42, 'sanral_toll_class': 4, 'description': '6x4 truck-tractor with a 6+12m interlink tautliner.'},
    {'name': 'Tri-axle Tautliner',  'capacity': 30, 'max_distance': 5000, 'base_rate': 23, 'fuel_consumption_l_per_100km': 38, 'sanral_toll_class': 4, 'description': '6x4 truck-tractor with a 13.6m tri-axle tautliner.'},
    {'name': 'Side Tipper',         'capacity': 32, 'max_distance': 1500, 'base_rate': 24, 'fuel_consumption_l_per_100km': 44, 'sanral_toll_class': 4, 'description': 'Interlink side tipper for grain, aggregate and coal.'},
    {'name': 'Reefer Tri-axle',     'capacity': 28, 'max_distance': 5000, 'base_rate': 27, 'fuel_consumption_l_per_100km': 44, 'sanral_toll_class': 4, 'description': 'Refrigerated tri-axle for fresh produce.'},
    {'name': 'Rigid 6x4 Curtainsider', 'capacity': 14, 'max_distance': 800, 'base_rate': 19, 'fuel_consumption_l_per_100km': 28, 'sanral_toll_class': 3, 'description': '14-ton 6x4 rigid for regional distribution.'},
    {'name': 'Box Truck 4t',        'capacity': 4,  'max_distance': 400, 'base_rate': 14, 'fuel_consumption_l_per_100km': 16, 'sanral_toll_class': 2, 'description': '4-ton box body for city deliveries.'},
]

HEAVY_TYPES = ('Superlink Tautliner', 'Tri-axle Tautliner', 'Side Tipper', 'Reefer Tri-axle')
RIGID_TYPES = ('Rigid 6x4 Curtainsider', 'Box Truck 4t')

# vin keeps the DEMOVIN… scheme the original demo used, so a non-reset re-seed
# upgrades those rows in place instead of duplicating them.
# odo = odometer (km) twelve months ago. driver = index into DEMO_DRIVERS.
DEMO_VEHICLES = [
    {'n': 1,  'plate': 'JT 42 KL GP', 'make': 'Scania',        'model': 'R 460 A6x4',       'year': 2021, 'type': 'Superlink Tautliner', 'odo': 412000, 'driver': 0,  'financed': False},
    {'n': 2,  'plate': 'KB 18 WX GP', 'make': 'Scania',        'model': 'R 460 A6x4',       'year': 2022, 'type': 'Superlink Tautliner', 'odo': 268000, 'driver': 1,  'financed': True},
    {'n': 3,  'plate': 'HV 07 PR GP', 'make': 'Mercedes-Benz', 'model': 'Actros 2645LS/33', 'year': 2020, 'type': 'Tri-axle Tautliner',  'odo': 498000, 'driver': 2,  'financed': False},
    {'n': 4,  'plate': 'ND 482-731',  'make': 'Mercedes-Benz', 'model': 'Actros 2645LS/33', 'year': 2023, 'type': 'Superlink Tautliner', 'odo': 141000, 'driver': 3,  'financed': True},
    {'n': 5,  'plate': 'ND 219-045',  'make': 'Volvo',         'model': 'FH 440 6x4',       'year': 2019, 'type': 'Superlink Tautliner', 'odo': 623000, 'driver': 4,  'financed': False},
    {'n': 6,  'plate': 'CA 318-552',  'make': 'Volvo',         'model': 'FH 460 6x4',       'year': 2022, 'type': 'Reefer Tri-axle',     'odo': 254000, 'driver': 5,  'financed': True},
    {'n': 7,  'plate': 'FZ 63 LM GP', 'make': 'MAN',           'model': 'TGS 27.440 6x4',   'year': 2018, 'type': 'Side Tipper',         'odo': 702000, 'driver': 6,  'financed': False},
    {'n': 8,  'plate': 'LC 91 TD GP', 'make': 'UD Trucks',     'model': 'Quon GW 26.460',   'year': 2021, 'type': 'Tri-axle Tautliner',  'odo': 389000, 'driver': 7,  'financed': False},
    {'n': 9,  'plate': 'GR 35 NB GP', 'make': 'UD Trucks',     'model': 'Quon GW 26.460',   'year': 2020, 'type': 'Side Tipper',         'odo': 455000, 'driver': 8,  'financed': False},
    {'n': 10, 'plate': 'MX 24 HF GP', 'make': 'FAW',           'model': 'JH6 28.500 6x4',   'year': 2022, 'type': 'Superlink Tautliner', 'odo': 236000, 'driver': 9,  'financed': True},
    {'n': 11, 'plate': 'BW 56 ZK GP', 'make': 'Isuzu',         'model': 'FVZ 1400 6x4',     'year': 2020, 'type': 'Rigid 6x4 Curtainsider', 'odo': 318000, 'driver': 10, 'financed': False},
    {'n': 12, 'plate': 'ND 604-118',  'make': 'Hino',          'model': '500 2836 6x4',     'year': 2021, 'type': 'Rigid 6x4 Curtainsider', 'odo': 246000, 'driver': 11, 'financed': False},
    {'n': 13, 'plate': 'DY 12 VC GP', 'make': 'Mercedes-Benz', 'model': 'Actros 2652LS/33', 'year': 2017, 'type': 'Superlink Tautliner', 'odo': 884000, 'driver': 12, 'financed': False},
    {'n': 14, 'plate': 'CA 771-209',  'make': 'Scania',        'model': 'G 460 A6x4',       'year': 2018, 'type': 'Tri-axle Tautliner',  'odo': 731000, 'driver': 13, 'financed': False},
    {'n': 15, 'plate': 'PK 88 RS GP', 'make': 'Isuzu',         'model': 'NPR 400 AMT',      'year': 2022, 'type': 'Box Truck 4t',        'odo': 98000,  'driver': None, 'financed': True},
]

# license_number keeps the DEMO-DL-… scheme (globally unique field, and it
# upgrades the original four demo drivers in place). Expiry offsets are days
# from "today" so the alerts stay live however old the seed is.
# inactive_from: days ago the driver stopped working (None = active).
DEMO_DRIVERS = [
    {'first': 'Thabo',    'last': 'Mokoena',   'state': 'Gauteng',       'hired_years': 9,  'exp': 21, 'lic': 700,  'med': 240, 'viol': 0, 'acc': 0},
    {'first': 'Lindiwe',  'last': 'Dlamini',   'state': 'KwaZulu-Natal', 'hired_years': 6,  'exp': 14, 'lic': 18,   'med': 300, 'viol': 1, 'acc': 0},
    {'first': 'Pieter',   'last': 'Venter',    'state': 'Free State',    'hired_years': 11, 'exp': 26, 'lic': 1100, 'med': 150, 'viol': 0, 'acc': 1},
    {'first': 'Sibusiso', 'last': 'Khumalo',   'state': 'KwaZulu-Natal', 'hired_years': 4,  'exp': 9,  'lic': 480,  'med': 25,  'viol': 2, 'acc': 0},
    {'first': 'Mandla',   'last': 'Zulu',      'state': 'KwaZulu-Natal', 'hired_years': 7,  'exp': 17, 'lic': 820,  'med': 410, 'viol': 0, 'acc': 0},
    {'first': 'Riaan',    'last': 'Steyn',     'state': 'Western Cape',  'hired_years': 5,  'exp': 19, 'lic': 39,   'med': 190, 'viol': 1, 'acc': 1},
    {'first': 'Kagiso',   'last': 'Molefe',    'state': 'North West',    'hired_years': 3,  'exp': 8,  'lic': 1300, 'med': 330, 'viol': 0, 'acc': 0},
    {'first': 'Nomvula',  'last': 'Mthembu',   'state': 'Gauteng',       'hired_years': 2,  'exp': 6,  'lic': 950,  'med': 280, 'viol': 0, 'acc': 0},
    {'first': 'Themba',   'last': 'Nkosi',     'state': 'Mpumalanga',    'hired_years': 8,  'exp': 22, 'lic': 610,  'med': 95,  'viol': 3, 'acc': 1},
    {'first': 'Jacques',  'last': 'Pretorius', 'state': 'Gauteng',       'hired_years': 5,  'exp': 12, 'lic': 1480, 'med': 360, 'viol': 0, 'acc': 0},
    {'first': 'Lucky',    'last': 'Maluleke',  'state': 'Limpopo',       'hired_years': 6,  'exp': 15, 'lic': 270,  'med': 205, 'viol': 1, 'acc': 0},
    {'first': 'Tshepo',   'last': 'Sithole',   'state': 'Gauteng',       'hired_years': 1,  'exp': 5,  'lic': 1210, 'med': 340, 'viol': 0, 'acc': 0},
    {'first': 'Andile',   'last': 'Radebe',    'state': 'Eastern Cape',  'hired_years': 5,  'exp': 13, 'lic': 560,  'med': 60,  'viol': 2, 'acc': 1, 'inactive_from': 120},
    {'first': 'Faizel',   'last': 'Adams',     'state': 'Western Cape',  'hired_years': 10, 'exp': 24, 'lic': 390,  'med': 120, 'viol': 0, 'acc': 0, 'inactive_from': 50},
]

# ---------------------------------------------------------------------------
# Lanes — real corridors, real distances, real SANRAL mainline plaza names.
# heavy / rigid = contracted all-in rate (ex VAT) for that vehicle class.
# The two loss-making lanes are deliberate: DBN→GQB is backhaul-starved and
# tendered too cheaply; JHB→PLK is short with four expensive N1 plazas.
# ---------------------------------------------------------------------------
_PLACES = {
    'JHB':  ('City Deep, Johannesburg', 'Johannesburg', 'GP', '2049', D('-26.2167'), D('28.0833')),
    'ISA':  ('Isando, Kempton Park', 'Kempton Park', 'GP', '1600', D('-26.1435'), D('28.2010')),
    'MID':  ('Midrand, Johannesburg', 'Midrand', 'GP', '1685', D('-25.9992'), D('28.1263')),
    'PTA':  ('Pretoria West, Pretoria', 'Pretoria', 'GP', '0183', D('-25.7560'), D('28.1580')),
    'DBN':  ('Prospecton, Durban', 'Durban', 'KZN', '4110', D('-29.9690'), D('30.9340')),
    'PMB':  ('Willowton, Pietermaritzburg', 'Pietermaritzburg', 'KZN', '3201', D('-29.5870'), D('30.3930')),
    'CPT':  ('Epping Industria, Cape Town', 'Cape Town', 'WC', '7460', D('-33.9300'), D('18.5400')),
    'GQB':  ('Markman, Gqeberha', 'Gqeberha', 'EC', '6001', D('-33.8830'), D('25.6400')),
    'PLK':  ('Ladanna, Polokwane', 'Polokwane', 'LP', '0699', D('-23.8800'), D('29.4300')),
    'MBB':  ('Riverside Park, Mbombela', 'Mbombela', 'MP', '1200', D('-25.4400'), D('30.9800')),
    'MPM':  ('Porto de Maputo, Maputo', 'Maputo', 'MZ', '1100', D('-25.9692'), D('32.5732')),
    'BFN':  ('Hamilton, Bloemfontein', 'Bloemfontein', 'FS', '9301', D('-29.1000'), D('26.2000')),
    'SEC':  ('Secunda Industria, Secunda', 'Secunda', 'MP', '2302', D('-26.5500'), D('29.1667')),
    'EML':  ('Ferrobank, eMalahleni', 'eMalahleni', 'MP', '1035', D('-25.8713'), D('29.2332')),
    'TZN':  ('Letsitele Road, Tzaneen', 'Tzaneen', 'LP', '0850', D('-23.8332'), D('30.1635')),
    'BTV':  ('Silo Road, Bothaville', 'Bothaville', 'FS', '9660', D('-27.3900'), D('26.6170')),
}

# plazas: (route, name) pairs from core/management/commands/seed_toll_data.py.
DEMO_LANES = {
    'JHB-DBN': {'from': 'JHB', 'to': 'DBN', 'km': 570,  'hours': 9,  'plazas': [('N3', 'De Hoek'), ('N3', 'Wilge'), ('N3', 'Tugela'), ('N3', 'Mooi'), ('N3', 'Mariannhill')], 'heavy': 23900, 'rigid': None},
    'DBN-JHB': {'from': 'DBN', 'to': 'JHB', 'km': 570,  'hours': 9,  'plazas': [('N3', 'Mariannhill'), ('N3', 'Mooi'), ('N3', 'Tugela'), ('N3', 'Wilge'), ('N3', 'De Hoek')], 'heavy': 21900, 'rigid': None},
    'JHB-CPT': {'from': 'JHB', 'to': 'CPT', 'km': 1400, 'hours': 22, 'plazas': [('N1', 'Grasmere'), ('N1', 'Vaal'), ('N1', 'Verkeerdevlei'), ('N1', 'Huguenot')], 'heavy': 51900, 'rigid': None},
    'CPT-JHB': {'from': 'CPT', 'to': 'JHB', 'km': 1400, 'hours': 22, 'plazas': [('N1', 'Huguenot'), ('N1', 'Verkeerdevlei'), ('N1', 'Vaal'), ('N1', 'Grasmere')], 'heavy': 48300, 'rigid': None},
    'DBN-GQB': {'from': 'DBN', 'to': 'GQB', 'km': 915,  'hours': 15, 'plazas': [('N2', 'Oribi')], 'heavy': 13600, 'rigid': None},
    'JHB-PLK': {'from': 'JHB', 'to': 'PLK', 'km': 320,  'hours': 5,  'plazas': [('N1', 'Pumulani'), ('N1', 'Carousel'), ('N1', 'Kranskop'), ('N1', 'Nyl')], 'heavy': 4700, 'rigid': 3900},
    'MBB-MPM': {'from': 'MBB', 'to': 'MPM', 'km': 210,  'hours': 10, 'plazas': [('N4', 'Nkomazi')], 'heavy': 18400, 'rigid': None, 'cross_border': True},
    'JHB-BFN': {'from': 'JHB', 'to': 'BFN', 'km': 400,  'hours': 6,  'plazas': [('N1', 'Grasmere'), ('N1', 'Vaal'), ('N1', 'Verkeerdevlei')], 'heavy': 15900, 'rigid': None},
    'ISA-SEC': {'from': 'ISA', 'to': 'SEC', 'km': 135,  'hours': 3,  'plazas': [('N17', 'Gosforth'), ('N17', 'Dalpark'), ('N17', 'Leandra'), ('N17', 'Trichardt')], 'heavy': 9400, 'rigid': 6300},
    'JHB-EML': {'from': 'JHB', 'to': 'EML', 'km': 125,  'hours': 2,  'plazas': [], 'heavy': 8800, 'rigid': 5700},
    'TZN-JHB': {'from': 'TZN', 'to': 'JHB', 'km': 420,  'hours': 7,  'plazas': [('N1', 'Nyl'), ('N1', 'Kranskop'), ('N1', 'Carousel'), ('N1', 'Pumulani')], 'heavy': 18300, 'rigid': None},
    'BTV-JHB': {'from': 'BTV', 'to': 'JHB', 'km': 205,  'hours': 4,  'plazas': [], 'heavy': 9900, 'rigid': None},
    'DBN-PMB': {'from': 'DBN', 'to': 'PMB', 'km': 80,   'hours': 2,  'plazas': [('N3', 'Mariannhill')], 'heavy': None, 'rigid': 4600},
    'MID-PTA': {'from': 'MID', 'to': 'PTA', 'km': 45,   'hours': 1,  'plazas': [], 'heavy': None, 'rigid': 3600},
}
for _code, _lane in DEMO_LANES.items():
    _lane['code'] = _code
    _lane['pickup'] = _PLACES[_lane['from']]
    _lane['delivery'] = _PLACES[_lane['to']]

# Cross-border lane extras: clearing agent + Moamba (Mozambique) toll, billed on.
CROSS_BORDER_CHARGE = D('3350.00')
CROSS_BORDER_AGENT_FEE = D('2650.00')

# ---------------------------------------------------------------------------
# Customers — 31 fictional shippers.
# profile drives payment behaviour:
#   prompt  pays before due     steady  around due      slow  1-4 weeks late
#   chronic 1-3 months late, some never     stopped  was prompt, has stopped paying
# weight = relative load volume; season = months with heavy volume (others 0.35x)
# since_days = only ships after this many days ago (newly won accounts)
# until_days = stopped shipping this many days ago (churned account)
# quotes = spot customer (most loads come from quotes) vs contract.
# ---------------------------------------------------------------------------
DEMO_CUSTOMERS = [
    # Agriculture
    {'name': 'Bothaville Ridge Maize Co-op',      'slug': 'bothavilleridge',  'sector': 'Agriculture', 'city': 'Bothaville',       'state': 'Free State',     'contact': 'Hendrik du Plessis', 'terms': 'NET30', 'profile': 'steady',  'lanes': ['BTV-JHB'], 'weight': 5, 'season': (5, 6, 7, 8, 9), 'cargo': ['Bulk yellow maize', 'Bulk white maize'], 'quotes': False},
    {'name': 'Groenkloof Citrus Packhouse',       'slug': 'groenkloofcitrus', 'sector': 'Agriculture', 'city': 'Tzaneen',          'state': 'Limpopo',        'contact': 'Marelize Joubert',   'terms': 'NET30', 'profile': 'prompt',  'lanes': ['TZN-JHB'], 'weight': 5, 'season': (4, 5, 6, 7, 8, 9), 'cargo': ['Palletised oranges (cold chain)', 'Palletised lemons (cold chain)'], 'quotes': False},
    {'name': 'Kraalspruit Feed Mills',            'slug': 'kraalspruitfeeds', 'sector': 'Agriculture', 'city': 'Standerton',       'state': 'Mpumalanga',     'contact': 'Sizwe Mabena',       'terms': 'NET30', 'profile': 'slow',    'lanes': ['JHB-DBN', 'JHB-BFN'], 'weight': 4, 'cargo': ['Bagged animal feed', 'Bagged poultry feed'], 'quotes': True},
    {'name': 'Crocodile River Veg Growers',       'slug': 'crocriverveg',     'sector': 'Agriculture', 'city': 'Brits',            'state': 'North West',     'contact': 'Annatjie Smit',      'terms': 'NET30', 'profile': 'prompt',  'lanes': ['JHB-DBN', 'JHB-CPT'], 'weight': 4, 'cargo': ['Onions in 10kg pockets', 'Potatoes in 10kg pockets'], 'quotes': True},
    {'name': 'Lowveld Canopy Nuts',               'slug': 'lowveldcanopy',    'sector': 'Agriculture', 'city': 'Mbombela',         'state': 'Mpumalanga',     'contact': 'Grant Liebenberg',   'terms': 'NET30', 'profile': 'prompt',  'lanes': ['MBB-MPM'], 'weight': 4, 'cargo': ['Macadamia nut-in-shell for export', 'Containerised macadamias'], 'quotes': False},
    {'name': 'Nyoni Cane & Agri Supplies',        'slug': 'nyonicane',        'sector': 'Agriculture', 'city': 'KwaDukuza',        'state': 'KwaZulu-Natal',  'contact': 'Bheki Ngcobo',       'terms': 'NET60', 'profile': 'chronic', 'lanes': ['DBN-JHB', 'DBN-GQB'], 'weight': 3, 'cargo': ['Bagged fertiliser', 'Bagged sugar'], 'quotes': True},
    {'name': 'Kouebokkeveld Rooibos Traders',     'slug': 'kbvrooibos',       'sector': 'Agriculture', 'city': 'Ceres',            'state': 'Western Cape',   'contact': 'Elmarie Kotze',      'terms': 'NET30', 'profile': 'steady',  'lanes': ['CPT-JHB'], 'weight': 2, 'cargo': ['Bulk rooibos tea in bales'], 'quotes': True},
    # FMCG distribution
    {'name': 'Ubuntu Pantry Distributors',        'slug': 'ubuntupantry',     'sector': 'FMCG distribution', 'city': 'Germiston',  'state': 'Gauteng',        'contact': 'Palesa Mofokeng',    'terms': 'NET30', 'profile': 'steady',  'lanes': ['JHB-DBN', 'MID-PTA'], 'weight': 6, 'cargo': ['Palletised dry groceries', 'Mixed FMCG pallets'], 'quotes': False},
    {'name': 'Blue Crane Beverage Distributors',  'slug': 'bluecranebev',     'sector': 'FMCG distribution', 'city': 'Pinetown',   'state': 'KwaZulu-Natal',  'contact': 'Kavitha Naidoo',     'terms': 'NET30', 'profile': 'prompt',  'lanes': ['DBN-JHB', 'DBN-PMB'], 'weight': 5, 'cargo': ['Palletised soft drinks', 'Bottled water pallets'], 'quotes': False},
    {'name': 'Kaapse Kombuis Wholesale',          'slug': 'kaapsekombuis',    'sector': 'FMCG distribution', 'city': 'Cape Town',  'state': 'Western Cape',   'contact': 'Yusuf Isaacs',       'terms': 'NET30', 'profile': 'slow',    'lanes': ['CPT-JHB', 'JHB-CPT'], 'weight': 7, 'cargo': ['Canned goods and preserves', 'Palletised baking supplies'], 'quotes': False},
    {'name': 'Imbali Cash & Carry',               'slug': 'imbalicc',         'sector': 'FMCG distribution', 'city': 'Pietermaritzburg', 'state': 'KwaZulu-Natal', 'contact': 'Nokuthula Shezi',  'terms': 'NET30', 'profile': 'chronic', 'lanes': ['DBN-PMB'], 'weight': 2, 'cargo': ['Mixed grocery pallets', 'Bagged maize meal'], 'quotes': True},
    {'name': 'Kestrel Snacks & Confectionery',    'slug': 'kestrelsnacks',    'sector': 'FMCG distribution', 'city': 'Kempton Park', 'state': 'Gauteng',      'contact': 'Riana Oosthuizen',   'terms': 'NET30', 'profile': 'prompt',  'lanes': ['JHB-CPT', 'JHB-BFN'], 'weight': 5, 'cargo': ['Boxed confectionery', 'Palletised snack foods'], 'quotes': True},
    {'name': 'Suikerbos Household Goods',         'slug': 'suikerboshh',      'sector': 'FMCG distribution', 'city': 'Midrand',    'state': 'Gauteng',        'contact': 'Tebogo Masemola',    'terms': 'NET30', 'profile': 'prompt',  'lanes': ['MID-PTA', 'JHB-DBN'], 'weight': 3, 'cargo': ['Cleaning products', 'Paper and household goods'], 'quotes': False},
    {'name': 'Seaview Canned Foods Distribution', 'slug': 'seaviewcanned',    'sector': 'FMCG distribution', 'city': 'Gqeberha',   'state': 'Eastern Cape',   'contact': 'Warren Jacobs',      'terms': 'NET30', 'profile': 'slow',    'lanes': ['DBN-GQB'], 'weight': 4, 'cargo': ['Canned fish and vegetables', 'Palletised canned foods'], 'quotes': False},
    {'name': 'Riverbend Dairy Distributors',      'slug': 'riverbenddairy',   'sector': 'FMCG distribution', 'city': 'Howick',     'state': 'KwaZulu-Natal',  'contact': 'Craig McKenzie',     'terms': 'NET30', 'profile': 'stopped', 'lanes': ['DBN-JHB'], 'weight': 4, 'cargo': ['Long-life milk pallets', 'UHT dairy products'], 'quotes': False},
    # Building supplies
    {'name': 'Rietspruit Bricks & Blocks',        'slug': 'rietspruitbricks', 'sector': 'Building supplies', 'city': 'Brakpan',    'state': 'Gauteng',        'contact': 'Lebo Mashaba',       'terms': 'NET30', 'profile': 'steady',  'lanes': ['JHB-EML', 'ISA-SEC'], 'weight': 3, 'cargo': ['Clay stock bricks', 'Cement blocks on pallets'], 'quotes': True},
    {'name': 'Ironbark Roofing & Steel',          'slug': 'ironbarkroofing',  'sector': 'Building supplies', 'city': 'Vereeniging', 'state': 'Gauteng',       'contact': 'Deon Swanepoel',     'terms': 'NET30', 'profile': 'slow',    'lanes': ['JHB-BFN', 'JHB-DBN'], 'weight': 4, 'cargo': ['IBR roof sheeting', 'Steel purlins and lipped channel'], 'quotes': True},
    {'name': 'Rooihuis Timber & Hardware',        'slug': 'rooihuistimber',   'sector': 'Building supplies', 'city': 'Polokwane',  'state': 'Limpopo',        'contact': 'Johan Grobler',      'terms': 'NET30', 'profile': 'chronic', 'lanes': ['JHB-PLK'], 'weight': 4, 'cargo': ['Treated pine timber', 'Hardware and fittings'], 'quotes': False},
    {'name': 'Kgosi Building Supplies',           'slug': 'kgosibuild',       'sector': 'Building supplies', 'city': 'Pretoria',   'state': 'Gauteng',        'contact': 'Kgomotso Mahlangu',  'terms': 'NET30', 'profile': 'steady',  'lanes': ['MID-PTA', 'JHB-PLK'], 'weight': 2, 'cargo': ['Bagged cement', 'Plumbing and PVC pipe'], 'quotes': True},
    {'name': 'Bayside Tile & Sanitary',           'slug': 'baysidetile',      'sector': 'Building supplies', 'city': 'Durban',     'state': 'KwaZulu-Natal',  'contact': 'Ashwin Pillay',      'terms': 'NET30', 'profile': 'prompt',  'lanes': ['JHB-DBN'], 'weight': 3, 'cargo': ['Palletised floor tiles', 'Sanitaryware crates'], 'quotes': True},
    {'name': 'Mzansi Glass & Aluminium',          'slug': 'mzansiglass',      'sector': 'Building supplies', 'city': 'Midrand',    'state': 'Gauteng',        'contact': 'Sello Ramaphakela',  'terms': 'NET30', 'profile': 'slow',    'lanes': ['JHB-CPT'], 'weight': 3, 'cargo': ['Glass on A-frames', 'Aluminium extrusions'], 'quotes': True},
    {'name': 'Duinefontein Paint Wholesalers',    'slug': 'duinefonteinpaint', 'sector': 'Building supplies', 'city': 'Bellville', 'state': 'Western Cape',   'contact': 'Chantal Fortuin',    'terms': 'NET60', 'profile': 'steady',  'lanes': ['CPT-JHB'], 'weight': 4, 'cargo': ['Palletised paint drums', 'Coatings and thinners (non-hazardous)'], 'quotes': False},
    # Mining services
    {'name': 'Olifants Drill & Blast Services',   'slug': 'olifantsdrill',    'sector': 'Mining services', 'city': 'eMalahleni',   'state': 'Mpumalanga',     'contact': 'Vusi Mnisi',         'terms': 'NET60', 'profile': 'steady',  'lanes': ['JHB-EML'], 'weight': 3, 'cargo': ['Drill steel and bits', 'Mining consumables'], 'quotes': False},
    {'name': 'Waterberg Mining Consumables',      'slug': 'waterbergmining',  'sector': 'Mining services', 'city': 'Lephalale',    'state': 'Limpopo',        'contact': 'Francois Brits',     'terms': 'NET60', 'profile': 'slow',    'lanes': ['JHB-PLK'], 'weight': 2, 'cargo': ['Conveyor idlers', 'Mining PPE and consumables'], 'quotes': True},
    {'name': 'Steelpoort Conveyor Spares',        'slug': 'steelpoortconv',   'sector': 'Mining services', 'city': 'Burgersfort',  'state': 'Limpopo',        'contact': 'Precious Sekgobela', 'terms': 'NET30', 'profile': 'steady',  'lanes': ['ISA-SEC', 'JHB-EML'], 'weight': 2, 'cargo': ['Conveyor belting rolls', 'Pulleys and bearings'], 'quotes': True, 'since_days': 110},
    {'name': 'Witbank Coalfield Engineering',     'slug': 'witbankcoaleng',   'sector': 'Mining services', 'city': 'eMalahleni',   'state': 'Mpumalanga',     'contact': 'Gerhard Nel',        'terms': 'NET60', 'profile': 'chronic', 'lanes': ['JHB-EML'], 'weight': 3, 'cargo': ['Fabricated steel sections', 'Pump and valve assemblies'], 'quotes': True},
    {'name': 'Trichardt Valve & Pump Supplies',   'slug': 'trichardtvalve',   'sector': 'Mining services', 'city': 'Secunda',      'state': 'Mpumalanga',     'contact': 'Neo Motaung',        'terms': 'NET30', 'profile': 'prompt',  'lanes': ['ISA-SEC'], 'weight': 3, 'cargo': ['Industrial valves', 'Pump spares on pallets'], 'quotes': True},
    {'name': 'Evander Reagent Supplies',          'slug': 'evanderreagent',   'sector': 'Mining services', 'city': 'Evander',      'state': 'Mpumalanga',     'contact': 'Lindokuhle Mkhize',  'terms': 'NET30', 'profile': 'steady',  'lanes': ['ISA-SEC'], 'weight': 2, 'cargo': ['Bagged lime', 'Flotation reagents (non-hazardous)'], 'quotes': True, 'since_days': 75},
    # Cross-border
    {'name': 'Matola Bay Freight Forwarders',     'slug': 'matolabay',        'sector': 'Freight forwarding', 'city': 'Maputo',    'state': 'Maputo (MZ)',    'contact': 'Armando Tembe',      'terms': 'NET30', 'profile': 'slow',    'lanes': ['MBB-MPM'], 'weight': 3, 'cargo': ['Containerised citrus for export', 'Export pallets for Maputo port'], 'quotes': True},
    {'name': 'Komati Gateway Clearing',           'slug': 'komatigateway',    'sector': 'Freight forwarding', 'city': 'Komatipoort', 'state': 'Mpumalanga',   'contact': 'Sipho Mavuso',       'terms': 'NET30', 'profile': 'steady',  'lanes': ['MBB-MPM'], 'weight': 2, 'cargo': ['Bonded general cargo', 'Palletised transit cargo'], 'quotes': True, 'since_days': 150},
    # Churned account — inactive, history only
    {'name': 'Sandveld Potato Packers',           'slug': 'sandveldpotato',   'sector': 'Agriculture', 'city': "Lambert's Bay",    'state': 'Western Cape',   'contact': 'Wikus Visser',       'terms': 'NET30', 'profile': 'steady',  'lanes': ['CPT-JHB'], 'weight': 3, 'cargo': ['Potatoes in 10kg pockets'], 'quotes': True, 'until_days': 150},
]

DECLINE_REASONS = [
    'Price too high - went with a cheaper carrier',
    'Customer postponed the shipment',
    'Transit time did not suit their production schedule',
    'Awarded to their incumbent contract carrier',
    'Needed a flatbed, not a tautliner',
    'Budget cut for this quarter',
]

# Monthly cost structure for a 15-truck general-freight fleet doing about five
# loads per truck a month. Fixed costs sized so the business runs a 12-20% net
# margin on both the cash and the invoice basis.
MONTHLY_COSTS = {
    'insurance_per_truck': 6000,       # comprehensive + goods-in-transit, per truck a month
    'rent': 50000,                     # City Deep yard and office
    'telematics_per_unit': 465,
    'it': (6500, 8200),                # office, telecoms & IT support (range)
    'staff_count': 3,
    'staff_salaries': 84000,           # operations & admin staff
    'accounting': 9500,
    'driver_basic': (16000, 19500),    # basic salary range per driver
    'night_out': 480,                  # subsistence per night away
    'finance_heavy': 28000,            # instalment, financed truck-tractor
    'finance_light': 10900,            # instalment, financed box truck
    'empty_fuel_share': '0.18',        # empty running on top of loaded-leg fuel
    'empty_toll_share': '0.30',        # tolls on empty return legs
    'claims_per_month': 2,             # driver out-of-pocket claims
}

# Driver out-of-pocket claims: (what, min rand, max rand).
DRIVER_CLAIMS = [
    ('overnight secure truck stop', 180, 420),
    ('roadside puncture repair', 450, 950),
    ('weighbridge and parking fees', 120, 300),
    ('load straps replaced', 380, 760),
]

# Fictional vendors for the expense ledger.
VENDORS = {
    'fuel': 'Fleet fuel card',
    'tolls': 'SANRAL toll plazas (e-tag)',
    'service': 'Highway Truck Services (Pty) Ltd',
    'tyres': 'Tread Right Tyre Centre',
    'repair': 'Roadside Rescue Diesel Repairs',
    'insurance': 'Mopane Fleet Underwriters',
    'rent': 'City Deep Depot Properties',
    'telematics': 'TrakSure Telematics',
    'accounting': 'Ledgerline Accounting & Payroll',
    'it': 'Bytehaul IT Services',
    'finance': 'Asset finance instalment (demo lender)',
    'clearing': 'Lebombo Border Clearing Agents',
    'licensing': 'Vehicle licence renewals (RTMC)',
}
