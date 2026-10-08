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

# Official petrol (FIASA ULP, in force from 7 Oct 2026 00:01 SAST). 93 is published inland only.
PETROL_95_INLAND = 30.25
PETROL_93_INLAND = 29.88
PETROL_95_COASTAL = 29.38

SUPERLINK = {'id': 11, 'name': 'Superlink', 'capacity': 34, 'rated_burn_l_per_100km': 42}
TRI_AXLE = {'id': 12, 'name': 'Tri-axle', 'capacity': 30, 'rated_burn_l_per_100km': 38}
RIGID_KG = {'id': 13, 'name': '8 ton rigid', 'capacity': 8000, 'rated_burn_l_per_100km': 24}
SUSPECT_LOW_BURN = {'id': 14, 'name': 'Tautliner', 'capacity': 34, 'rated_burn_l_per_100km': 12}
SUSPECT_GVM = {'id': 15, 'name': 'Interlink', 'capacity': 56, 'rated_burn_l_per_100km': 45}
PETROL_RIGID = {'id': 16, 'name': '4 ton petrol rigid', 'capacity': 4, 'rated_burn_l_per_100km': 16}


def official(zone='INLAND', price=INLAND, **extra):
    out = {'zone': zone, 'mode': 'LIVE', 'own_price': None, 'own_set_at': None,
           'official_price': price, 'official_effective_from': EFFECTIVE, 'official_stale': False,
           'use_official': False, 'override_price': None}
    out.update(extra)
    return out


def petrol(zone='INLAND', price=PETROL_95_INLAND, grade='95', **extra):
    """Petrol / hybrid trucks: the same Official/Own resolution as diesel."""
    return official(zone=zone, price=price, fuel_type='Petrol', grade=grade, **extra)


