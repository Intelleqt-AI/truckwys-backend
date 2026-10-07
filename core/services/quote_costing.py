"""THE quote costing rules (QUOTE-RULES.md §1, §3-§7, §10).

One authoritative calculation of a quote's cost lines, cost floor, target
price and warnings. Everything that prices or checks a quote uses it: the
pricing analysis, the AI price check, the send guard, the save-time snapshot
and POST /api/v1/quotes/cost-breakdown/. Web and mobile mirror `compute()`
and are checked against core/tests/fixtures/quote_golden.json.

Two layers:

* ``compute(inputs)`` — PURE. Plain JSON in, plain JSON out, no database,
  no clock. This is what the golden vectors pin and what clients mirror.
* ``build_inputs(...)`` / ``costing_for_payload`` / ``costing_for_quote`` —
  resolve those inputs from the database (company settings, vehicle type,
  official diesel price, operating cost, driver allowance) and call compute().

Arithmetic contract (so JS and Python agree to the cent): IEEE doubles, the
operations in the order written below, and ``cents(x) = floor(x * 100 + 0.5)
/ 100`` applied ONLY to each line total, the floor and the target price.
Litres and burn are never rounded internally.

    cap_t        = capacity / 1000 if capacity > 100 else capacity
    load_t       = load_kg / 1000
    ratio        = min(load_t / cap_t, 1)       (1 when cap_t or load unknown)
    burn_loaded  = rated * (0.70 + 0.30 * ratio)            L/100km
    burn_empty   = rated * 0.70
    km_loaded    = distance_km * legs_loaded     (legs_loaded = 2 for a round trip)
    km_empty     = distance_km                   (one-way with empty return) else 0
    litres_loaded= km_loaded * burn_loaded / 100
    litres_empty = km_empty * burn_empty / 100
    fuel         = cents(litres_loaded * price)
    fuel_return  = cents(litres_empty * price)
    operating    = cents(km_loaded * operating_cost_per_km)
    operating_return = cents(km_empty * operating_cost_per_km)
    tolls        = cents(tolls_one_way * legs_loaded)
    tolls_return = cents(tolls_empty_return ?? tolls_one_way)
    nights(h)    = max(ceil(h / hours_per_day) - 1, 0)       h = driving hours
    driver       = cents(nights_loaded * allowance_per_night)  or the user's amount
    driver_return= cents((nights(2h) - nights(h)) * allowance_per_night)
    border       = cents(border_cost)
    floor        = cents(sum of line amounts)   (null if any required line is unknown)
    target_price = max(cents(floor / (1 - target)), minimum_charge or 0)
    margin       = price - floor;  margin_pct = margin / price * 100
"""
import math
from datetime import datetime, timezone as dt_timezone

VERSION = 'qc-1'

LOADED_BASE = 0.70
LOADED_SLOPE = 0.30
EMPTY_FACTOR = 0.70
OWN_OFF_THRESHOLD = 0.03          # |own - official| / official above this -> diesel_own_off
KG_CAPACITY_THRESHOLD = 100       # capacity above this is kilograms
SUSPECT_BURN_MIN = 20.0           # L/100km ...
SUSPECT_BURN_MIN_CAPACITY_T = 8.0  # ... for a truck of at least this payload
SUSPECT_CAPACITY_MAX_T = 40.0     # payload above this is probably GVM
DEFAULT_HOURS_PER_DAY = 9.0
DEFAULT_EMPTY_RETURN_MIN_KM = 300.0

ZONES = ('INLAND', 'COASTAL')

LINE_LABELS = {
    'fuel': 'Fuel',
    'operating': 'Operating costs',
    'tolls': 'Tolls',
    'driver': 'Driver nights out',
    'border': 'Border fees',
    'fuel_return': 'Fuel, empty return',
    'operating_return': 'Operating costs, empty return',
    'tolls_return': 'Tolls, empty return',
    'driver_return': 'Driver nights, empty return',
}

_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


# ---------------------------------------------------------------------------
# Small helpers (mirrored by the clients)
# ---------------------------------------------------------------------------

def cents(x):
    """Half-up to the cent on the double, as JS Math.round(x * 100) / 100."""
    if x is None:
        return None
    return math.floor(x * 100 + 0.5) / 100


def _num(v):
    if v is None or v == '':
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pos(v):
    out = _num(v)
    return out if out is not None and out > 0 else None


def capacity_tonnes(raw):
    """§3: values > 100 are kg. None when unknown or not positive."""
    v = _pos(raw)
    if v is None:
        return None
    return v / 1000 if v > KG_CAPACITY_THRESHOLD else v


def nights_away(hours, hours_per_day=DEFAULT_HOURS_PER_DAY):
    """Nights slept away for `hours` of driving (None when unknown)."""
    if hours is None or hours <= 0:
        return None if hours is None else 0
    return max(math.ceil(hours / hours_per_day) - 1, 0)


