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
