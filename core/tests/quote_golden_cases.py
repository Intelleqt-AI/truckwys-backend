"""Inputs of the quote costing golden vectors (QUOTE-RULES.md §12).

The expected outputs in core/tests/fixtures/quote_golden.json are generated
from core.services.quote_costing.compute() by:

    QUOTE_GOLDEN_WRITE=1 python manage.py test core.tests.test_quote_costing

Web and mobile copy that JSON file verbatim and run their own calculators
over it; every line amount, the floor and the target price must match to the
cent, and the warning codes / severities / impact_zar must match exactly.
"""

INLAND = 32.7989           # FIASA 50ppm inland, in force from 7 Oct 2026 00:01 SAST
COASTAL = 31.9269
EFFECTIVE = '2026-10-06T22:01:00Z'   # 7 Oct 2026 00:01 SAST
PREVIOUS_EFFECTIVE = '2026-09-01T22:01:00Z'   # 2 Sep 2026 00:01 SAST

SUPERLINK = {'id': 11, 'name': 'Superlink', 'capacity': 34, 'rated_burn_l_per_100km': 42}
TRI_AXLE = {'id': 12, 'name': 'Tri-axle', 'capacity': 30, 'rated_burn_l_per_100km': 38}
RIGID_KG = {'id': 13, 'name': '8 ton rigid', 'capacity': 8000, 'rated_burn_l_per_100km': 24}
SUSPECT_LOW_BURN = {'id': 14, 'name': 'Tautliner', 'capacity': 34, 'rated_burn_l_per_100km': 12}
SUSPECT_GVM = {'id': 15, 'name': 'Interlink', 'capacity': 56, 'rated_burn_l_per_100km': 45}


def official(zone='INLAND', price=INLAND, **extra):
    out = {'zone': zone, 'mode': 'LIVE', 'own_price': None, 'own_set_at': None,
           'official_price': price, 'official_effective_from': EFFECTIVE, 'official_stale': False,
           'use_official': False, 'override_price': None}
    out.update(extra)
    return out


def base(**over):
    """A one-way 250 km inland trip on a superlink, everything known."""
    inputs = {
        'trip_type': 'ONE_WAY',
        'distance_km': 250.0,
        'distance_estimated': False,
        'distance_confirmed': False,
        'duration_minutes': 200,
        'load_kg': 20000,
        'vehicle': dict(SUPERLINK),
        'diesel': official(),
        'operating_cost_per_km': 16.0,
        'operating_cost_source': 'vehicle_default',
        'tolls': {'one_way': 412.17, 'empty_return': None, 'lookup_failed': False, 'confirmed_none': False},
        'driver': {'allowance_per_night': 450.0, 'nights': None, 'amount': None},
        'hours_per_day': 9.0,
        'border_cost': 0.0,
        'include_empty_return': None,
        'settings': {'include_empty_return_default': True, 'empty_return_min_km': 300.0},
        'minimum_charge': None,
        'target_margin_pct': 10.0,
        'price': 12500.0,
    }
    for key, value in over.items():
        inputs[key] = value
    return inputs


def long_trip(**over):
    """JHB -> DBN-ish: 568 km one way, 7 h 20 min driving."""
    inputs = base(distance_km=568.4, duration_minutes=440,
                  tolls={'one_way': 1043.48, 'empty_return': 812.61, 'lookup_failed': False,
                         'confirmed_none': False},
                  price=31500.0, load_kg=28000)
    inputs.update(over)
    return inputs