def parse_dt(value):
    """ISO string / datetime -> aware datetime (UTC when naive), or None."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


def iso(dt):
    dt = parse_dt(dt)
    return dt.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


def sa_date(value):
    """'7 Oct 2026' in SAST."""
    dt = parse_dt(value)
    if dt is None:
        return None
    from zoneinfo import ZoneInfo
    d = dt.astimezone(ZoneInfo('Africa/Johannesburg')).date()
    return f'{d.day} {_MONTHS[d.month - 1]} {d.year}'


def fmt_num(v, dp=0):
    """SA style: space thousands, comma decimals ('1 050', '32,80')."""
    v = float(v)
    txt = f'{abs(v):,.{dp}f}'.replace(',', ' ').replace('.', ',')
    return ('−' if v < 0 and txt.strip('0, ') else '') + txt


def fmt_rand(v, dp=0):
    """'R 32,80' / 'R 1 050' (whole rand half-up when dp=0)."""
    v = float(v)
    if dp == 0:
        v = math.floor(abs(v) + 0.5) * (1 if v >= 0 else -1)
    sign = '−' if v < 0 and abs(v) >= (0.5 if dp == 0 else 0.005) else ''
    return f'{sign}R {fmt_num(abs(v), dp)}'


def warning(code, severity, title, detail, impact_zar=None, actions=(), **extra):
    out = {'code': code, 'severity': severity, 'title': title, 'detail': detail,
           'impact_zar': impact_zar, 'actions': [{'id': a, 'label': ACTION_LABELS[a]} for a in actions]}
    out.update(extra)
    return out


ACTION_LABELS = {
    'use_official': 'Use official price',
    'update_own': 'Update my price',
    'retry_diesel': 'Try again',
    'choose_vehicle': 'Choose truck',
    'add_vehicle': 'Add a truck',
    'edit_vehicle': 'Check truck',
    'enter_tolls': 'Enter tolls',
    'confirm_no_tolls': 'No tolls on this route',
    'recalculate_route': 'Recalculate route',
    'confirm_distance': 'Distance is right',
    'enter_route': 'Add route',
    'enter_driver_cost': 'Enter driver cost',
    'update_allowance': 'Set allowance',
    'use_minimum': 'Use minimum charge',
    'reprice': 'Re-price',
    'keep_price': 'Keep price',
    'enter_weight': 'Enter weight',
}


# ---------------------------------------------------------------------------
# Diesel (§1)
# ---------------------------------------------------------------------------

def resolve_diesel(d):
    """Which diesel price this quote uses. Pure.

    d: {zone, mode LIVE|OWN, own_price, own_set_at, official_price,
        official_effective_from, official_stale, use_official, override_price}
    -> {price, source own|official|override|missing, zone, mode, own_price,
        own_set_at, official_price, official_effective_from, official_stale}
    """
    d = d or {}
    zone = str(d.get('zone') or 'INLAND').upper()
    zone = zone if zone in ZONES else 'INLAND'
    mode = str(d.get('mode') or 'LIVE').upper()
    own = _pos(d.get('own_price'))
    if mode != 'OWN' or own is None:
        mode = 'LIVE' if own is None else mode   # own price empty => LIVE
    official = _pos(d.get('official_price'))
    override = _pos(d.get('override_price'))
    out = {'zone': zone, 'mode': mode, 'own_price': own, 'own_set_at': iso(d.get('own_set_at')),
           'official_price': official, 'official_effective_from': iso(d.get('official_effective_from')),
           'official_stale': bool(d.get('official_stale')) if official is not None else False}
    if override is not None:
        price, source = override, 'override'
    elif mode == 'OWN' and not d.get('use_official'):
        price, source = own, 'own'
    elif official is not None:
        price, source = official, 'official'
    else:
        price, source = None, 'missing'
    out.update({'price': price, 'source': source})
    return out


def diesel_warnings(diesel, litres_total=None):
    """§1 warnings for a resolved diesel price. Pure."""
    out = []
    zone_txt = 'coastal' if diesel['zone'] == 'COASTAL' else 'inland'
    if diesel['source'] == 'missing':
        out.append(warning('diesel_missing', 'block', 'No diesel price available',
                           'No official price on record; set your own in settings.',
                           actions=('retry_diesel', 'update_own')))
        return out
    if diesel['source'] == 'own' and diesel['official_price']:
        own, official = diesel['own_price'], diesel['official_price']
        if abs(own - official) / official > OWN_OFF_THRESHOLD:
            impact = cents((own - official) * litres_total) if litres_total is not None else None
            more_less = ''
            if impact is not None:
                more_less = f': {fmt_rand(abs(impact))} {"more" if impact > 0 else "less"} on this quote'
            out.append(warning(
                'diesel_own_off', 'warn', 'Your diesel price differs from official',
                f'Yours {fmt_rand(own, 2)}/L, official {fmt_rand(official, 2)}/L ({zone_txt}){more_less}.',
                impact_zar=impact, actions=('use_official', 'update_own'),
                own_price=own, official_price=official))
        set_at, eff = parse_dt(diesel['own_set_at']), parse_dt(diesel['official_effective_from'])
        if set_at is not None and eff is not None and set_at < eff:
            out.append(warning(
                'diesel_own_old', 'warn', 'Your diesel price predates the latest change',
                f'Set {sa_date(set_at)}; official price changed {sa_date(eff)}.',
                actions=('update_own', 'use_official')))
    if diesel['source'] == 'official' and diesel['official_stale']:
        eff = diesel['official_effective_from']
        out.append(warning(
            'diesel_stale', 'warn', 'Official diesel price may be out of date',
            f'Latest on record is from {sa_date(eff)}.' if eff else 'This month\'s price is not loaded yet.',
            actions=('retry_diesel', 'update_own')))
    return out


# ---------------------------------------------------------------------------
# The calculation
# ---------------------------------------------------------------------------

def _line(key, leg, amount, basis, **extra):
    out = {'key': key, 'label': LINE_LABELS[key], 'leg': leg, 'amount': amount, 'basis': basis}
    out.update(extra)
    return out


def compute(inputs):
    """The cost lines, floor, target price and warnings for one quote. PURE.

    inputs (all optional; unknown = null):
      trip_type            'ONE_WAY' | 'ROUND_TRIP'
      distance_km          one-way route distance
      distance_estimated   routing fell back to a straight-line estimate
      distance_confirmed   the user confirmed that distance
      duration_minutes     one-way driving time
      load_kg
      vehicle              {id, name, capacity, rated_burn_l_per_100km} | null
      diesel               see resolve_diesel()
      operating_cost_per_km, operating_cost_source
      tolls                {one_way, empty_return, lookup_failed, confirmed_none}
      driver               {allowance_per_night, nights, amount}
      hours_per_day        driving hours per day (9)
      border_cost
      include_empty_return null = company default rule; false = return load booked
      settings             {include_empty_return_default (true), empty_return_min_km (300)}
      minimum_charge
      target_margin_pct
      price                the quoted price excl. VAT, or null
    """
    inputs = inputs or {}
    warnings = []

    # --- trip ---
    round_trip = str(inputs.get('trip_type') or 'ONE_WAY').upper() == 'ROUND_TRIP'
    legs_loaded = 2 if round_trip else 1
    distance = _pos(inputs.get('distance_km'))
    settings = inputs.get('settings') or {}
    default_on = settings.get('include_empty_return_default')
    default_on = True if default_on is None else bool(default_on)
    min_km = _num(settings.get('empty_return_min_km'))
    min_km = DEFAULT_EMPTY_RETURN_MIN_KM if min_km is None else min_km
    requested = inputs.get('include_empty_return')
    if round_trip or distance is None:
        empty_return = False
    elif requested is not None:
        empty_return = bool(requested)
    else:
        empty_return = default_on and distance >= min_km
    km_loaded = distance * legs_loaded if distance is not None else None
    km_empty = distance if empty_return else 0.0

    if distance is None:
        warnings.append(warning('distance_missing', 'block', 'Route distance is missing',
                                'Add collection and delivery to work out the route.',
                                actions=('enter_route',)))
    elif inputs.get('distance_estimated') and not inputs.get('distance_confirmed'):
        warnings.append(warning('distance_estimated', 'block', 'Distance is a straight-line estimate',
                                f'{fmt_num(distance)} km was estimated; recalculate or confirm it.',
                                actions=('recalculate_route', 'confirm_distance')))

    # --- truck (§3) ---
    vehicle = inputs.get('vehicle') or None
    cap_t = load_t = ratio = rated = burn_loaded = burn_empty = None
    load_kg = _num(inputs.get('load_kg'))
    if vehicle is None:
        warnings.append(warning('no_vehicle', 'block', 'Choose a truck for this quote',
                                'Every quote is priced on one of your vehicle types.',
                                actions=('choose_vehicle', 'add_vehicle')))
    else:
        cap_t = capacity_tonnes(vehicle.get('capacity'))
        rated = _pos(vehicle.get('rated_burn_l_per_100km'))
        load_t = load_kg / 1000 if load_kg is not None and load_kg >= 0 else None
        if cap_t is not None and load_t is not None:
            ratio = min(load_t / cap_t, 1)
        else:
            ratio = 1   # conservative: full-load burn when either is unknown
        if load_t is None:
            warnings.append(warning('load_missing', 'warn', 'Load weight is missing',
                                    'Fuel is priced as a full load until you enter it.',
                                    actions=('enter_weight',)))
        if rated is not None:
            burn_loaded = rated * (LOADED_BASE + LOADED_SLOPE * ratio)
            burn_empty = rated * EMPTY_FACTOR
        else:
            warnings.append(warning('truck_burn_missing', 'block', 'Truck fuel use is missing',
                                    f'Set litres per 100 km for {vehicle.get("name") or "this truck"}.',
                                    actions=('edit_vehicle',)))
        if cap_t is not None and load_t is not None and load_t > cap_t:
            warnings.append(warning('overload', 'block', 'Load is heavier than the truck',
                                    f'{fmt_num(load_t, 1)} t on a {fmt_num(cap_t, 1)} t truck.',
                                    actions=('choose_vehicle',)))
        if cap_t is not None and ((rated is not None and rated < SUSPECT_BURN_MIN
                                   and cap_t >= SUSPECT_BURN_MIN_CAPACITY_T) or cap_t > SUSPECT_CAPACITY_MAX_T):
            what = (f'{fmt_num(cap_t, 1)} t payload looks like GVM' if cap_t > SUSPECT_CAPACITY_MAX_T
                    else f'{fmt_num(rated, 1)} L/100km for {fmt_num(cap_t, 1)} t looks low')
            warnings.append(warning('truck_burn_suspect', 'warn', 'Check this truck\'s fuel or capacity',
                                    f'{what}.', actions=('edit_vehicle',)))

    litres_loaded = km_loaded * burn_loaded / 100 if km_loaded is not None and burn_loaded is not None else None
    litres_empty = (km_empty * burn_empty / 100 if empty_return and burn_empty is not None
                    else (0.0 if not empty_return else None))
    litres_total = (litres_loaded + litres_empty
                    if litres_loaded is not None and litres_empty is not None else None)

    # --- diesel (§1) ---
    diesel = resolve_diesel(inputs.get('diesel'))
    warnings.extend(diesel_warnings(diesel, litres_total))
    price_l = diesel['price']

    lines = []
    complete = True

    def add(key, leg, amount, basis, required=True, **extra):
        nonlocal complete
        if amount is None and required:
            complete = False
        lines.append(_line(key, leg, amount, basis, **extra))

    # --- fuel (§4) ---
    fuel_amt = cents(litres_loaded * price_l) if litres_loaded is not None and price_l is not None else None
    add('fuel', 'loaded', fuel_amt,
        f'{fmt_num(km_loaded or 0)} km at {fmt_num(burn_loaded, 1) if burn_loaded else "?"} L/100km'
        + (f' × {fmt_rand(price_l, 2)}/L' if price_l else ''),
        litres=litres_loaded, burn_l_per_100km=burn_loaded, price_per_litre=price_l, km=km_loaded)

    # --- operating cost (§6) ---
    op = _num(inputs.get('operating_cost_per_km'))
    op_amt = cents(km_loaded * op) if km_loaded is not None and op is not None else None
    add('operating', 'loaded', op_amt,
        f'{fmt_num(km_loaded or 0)} km × {fmt_rand(op, 2) if op is not None else "?"}/km',
        rate_per_km=op, km=km_loaded, source=inputs.get('operating_cost_source'))

    # --- tolls (§6) ---
    tolls = inputs.get('tolls') or {}
    toll_one_way = _num(tolls.get('one_way'))
    tolls_unknown = toll_one_way is None or bool(tolls.get('lookup_failed'))
    if tolls_unknown and tolls.get('confirmed_none'):
        toll_one_way, tolls_unknown = 0.0, False
    if tolls_unknown:
        toll_one_way = None
        warnings.append(warning('tolls_unknown', 'block', 'Tolls could not be worked out',
                                'Enter the tolls, or confirm there are none on this route.',
                                actions=('enter_tolls', 'confirm_no_tolls')))
    toll_amt = cents(toll_one_way * legs_loaded) if toll_one_way is not None else None
    add('tolls', 'loaded', toll_amt,
        'Unknown' if toll_amt is None else (f'{fmt_rand(toll_one_way, 2)} × 2 legs' if round_trip
                                             else f'{fmt_rand(toll_one_way, 2)} one way'),
        one_way=toll_one_way, legs=legs_loaded)

    # --- driver nights (§6) ---
    driver = inputs.get('driver') or {}
    hpd = _pos(inputs.get('hours_per_day')) or DEFAULT_HOURS_PER_DAY
    minutes = _pos(inputs.get('duration_minutes'))
    hours = minutes / 60 if minutes is not None else None
    rate = _pos(driver.get('allowance_per_night'))
    nights_one = nights_away(hours, hpd)
    nights_two = nights_away(hours * 2, hpd) if hours is not None else None
    suggested_nights = nights_two if round_trip else nights_one
    nights_override = _num(driver.get('nights'))
    nights = int(nights_override) if nights_override is not None and nights_override >= 0 else suggested_nights
    user_amount = _num(driver.get('amount'))
    suggested = cents(nights * rate) if nights is not None and rate is not None else (
        0.0 if nights == 0 else None)
    if user_amount is not None and user_amount >= 0:
        drv_amt, drv_source = cents(user_amount), 'user'
    else:
        drv_amt, drv_source = suggested, 'suggested'
    if drv_amt is None:
        if nights is None:
            warnings.append(warning('driver_nights_unknown', 'block', 'Driving time is unknown',
                                    'Enter the driver cost, or recalculate the route.',
                                    actions=('enter_driver_cost', 'recalculate_route')))
        else:
            warnings.append(warning('driver_allowance_missing', 'block', 'Driver nights have no allowance',
                                    f'{nights} night{"s" if nights != 1 else ""} away: enter the driver cost '
                                    'or set a rate.', actions=('enter_driver_cost', 'update_allowance')))
    add('driver', 'loaded', drv_amt,
        ('Your figure' if drv_source == 'user' else
         f'{nights} night{"s" if nights != 1 else ""} × {fmt_rand(rate, 2)}' if rate is not None and nights
         else 'No night away' if nights == 0 else 'Unknown'),
        nights=nights, suggested_nights=suggested_nights, rate_per_night=rate, suggested=suggested,
        source=drv_source)

    # --- border ---
    border = _num(inputs.get('border_cost'))
    if border is not None and border > 0:
        add('border', 'loaded', cents(border), 'Border, permit and non-SA toll costs')

    # --- empty return (§5) ---
    return_nights = None
    if empty_return:
        fr_amt = cents(litres_empty * price_l) if litres_empty is not None and price_l is not None else None
        add('fuel_return', 'empty_return', fr_amt,
            f'{fmt_num(km_empty)} km empty at {fmt_num(burn_empty, 1) if burn_empty else "?"} L/100km'
            + (f' × {fmt_rand(price_l, 2)}/L' if price_l else ''),
            litres=litres_empty, burn_l_per_100km=burn_empty, price_per_litre=price_l, km=km_empty)
        add('operating_return', 'empty_return', cents(km_empty * op) if op is not None else None,
            f'{fmt_num(km_empty)} km × {fmt_rand(op, 2) if op is not None else "?"}/km',
            rate_per_km=op, km=km_empty, source=inputs.get('operating_cost_source'))
        ret_toll = _num(tolls.get('empty_return'))
        if ret_toll is None:
            ret_toll = toll_one_way
        add('tolls_return', 'empty_return', cents(ret_toll) if ret_toll is not None else None,
            'Unknown' if ret_toll is None else f'{fmt_rand(ret_toll, 2)} home empty', one_way=ret_toll)
        return_nights = (nights_two - nights_one) if nights_one is not None else None
        dr_amt = (cents(return_nights * rate) if return_nights is not None and rate is not None
                  else (0.0 if return_nights == 0 else None))
        if dr_amt is None and drv_amt is not None:
            warnings.append(warning('driver_allowance_missing', 'block', 'Driver nights have no allowance',
                                    f'{return_nights} extra night{"s" if return_nights != 1 else ""} '
                                    'coming home: set a rate per night.', actions=('update_allowance',)))
        add('driver_return', 'empty_return', dr_amt,
            (f'{return_nights} extra night{"s" if return_nights != 1 else ""} × {fmt_rand(rate, 2)}'
             if rate is not None and return_nights else 'No extra night' if return_nights == 0 else 'Unknown'),
            nights=return_nights, rate_per_night=rate)

    if op is None and distance is not None:
        complete = False
    known = [ln['amount'] for ln in lines if ln['amount'] is not None]
    floor_known = cents(sum(known)) if known else 0.0
    floor = floor_known if complete else None

    # --- price and margin (§7) ---
    target = _num(inputs.get('target_margin_pct'))
    minimum = _pos(inputs.get('minimum_charge'))
    target_price = None
    if floor is not None and target is not None and target < 100:
        target_price = cents(floor / (1 - target / 100))
        if minimum is not None and minimum > target_price:
            target_price = minimum
    price = _pos(inputs.get('price'))
    margin = margin_pct = None
    if price is not None and floor is not None:
        margin = cents(price - floor)
        margin_pct = (price - floor) / price * 100
        if price < floor:
            warnings.append(warning('below_floor', 'warn', 'Price is below your costs',
                                    f'This trip loses {fmt_rand(floor - price)}.', impact_zar=cents(price - floor)))
    if price is not None and minimum is not None and price < minimum:
        warnings.append(warning('below_minimum_charge', 'block', 'Price is below your minimum charge',
                                f'Your minimum charge is {fmt_rand(minimum)}.', impact_zar=cents(minimum - price),
                                actions=('use_minimum',)))

    blocking = [w['code'] for w in warnings if w['severity'] == 'block']
    return {
        'version': VERSION,
        'trip': {'type': 'ROUND_TRIP' if round_trip else 'ONE_WAY', 'legs_loaded': legs_loaded,
                 'distance_km': distance, 'empty_return_included': empty_return,
                 'empty_return_default': (not round_trip and distance is not None and default_on
                                          and distance >= min_km),
                 'km_loaded': km_loaded, 'km_empty': km_empty,
                 'km_driven': (km_loaded + km_empty) if km_loaded is not None else None,
                 'hours_one_way': hours, 'return_nights': return_nights},
        'vehicle': None if vehicle is None else {
            'id': vehicle.get('id'), 'name': vehicle.get('name'), 'capacity_t': cap_t, 'load_t': load_t,
            'load_ratio': ratio, 'rated_burn_l_per_100km': rated,
            'burn_loaded_l_per_100km': burn_loaded, 'burn_empty_l_per_100km': burn_empty},
        'diesel': diesel,
        'litres': {'loaded': litres_loaded, 'empty_return': litres_empty, 'total': litres_total},
        'lines': lines,
        'floor': floor,
        'floor_known': floor_known,
        'floor_complete': floor is not None,
        'target_margin_pct': target,
        'target_price': target_price,
        'minimum_charge': minimum,
        'price': price,
        'margin': margin,
        'margin_pct': margin_pct,
        'warnings': warnings,
        'blocking': blocking,
        'can_send': not blocking,
    }


# ===========================================================================
# Database layer: resolve compute()'s inputs for a company / payload / quote.
# ===========================================================================

def _truthy(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def vehicle_input(vt):
    if vt is None:
        return None
    return {'id': vt.id, 'name': vt.name, 'capacity': _num(vt.capacity),
            'rated_burn_l_per_100km': _num(vt.fuel_consumption_l_per_100km)}


def suggest_vehicle(company, load_kg):
    """§3: the smallest-capacity visible vehicle type that carries the load
    (tie: lowest rated burn), else None."""
    from core.services.vehicle_types import visible_vehicle_types_queryset
    load_t = (_num(load_kg) or 0) / 1000
    best = None
    for vt in visible_vehicle_types_queryset(company).filter(active=True):
        cap = capacity_tonnes(vt.capacity)
        burn = _pos(vt.fuel_consumption_l_per_100km)
        if cap is None or burn is None or cap < load_t:
            continue
        key = (cap, burn, vt.id)
        if best is None or key < best[0]:
            best = (key, vt)
    return best[1] if best else None


def resolve_vehicle(company, *, vehicle_type_id=None, name=None, load_kg=None, suggest=True):
    """(VehicleType | None, how): 'selected' (id), 'by_name', 'suggested' or
    None. Only types this company can see (never another tenant's)."""
    from core.services.vehicle_types import visible_vehicle_types_queryset
    qs = visible_vehicle_types_queryset(company)
    vt_id = _num(vehicle_type_id)
    if vt_id:
        row = qs.filter(id=int(vt_id)).first()
        if row is not None:
            return row, 'selected'
    name = str(name or '').strip()
    if name:
        rows = list(qs.filter(name__iexact=name))
        own = [r for r in rows if company is not None and r.company_id == getattr(company, 'id', None)]
        row = (own or rows or [None])[0]
        if row is not None:
            return row, 'by_name'
    if suggest:
        row = suggest_vehicle(company, load_kg)
        if row is not None:
            return row, 'suggested'
    return None, None


def fleet_reference_class(company):
    """The vehicle class a company-wide operating cost figure describes: the
    most common class among the company's trucks (else its own vehicle
    types), or None when it has neither."""
    from collections import Counter
    from core.services.pricing_analysis import vehicle_class
    if company is None:
        return None
    try:
        from core.models import Vehicle, VehicleType
        counts = Counter(vehicle_class(v.vehicle_type) for v in
                         Vehicle.objects.filter(company=company, vehicle_type__isnull=False)
                         .select_related('vehicle_type'))
        if not counts:
            counts = Counter(vehicle_class(vt) for vt in VehicleType.objects.filter(company=company))
        return counts.most_common(1)[0][0] if counts else None
    except Exception:
        return None


def operating_cost_for(company, vt):
    """§6: operating cost per km for THIS truck's class: {value, source,
    class, class_label, scaled_from}. A company-wide figure (setting or
    actuals) is scaled by class default ratio when the fleet's main class
    differs; with no company figure, the class default."""
    from core.services.pricing_analysis import (OPERATING_COST_CLASSES, _class_default, company_operating_cost,
                                                vehicle_class)
    cls = vehicle_class(vt)
    out = {'class': cls, 'class_label': OPERATING_COST_CLASSES[cls][0], 'scaled_from': None}
    setting = _pos(getattr(company, 'operating_cost_per_km', None))
    actual = company_operating_cost(company) if company is not None else {}
    figure, source = (setting, 'company_setting') if setting else (_pos(actual.get('value')), 'company_actuals')
    if figure:
        ref = fleet_reference_class(company)
        if ref and ref != cls:
            figure = figure * _class_default(cls)[0] / _class_default(ref)[0]
            out['scaled_from'] = ref
        out.update({'value': round(figure, 2), 'source': source})
        return out
    out.update({'value': _class_default(cls)[0], 'source': 'vehicle_default'})
    return out


def driver_rate(company, today):
    """(rate per night | None, source): company setting first (what the fleet
    pays), else the approved allowance in force."""
    from core.services.quote_ai_pricing import DRIVER_RATE_MAX_PER_DAY, stored_allowance
    own = _pos(getattr(company, 'driver_allowance_per_night', None))
    if own is not None and own <= DRIVER_RATE_MAX_PER_DAY:
        return own, 'company_setting'
    allowance = stored_allowance(today)
    rate = _pos((allowance or {}).get('rate_per_night'))
    if rate is not None and rate <= DRIVER_RATE_MAX_PER_DAY:
        return rate, 'approved_allowance'
    return None, None


def target_margin(company):
    from core.services.pricing_analysis import MARGIN_TARGET_RANGE
    t = _num(getattr(company, 'margin_target_pct', None)) or 10.0
    return min(max(t, float(MARGIN_TARGET_RANGE[0])), float(MARGIN_TARGET_RANGE[1]))


def build_inputs(payload, company, now=None, *, diesel_override=None):
    """compute() inputs from a pricing payload (the builder's fields, the
    pricing-analysis payload and POST /quotes/cost-breakdown/ share it).

    Payload: trip_type | legs, one_way_distance_km | distance_km (total for
    the legs), duration_minutes (one way), weight | load_kg, vehicle_type_id,
    vehicle_type, toll_cost (all legs) | toll_cost_one_way, tolls_unknown,
    tolls_confirmed_none, toll_cost_empty_return, driver_cost, driver_nights,
    cross_border_cost, include_empty_return | include_return,
    distance_estimated, distance_confirmed, use_official_fuel,
    fuel_price_override, price | your_price.
    Returns (inputs, context) — context holds the resolved objects."""
    from django.conf import settings as dj_settings
    from django.utils import timezone
    from core.services.fuel_price import resolve_company_diesel

    payload = payload or {}
    now = now or timezone.now()
    today = timezone.localdate(now)
    trip = str(payload.get('trip_type') or '').upper()
    legs = _num(payload.get('legs'))
    round_trip = trip == 'ROUND_TRIP' or legs == 2
    legs = 2 if round_trip else 1

    one_way = _pos(payload.get('one_way_distance_km'))
    if one_way is None:
        total = _pos(payload.get('distance_km'))
        one_way = total / legs if total is not None else None

    load_kg = _num(payload.get('load_kg'))
    if load_kg is None:
        load_kg = _num(payload.get('weight'))
    vt, how = resolve_vehicle(company, vehicle_type_id=payload.get('vehicle_type_id'),
                              name=payload.get('vehicle_type'), load_kg=load_kg)

    if diesel_override is not None:
        diesel = diesel_override
    else:
        diesel = resolve_company_diesel(company, now, use_official=bool(_truthy(payload.get('use_official_fuel'))),
                                        override_price=_pos(payload.get('fuel_price_override')))
    op = operating_cost_for(company, vt) if vt is not None else None
    rate, rate_source = driver_rate(company, today)

    toll_one_way = _num(payload.get('toll_cost_one_way'))
    if toll_one_way is None:
        toll_total = _num(payload.get('toll_cost'))
        toll_one_way = toll_total / legs if toll_total is not None else None
    tolls_unknown = bool(_truthy(payload.get('tolls_unknown')))

    driver_amount = _num(payload.get('driver_cost'))
    if driver_amount is None:
        driver_amount = _num(payload.get('driver_allowance'))
    if _truthy(payload.get('driver_cost_is_override')) is False:
        driver_amount = None

    include = payload.get('include_empty_return')
    if include is None:
        include = payload.get('include_return')
    include = _truthy(include) if include not in (None, '') else None

    price = _pos(payload.get('price'))
    if price is None:
        price = _pos(payload.get('your_price'))

    inputs = {
        'trip_type': 'ROUND_TRIP' if round_trip else 'ONE_WAY',
        'distance_km': one_way,
        'distance_estimated': bool(_truthy(payload.get('distance_estimated'))),
        'distance_confirmed': bool(_truthy(payload.get('distance_confirmed'))),
        'duration_minutes': _pos(payload.get('duration_minutes')),
        'load_kg': load_kg,
        'vehicle': vehicle_input(vt),
        'diesel': diesel['input'],
        'operating_cost_per_km': op['value'] if op else (
            _pos(getattr(company, 'operating_cost_per_km', None))),
        'operating_cost_source': op['source'] if op else ('company_setting' if _pos(
            getattr(company, 'operating_cost_per_km', None)) else None),
        'tolls': {'one_way': toll_one_way, 'empty_return': _num(payload.get('toll_cost_empty_return')),
                  'lookup_failed': tolls_unknown,
                  'confirmed_none': bool(_truthy(payload.get('tolls_confirmed_none')))},
        'driver': {'allowance_per_night': rate, 'nights': _num(payload.get('driver_nights')),
                   'amount': driver_amount},
        'hours_per_day': float(getattr(dj_settings, 'DRIVER_DRIVING_HOURS_PER_DAY', DEFAULT_HOURS_PER_DAY)),
        'border_cost': _num(payload.get('cross_border_cost')) or 0.0,
        'include_empty_return': include,
        'settings': {
            'include_empty_return_default': bool(getattr(company, 'include_empty_return_default', True)),
            'empty_return_min_km': _num(getattr(company, 'empty_return_min_km', None)) or DEFAULT_EMPTY_RETURN_MIN_KM,
        },
        'minimum_charge': _pos(getattr(company, 'minimum_charge', None)),
        'target_margin_pct': target_margin(company),
        'price': price,
    }
    context = {'vehicle_type': vt, 'vehicle_how': how, 'operating_cost': op, 'diesel': diesel,
               'driver_rate_source': rate_source, 'now': now}
    return inputs, context


def _context_out(context):
    d = dict(context['diesel'])
    d.pop('input', None)
    d.pop('warnings', None)
    return {'vehicle_selection': context['vehicle_how'], 'operating_cost': context['operating_cost'],
            'driver_rate_source': context['driver_rate_source'], 'diesel_resolution': d}


def costing_for_payload(payload, company, now=None):
    """compute() for a builder payload, plus how each input was resolved."""
    inputs, context = build_inputs(payload, company, now)
    out = compute(inputs)
    out['inputs'] = inputs
    out['resolution'] = _context_out(context)
    return out


# ---------------------------------------------------------------------------
# Saved quotes
# ---------------------------------------------------------------------------

COSTING_INPUT_KEYS = {
    'distance_estimated': bool, 'distance_confirmed': bool, 'tolls_unknown': bool,
    'tolls_confirmed_none': bool, 'include_empty_return': bool, 'use_official_fuel': bool,
    'tolls_empty_return': float, 'fuel_price_override': float, 'vehicle_type_id': int,
    'driver_nights': int, 'duration_minutes': float, 'toll_cost_one_way': float,
}


def quote_payload(quote):
    """The pricing payload for a saved quote: its own fields (stored amounts
    are the user's figures) plus its costing_inputs."""
    ci = dict(quote.costing_inputs or {})
    legs = 2 if quote.trip_type == 'ROUND_TRIP' else 1
    distance = _pos(quote.distance)
    payload = {
        'trip_type': quote.trip_type,
        'distance_km': distance,
        'duration_minutes': ci.get('duration_minutes') or quote.estimated_duration_minutes,
        'weight': _num(quote.weight),
        'vehicle_type': quote.vehicle_type,
        'vehicle_type_id': ci.get('vehicle_type_id') or getattr(quote, 'priced_vehicle_type_id', None),
        'toll_cost': _num(quote.toll_charges),
        'toll_cost_one_way': ci.get('toll_cost_one_way'),
        'tolls_unknown': ci.get('tolls_unknown'),
        'tolls_confirmed_none': ci.get('tolls_confirmed_none'),
        'toll_cost_empty_return': ci.get('tolls_empty_return'),
        'driver_cost': _num(quote.driver_allowance),
        'driver_nights': ci.get('driver_nights'),
        'cross_border_cost': 0.0,
        'include_empty_return': ci.get('include_empty_return'),
        'distance_estimated': ci.get('distance_estimated'),
        'distance_confirmed': ci.get('distance_confirmed'),
        'use_official_fuel': ci.get('use_official_fuel'),
        'fuel_price_override': ci.get('fuel_price_override'),
        'price': _num(quote.total_amount),
    }
    # Quote.distance is the one-way leg (pricing_decisions reads it so too);
    # toll_charges is the total for the legs.
    payload['one_way_distance_km'] = distance
    payload['legs'] = legs
    return payload


def costing_for_quote(quote, now=None, *, use_snapshot_diesel=False):
    """compute() for a saved quote. use_snapshot_diesel: price fuel on the
    diesel stored when the quote was priced (send guard) instead of today's."""
    company = quote.company
    override = None
    if use_snapshot_diesel and quote.fuel_price_used is not None and quote.fuel_price_source:
        override = {'input': {
            'zone': quote.fuel_zone or getattr(company, 'fuel_zone', 'INLAND'), 'mode': 'LIVE',
            'own_price': None, 'own_set_at': None,
            'official_price': float(quote.fuel_price_used), 'official_effective_from': iso(quote.fuel_effective_from),
            'official_stale': False, 'use_official': False, 'override_price': None},
            'source': quote.fuel_price_source}
    inputs, context = build_inputs(quote_payload(quote), company, now, diesel_override=override)
    out = compute(inputs)
    if override is not None:
        out['diesel']['source'] = quote.fuel_price_source
    out['inputs'] = inputs
    out['resolution'] = _context_out(context) if override is None else {
        'vehicle_selection': context['vehicle_how'], 'operating_cost': context['operating_cost'],
        'driver_rate_source': context['driver_rate_source'], 'diesel_resolution': 'snapshot'}
    return out