def petrol_trip(**over):
    """A one-way 250 km trip on a petrol 4 t rigid, 3 t load."""
    inputs = base(vehicle=dict(PETROL_RIGID), load_kg=3000, operating_cost_per_km=8.0, price=6500.0,
                  diesel=petrol())
    inputs.update(over)
    return inputs


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
        'international': False,
        'include_empty_return': None,
        'settings': {'include_empty_return_default': True, 'empty_return_min_km': 300.0},
        'minimum_charge': None,
        'default_price_per_km': None,
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
    ('default_price_target_wins',
     'Company default R20,00/km on a one-way >= 300 km trip: the target price is higher, so it wins; '
     'the return-load alternative carries its own floor / target / default price.',
     long_trip(default_price_per_km=20.0)),
    ('default_price_rate_wins',
     'Company default R80,00/km is above the target price: default price = rate price rounded up.',
     long_trip(default_price_per_km=80.0)),
    ('default_price_no_rate_one_way_long',
     'No default price per km: default price = target price rounded up; alternative included.',
     long_trip(default_price_per_km=None)),
    ('tolls_no_plazas',
     'Toll lookup worked and found no plazas: R 0 known, line "No toll plazas on this route", no warning.',
     base(tolls={'one_way': 0.0, 'empty_return': None, 'lookup_failed': False, 'confirmed_none': False})),
    ('international_border_costs_missing',
     'International trip without border costs: border line null, border_costs_missing blocks.',
     long_trip(international=True, include_empty_return=False)),
    ('international_with_border_costs',
     'International trip with its border costs: complete floor.',
     long_trip(international=True, border_cost=6700.0, include_empty_return=False)),
    ('return_driver_nights_unknown',
     'Driver cost entered, no driving time, empty return included: the return driver line is unknown, '
     'driver_nights_unknown blocks.',
     long_trip(duration_minutes=None, driver={'allowance_per_night': 450.0, 'nights': None, 'amount': 900.0})),
    ('international_empty_return_crosses_back',
     'International one-way with the empty return: border costs again for crossing back.',
     long_trip(international=True, border_cost=6700.0)),
    ('international_round_trip_border',
     'International round trip: the loaded border line as given (no empty return).',
     long_trip(international=True, border_cost=6700.0, trip_type='ROUND_TRIP', price=60000.0)),
    ('default_price_incomplete_floor',
     'Floor incomplete (tolls unknown): no default price, alternative floor null too.',
     long_trip(default_price_per_km=20.0,
               tolls={'one_way': None, 'empty_return': None, 'lookup_failed': True, 'confirmed_none': False})),
    # Petrol (added 7 Oct 2026, after the diesel cases; those are unchanged).
    ('petrol_official_inland_95',
     'Petrol truck, LIVE company: official ULP 95 inland.',
     petrol_trip()),
    ('petrol_own_off',
     'Petrol OWN R27,00 set after the 7 Oct change: more than 3% under official 95 inland.',
     petrol_trip(diesel=petrol(mode='OWN', own_price=27.0, own_set_at='2026-10-07T06:30:00Z'))),
    ('petrol_missing',
     'Petrol truck, LIVE company, no official petrol price on record: blocks, no floor.',
     petrol_trip(diesel=petrol(price=None, official_effective_from=None))),
    ('petrol_coastal',
     'Coastal fleet, petrol truck: official ULP 95 coastal.',
     petrol_trip(diesel=petrol(zone='COASTAL', price=PETROL_95_COASTAL))),
    ('electric_own_missing',
     'Electric truck with no electricity cost set: blocks (no official price exists).',
     petrol_trip(diesel={'zone': 'INLAND', 'mode': 'OWN', 'own_price': None, 'own_set_at': None,
                         'official_price': None, 'official_effective_from': None, 'official_stale': False,
                         'use_official': False, 'override_price': None, 'fuel_type': 'Electric'})),
    # Border costs not on file for part of the route (added 8 Oct 2026).
    ('international_border_unknown_country',
     'SA -> Namibia -> Angola: the Namibia->Angola crossing has no figures on file; the route border total '
     'leaves it out, so the border lines are null and border_costs_missing blocks.',
     long_trip(international=True, border_cost=4840.0,
               border_costs_unknown={'countries': ['Angola'], 'crossings': ['Namibia→Angola'],
                                     'known': [{'label': 'SA→NA', 'amount': 4463.29},
                                               {'label': 'permit', 'amount': 376.71}]})),
    ('international_border_unknown_country_user_cost',
     'The same trip with the border costs entered by the user (border_cost_is_override): complete.',
     long_trip(international=True, border_cost=9800.0, border_cost_is_override=True,
               border_costs_unknown={'countries': ['Angola'], 'crossings': ['Namibia→Angola'],
                                     'known': [{'label': 'SA→NA', 'amount': 4463.29},
                                               {'label': 'permit', 'amount': 376.71}]})),
    # Both legs as the route calculation prices them (toll/border audit, 8 Oct 2026).
    ('round_trip_return_leg_tolls',
     'Round trip whose way back has its own plazas: tolls = one_way + return_leg (R1 043,48 + R812,61), '
     'not one_way x 2.',
     long_trip(trip_type='ROUND_TRIP', price=62000.0,
               tolls={'one_way': 1043.48, 'empty_return': None, 'return_leg': 812.61,
                      'lookup_failed': False, 'confirmed_none': False})),
    ('international_border_estimate_one_way',
     'One-way international trip, empty return: border R6 239,66 out of which R2 005,00 (agent) is an '
     'estimate; the way back empty is priced on its own (R2 381,71, all estimate).',
     long_trip(international=True, include_empty_return=True, border_cost=6239.66, border_estimate=2005.0,
               border_cost_empty_return=2381.71, border_estimate_empty_return=2381.71)),
    ('international_border_estimate_round_trip',
     'Round trip: border_cost and border_estimate are out + back (R9 618,08 of which R4 386,71 '
     'estimated = R2 005,00 + R2 381,71).',
     long_trip(trip_type='ROUND_TRIP', international=True, price=70000.0,
               border_cost=9618.08, border_estimate=4386.71,
               tolls={'one_way': 1043.48, 'empty_return': None, 'return_leg': 812.61,
                      'lookup_failed': False, 'confirmed_none': False})),
    ('international_border_agent_fee_entered',
     "The user entered their clearing agent's fee: the route calculation then has no estimate left "
     '(border_estimate 0) and the line says nothing about estimates.',
     long_trip(international=True, include_empty_return=False, border_cost=5734.66, border_estimate=0.0)),
    # User-applied driver nights (costing_inputs.driver_nights, 8 Oct 2026).
    ('driver_nights_applied',
     'User applied 3 nights out (the route suggests 0 on 7 h 20 min): driver line = 3 x R450,00; '
     'the empty return still adds its own suggested extra night.',
     long_trip(driver={'allowance_per_night': 450.0, 'nights': 3, 'amount': None})),
    ('driver_amount_wins_over_nights',
     'Applied nights AND a typed driver amount: the typed amount wins.',
     long_trip(driver={'allowance_per_night': 450.0, 'nights': 3, 'amount': 1000.0}, include_empty_return=False)),
]


# (name, changes_since_priced kwargs)
REOPEN_CASES = [
    ('costs_up', {'price': 20000.0, 'floor_then': 17200.0, 'floor_now': 18250.0,
                  'priced_at': '2026-09-02T08:00:00Z'}),
    ('costs_down', {'price': 31500.0, 'floor_then': 28000.0, 'floor_now': 26412.37,
                    'priced_at': '2026-09-10T08:00:00Z'}),
    ('unchanged', {'price': 12500.0, 'floor_then': 7430.63, 'floor_now': 7430.63,
                   'priced_at': '2026-10-07T08:00:00Z'}),
    ('floor_unknown_then', {'price': 12500.0, 'floor_then': None, 'floor_now': 7430.63,
                            'priced_at': None}),
    ('loss_making_then', {'price': 9000.0, 'floor_then': 9500.0, 'floor_now': 9800.55,
                          'priced_at': '2026-09-15T08:00:00Z'}),
]