CASES = [
    ('official_inland_short_one_way',
     'LIVE company, official inland price, one-way under 300 km: no empty return.',
     base()),
    ('own_price_off_and_old',
     'OWN R30,00 set on 10 Sep: more than 3% under official and older than the 7 Oct change.',
     base(diesel=official(mode='OWN', own_price=30.0, own_set_at='2026-09-10T08:00:00Z'))),
    ('own_price_close_and_current',
     'OWN R32,50 set after the 7 Oct change: within 3%, no warnings.',
     base(diesel=official(mode='OWN', own_price=32.5, own_set_at='2026-10-07T06:30:00Z'))),
    ('own_price_use_official_on_quote',
     'OWN company, but this quote uses the official price (use_official action).',
     base(diesel=official(mode='OWN', own_price=30.0, own_set_at='2026-09-10T08:00:00Z',
                          use_official=True))),
    ('diesel_override_on_quote',
     'Per-quote diesel override R33,10 (source override).',
     base(diesel=official(override_price=33.1))),
    ('missing_diesel',
     'LIVE company and no official price on record: diesel_missing blocks, no floor.',
     base(diesel=official(price=None, official_effective_from=None))),
    ('stale_official_price',
     'Official price in force is from the previous period after a refresh attempt.',
     base(diesel=official(official_effective_from=PREVIOUS_EFFECTIVE, official_stale=True, price=31.2471))),
    ('coastal_zone',
     'Coastal fleet: coastal official price.',
     base(diesel=official(zone='COASTAL', price=COASTAL))),
    ('kg_capacity_rigid',
     'Vehicle capacity typed in kg (8000 -> 8 t), 5,5 t load.',
     base(vehicle=dict(RIGID_KG), load_kg=5500, operating_cost_per_km=8.0)),
    ('one_way_long_empty_return_default',
     'One-way 568,4 km (>= 300 km): empty return included by default, empty-class return tolls.',
     long_trip()),
    ('one_way_long_return_load_booked',
     'Same trip with "Return load booked": no empty return.',
     long_trip(include_empty_return=False)),
    ('one_way_long_company_default_off',
     'Company turned the empty-return default off.',
     long_trip(settings={'include_empty_return_default': False, 'empty_return_min_km': 300.0})),
    ('round_trip',
     'Round trip: both legs loaded, tolls both ways, nights for both legs.',
     long_trip(trip_type='ROUND_TRIP', price=52000.0)),
    ('tolls_unknown',
     'Toll lookup failed: tolls_unknown blocks and the floor is unknown.',
     base(tolls={'one_way': None, 'empty_return': None, 'lookup_failed': True, 'confirmed_none': False})),
    ('tolls_confirmed_none',
     'Toll lookup failed but the user confirmed there are no tolls.',
     base(tolls={'one_way': None, 'empty_return': None, 'lookup_failed': True, 'confirmed_none': True})),
    ('distance_estimated',
     'Routing fell back to a straight-line estimate: distance_estimated blocks.',
     base(distance_estimated=True)),
    ('distance_estimated_confirmed',
     'Estimated distance confirmed by the user.',
     base(distance_estimated=True, distance_confirmed=True)),
    ('minimum_charge',
     'Minimum charge R15 000: target price lifted to it, price below it blocks.',
     base(minimum_charge=15000.0)),
    ('suspect_truck_low_burn',
     '12 L/100km on a 34 t truck: truck_burn_suspect.',
     base(vehicle=dict(SUSPECT_LOW_BURN))),
    ('suspect_truck_gvm_capacity',
     '56 t "payload" (likely GVM): truck_burn_suspect.',
     base(vehicle=dict(SUSPECT_GVM))),
    ('no_vehicle',
     'No vehicle type: no_vehicle blocks, no fuel line.',
     base(vehicle=None, operating_cost_per_km=None)),
    ('overload',
     '36 t on a 34 t truck: overload blocks.',
     base(load_kg=36000)),
    ('driver_allowance_missing',
     'Two nights away and no allowance rate on record: blocks until entered.',
     long_trip(driver={'allowance_per_night': None, 'nights': None, 'amount': None},
               include_empty_return=False, duration_minutes=1100)),
    ('driver_amount_entered',
     'User entered the driver cost themselves.',
     long_trip(driver={'allowance_per_night': None, 'nights': None, 'amount': 1250.0},
               include_empty_return=False, duration_minutes=1100)),
    ('price_below_floor',
     'Price below the cost floor: warn with the loss.',
     base(price=6500.0)),
    ('border_costs',
     'Cross-border costs as their own line.',
     long_trip(border_cost=3875.5, include_empty_return=False)),
]