# ---------------------------------------------------------------------------
# Tonnage quotes (rate per tonne): compute_tonnage() inputs. Added after every
# case above (those are unchanged); golden key `tonnage_cases`.
# ---------------------------------------------------------------------------

TAUTLINER = {'id': 17, 'name': 'Tautliner', 'capacity': 30, 'rated_burn_l_per_100km': 40}
NO_BURN = {'id': 18, 'name': 'Flatdeck', 'capacity': 32, 'rated_burn_l_per_100km': None}

_LANE_DROP = ('vehicle', 'load_kg', 'price', 'operating_cost_per_km', 'operating_cost_source')


def lane(**over):
    """The JHB -> DBN-ish lane (568,4 km, empty return by default) without the
    truck / load / price, for tonnage quotes."""
    out = {k: v for k, v in long_trip().items() if k not in _LANE_DROP}
    out.update(over)
    return out


def truck(vehicle, op):
    return {'vehicle': dict(vehicle), 'operating_cost_per_km': op, 'operating_cost_source': 'vehicle_default'}


FLEET = [truck(SUPERLINK, 16.0), truck(TRI_AXLE, 15.0), truck(TAUTLINER, 15.0)]
MIXED_FLEET = [truck(SUPERLINK, 16.0), truck(TAUTLINER, 15.0), truck(RIGID_KG, 8.0)]


def tonnage(**over):
    inputs = {'lane': lane(), 'trucks': [dict(t) for t in FLEET], 'tonnes_per_load': None, 'total_tonnes': None,
              'min_tonnes_per_load': None, 'vehicle_type_id': None, 'rate_per_tonne': None}
    inputs.update(over)
    return inputs


TONNAGE_CASES = [
    ('single_load_fits_one_truck',
     'One 30 t consignment, only a superlink in the fleet: one load, minimum = the 30 t planned.',
     tonnage(trucks=[truck(SUPERLINK, 16.0)], tonnes_per_load=30.0, rate_per_tonne=1250.0)),
    ('truck_unknown_three_eligible_safest',
     'Truck unknown, 30 t: superlink, tri-axle and tautliner can carry it; priced on the highest cost per tonne.',
     tonnage(tonnes_per_load=30.0)),
    ('chosen_truck',
     'Same consignment, the user picks the tri-axle: priced on it, the others shown with their margin.',
     tonnage(tonnes_per_load=30.0, vehicle_type_id=12, rate_per_tonne=1200.0)),
    ('partial_load_under_minimum',
     '12 t consignment with a 30 t minimum per load: charged for 30 t; the rigid is too small.',
     tonnage(trucks=[dict(t) for t in MIXED_FLEET], tonnes_per_load=12.0, min_tonnes_per_load=30.0,
             rate_per_tonne=1100.0)),
    ('volume_600t_mixed_fleet',
     '600 t contract over superlink, tautliner and 8 t rigid: loads per truck, safest basis (the rigid).',
     tonnage(trucks=[dict(t) for t in MIXED_FLEET], total_tonnes=600.0, rate_per_tonne=2700.0)),
    ('volume_partial_last_load',
     '100 t at 28 t a load on a chosen superlink: 4 loads, the last 16 t, charged at the 28 t minimum.',
     tonnage(total_tonnes=100.0, tonnes_per_load=28.0, vehicle_type_id=11, rate_per_tonne=1350.0)),
    ('return_load_booked',
     'Return load booked: no empty return in any truck\'s cost per load.',
     tonnage(lane=lane(include_empty_return=False), tonnes_per_load=30.0, rate_per_tonne=900.0)),
    ('diesel_missing_blocked',
     'No diesel price: every cost is unknown, diesel_missing blocks, no rate.',
     tonnage(lane=lane(diesel=official(price=None, official_effective_from=None)), tonnes_per_load=30.0)),
    ('rate_below_cost',
     'R 800/t is under the cost per tonne on the basis truck: rate_below_cost warns with the loss.',
     tonnage(tonnes_per_load=30.0, rate_per_tonne=800.0)),
    ('tonnes_exceed_payload',
     '40 t consignment, no truck carries it: split into loads on the safest truck.',
     tonnage(tonnes_per_load=40.0)),
    ('no_eligible_trucks',
     'Only a truck without fuel use on record: no eligible truck, blocks.',
     tonnage(trucks=[truck(NO_BURN, 15.0)], tonnes_per_load=30.0)),
    ('minimum_charge_lifts_rate',
     'Company minimum charge R 45 000 a load: default rate lifted to it; a lower rate blocks.',
     tonnage(lane=lane(minimum_charge=45000.0), tonnes_per_load=30.0, rate_per_tonne=1300.0)),
]
