"""
Toll calculator service — determines SANRAL toll costs for a given route.

Uses TollPlaza records seeded by the seed_toll_data management command.
Gauteng e-tolls (GFIP / Urban Network) are excluded — scrapped April 2024.

Usage::

    from core.services.toll_calculator import calculate_tolls

    result = calculate_tolls('Johannesburg', 'Durban', 'combination')
    print(f"Total tolls: R{result.total_zar}")
    for item in result.breakdown:
        print(f"  {item.plaza_name} ({item.route}): R{item.tariff}")
"""

import logging
import math as _math
import re as _re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VAT
# ---------------------------------------------------------------------------

# SANRAL / concessionaire toll tariffs are published INCLUDING VAT (the 2026
# tariffs seeded by seed_toll_data are the gazetted, VAT-inclusive amounts). A
# carrier that invoices VAT reclaims the input VAT on each toll slip, so its
# real toll cost — the amount that belongs in a quote priced excl. VAT — is the
# tariff divided by 1.15. Invoices then add 15% VAT once on the quote total.
VAT_RATE = Decimal('0.15')
_CENT = Decimal('0.01')


def tariff_excl_vat(amount_incl_vat) -> Decimal:
    """VAT-inclusive tariff → VAT-exclusive amount, rounded half-up to the cent.

    Applied per plaza (each toll transaction is its own tax invoice), so a
    breakdown's excl.-VAT lines always sum exactly to its excl.-VAT total.
    """
    return (Decimal(str(amount_incl_vat)) / (Decimal('1') + VAT_RATE)).quantize(_CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Vehicle type → SANRAL class mapping
# ---------------------------------------------------------------------------
#
# SANRAL toll classes (2026 tariff table, "Toll Road Tariffs effective from
# 1 March 2026"):
#   Class 1 — light vehicles
#   Class 2 — 2-axle heavy vehicle
#   Class 3 — 3 & 4-axle heavy vehicle
#   Class 4 — heavy vehicle with more than 4 axles
# Axles are counted over the whole vehicle as it travels (horse + trailers).
#
# The class for a quote comes, in order, from:
#   1. VehicleType.sanral_toll_class, when set (see resolve_toll_class);
#   2. otherwise a best-effort guess from the vehicle-type NAME
#      (resolve_toll_class_from_name), reported as source 'name_inferred', or
#      'default' (Class 4) when the name says nothing usable.

# NOTE on the column offset: TollPlaza tariff columns are named one higher than the
# SANRAL class they hold. tariff_class_2 = SANRAL Class 1 (light), tariff_class_3 =
# SANRAL Class 2 (2-axle heavy), tariff_class_4 = SANRAL Class 3 (3 & 4-axle heavy),
# tariff_class_5 = SANRAL Class 4 (more than 4 axles). get_tariff(n) returns column
# tariff_class_n, so the int below is the COLUMN index (2–5), i.e. SANRAL class + 1.
TRUCK_TYPE_TO_CLASS: dict[str, int] = {
    'light':       2,   # SANRAL Class 1 — light vehicles (LDV, bakkie)       → tariff_class_2
    'medium':      3,   # SANRAL Class 2 — 2-axle heavy vehicle               → tariff_class_3
    'heavy':       4,   # SANRAL Class 3 — 3 & 4-axle heavy vehicle           → tariff_class_4
    'combination': 5,   # SANRAL Class 4 — more than 4 axles (most horse+trailer,
                        #                  every interlink)                   → tariff_class_5
    # Aliases
    'rigid':       3,
    'semi':        5,
    'interlink':   5,
}

# SANRAL class (1–4) ↔ the truck-type keys the calculators take.
SANRAL_CLASS_TO_TRUCK_TYPE: dict[int, str] = {1: 'light', 2: 'medium', 3: 'heavy', 4: 'combination'}
TRUCK_TYPE_TO_SANRAL_CLASS: dict[str, int] = {v: k for k, v in SANRAL_CLASS_TO_TRUCK_TYPE.items()}


def sanral_class_for_axles(axles: int) -> int:
    """Total axle count of a HEAVY vehicle → SANRAL class (2, 3 or 4).

    Class 1 (light vehicles) is not decided by axles — a bakkie has 2 axles
    too — so this only covers heavy vehicles.
    """
    axles = int(axles)
    if axles < 2:
        raise ValueError(f'A heavy vehicle has at least 2 axles, got {axles}')
    if axles == 2:
        return 2
    if axles <= 4:
        return 3
    return 4


# Frontend vehicle_type → toll truck type (also used by cross_border service).
# Exact-name hits for the fixed frontend list. No axle information in these
# names, so they keep their long-standing classes; a VehicleType row with
# sanral_toll_class set overrides all of them.
VEHICLE_TO_TOLL_TYPE_LOOKUP: dict[str, str] = {
    'Flatbed':      'combination',
    'Tautliner':    'combination',
    'Refrigerated': 'combination',
    'Tanker':       'combination',
    'Danger Load':  'combination',
    'Box Truck':    'medium',        # 2-axle rigid → SANRAL Class 2 → tariff_class_3
    # extra fleet types
    'Light Truck':  'light',
    'Van':          'light',
    'Bakkie':       'light',
    'Rigid':        'heavy',         # rigid of unknown axles → SANRAL Class 3 (3 & 4 axles)
    'Interlink':    'combination',
    'Superlink':    'combination',
}

# Keyword rules for names that don't hit the exact-match dict above, in order.
# First matching rule wins. Unknown names fall back to 'combination' (safe
# over-estimate) and are reported as source 'default'.
#
# Order matters:
#   * combination words first — a "6x4 horse" wheel formula describes the
#     tractor only, not the whole combination;
#   * light-vehicle words next — a "4x4 bakkie" is Class 1, not Class 2;
#   * then an explicit axle configuration in the name (see _axles_from_name);
#   * then body words. A body (flatbed, tautliner, reefer, tanker) says nothing
#     about axles, so it only applies when the name has no axle information.
_COMBINATION_KEYWORDS = ('interlink', 'b-train', 'superlink', 'super link',
                         'semi', 'horse', 'trailer', 'articulated')
_LIGHT_KEYWORDS = ('ldv', 'light delivery', 'bakkie', 'van ', ' van', 'light')
_BODY_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    (('flatbed', 'tautliner', 'refrigerated', 'reefer', 'tanker', 'danger'), 'combination'),
    (('box', 'medium', '4-8', '4–8', '5 ton', '5-ton'),   'medium'),
    (('heavy', 'rigid', '8-16', '8–16'),                  'heavy'),
]

# "4x2", "6×4", "8 x 4" (wheel positions x driven) → axles = wheel positions / 2.
_WHEEL_FORMULA_RE = _re.compile(r'(?<![\d.])(4|6|8|10)\s*[x×]\s*(2|4|6|8)(?![\d.])')
# "2-axle", "3 axle", "4axle" (an explicit total).
_AXLE_COUNT_RE = _re.compile(r'(?<![\d.])([2-9])\s*-?\s*axles?\b')


def _axles_from_name(name_cf: str) -> Optional[int]:
    m = _AXLE_COUNT_RE.search(name_cf)
    if m:
        return int(m.group(1))
    m = _WHEEL_FORMULA_RE.search(name_cf)
    if m:
        return int(m.group(1)) // 2
    return None


@dataclass
class TollClassResolution:
    """Which SANRAL class a quote is tolled at, and why."""
    sanral_class: int           # 1–4
    truck_type: str             # key for calculate_tolls*/TRUCK_TYPE_TO_CLASS
    source: str                 # 'vehicle_type' | 'name_inferred' | 'default'
    detail: str = ''


def resolve_toll_class_from_name(vehicle_type: str) -> TollClassResolution:
    """Best-effort SANRAL class from a vehicle-type NAME only (the fallback)."""
    def _res(truck_type, source, detail):
        return TollClassResolution(TRUCK_TYPE_TO_SANRAL_CLASS[truck_type], truck_type, source, detail)

    if not vehicle_type:
        return _res('combination', 'default', 'No vehicle type given; tolled as Class 4 (more than 4 axles)')
    exact = VEHICLE_TO_TOLL_TYPE_LOOKUP.get(vehicle_type)
    if exact:
        return _res(exact, 'name_inferred', f'Class guessed from the name {vehicle_type!r}')
    name = vehicle_type.casefold()
    if any(k in name for k in _COMBINATION_KEYWORDS):
        return _res('combination', 'name_inferred', f'{vehicle_type!r} reads as a combination vehicle')
    if any(k in name for k in _LIGHT_KEYWORDS):
        return _res('light', 'name_inferred', f'{vehicle_type!r} reads as a light vehicle')
    axles = _axles_from_name(name)
    if axles is not None and axles >= 2:
        return _res(SANRAL_CLASS_TO_TRUCK_TYPE[sanral_class_for_axles(axles)], 'name_inferred',
                    f'{vehicle_type!r} reads as {axles} axles')
    for keywords, toll_type in _BODY_KEYWORD_RULES:
        if any(k in name for k in keywords):
            return _res(toll_type, 'name_inferred', f'Class guessed from the name {vehicle_type!r}')
    logger.warning('Unrecognised vehicle_type %r for toll class — defaulting to combination', vehicle_type)
    return _res('combination', 'default',
                f'{vehicle_type!r} is not recognised; tolled as Class 4 (more than 4 axles)')


def resolve_toll_truck_type(vehicle_type: str) -> str:
    """Vehicle-type name (any source) → toll truck type ('light'/'medium'/'heavy'/'combination').

    Name-only; kept for existing callers. New code should use
    :func:`resolve_toll_class`, which honours VehicleType.sanral_toll_class.
    """
    return resolve_toll_class_from_name(vehicle_type).truck_type


def resolve_toll_class(vehicle_type: str, company=None) -> TollClassResolution:
    """SANRAL class for a quote's vehicle type.

    Uses the explicit ``sanral_toll_class`` of the VehicleType row this company
    can see under that name (its own row first, else the shared default — the
    same set the quote dropdown offers). Falls back to the name-based guess
    when there is no such row or its class is not set.
    """
    if vehicle_type:
        try:
            from django.db.models import F
            from core.services.vehicle_types import visible_vehicle_types_queryset
            # A company's own row beats a shared default of the same name.
            vt = (visible_vehicle_types_queryset(company)
                  .filter(name__iexact=vehicle_type.strip())
                  .exclude(sanral_toll_class__isnull=True)
                  .order_by(F('company_id').desc(nulls_last=True), 'id')
                  .first())
        except Exception:
            logger.exception('VehicleType lookup for toll class failed; using the name')
            vt = None
        if vt is not None and vt.sanral_toll_class in SANRAL_CLASS_TO_TRUCK_TYPE:
            cls = int(vt.sanral_toll_class)
            return TollClassResolution(cls, SANRAL_CLASS_TO_TRUCK_TYPE[cls], 'vehicle_type',
                                       f'Class {cls} set on vehicle type {vt.name!r}')
    return resolve_toll_class_from_name(vehicle_type)

# ---------------------------------------------------------------------------
# City alias normaliser — maps suburbs/metro areas to their parent city name.
# Applied before keyword matching so that "Amanzimtoti" is treated as "durban",
# "Sandton" as "johannesburg", etc.
# ---------------------------------------------------------------------------

_CITY_ALIASES: dict[str, str] = {
    # Durban / eThekwini metro
    'amanzimtoti': 'durban', 'pinetown': 'durban', 'umhlanga': 'durban',
    'ballito': 'durban', 'tongaat': 'durban', 'ethekwini': 'durban',
    'westville': 'durban', 'berea': 'durban', 'overport': 'durban',
    'umlazi': 'durban', 'isipingo': 'durban', 'prospecton': 'durban',
    'kwadukuza': 'durban', 'stanger': 'durban',
    'durban harbour': 'durban', 'point': 'durban',
    # Cape Town metro
    'pinelands': 'cape town', 'bellville': 'cape town', 'goodwood': 'cape town',
    'parow': 'cape town', 'tygervalley': 'cape town', 'tyger valley': 'cape town',
    'stellenbosch': 'cape town', 'somerset west': 'cape town', 'strand': 'cape town',
    'athlone': 'cape town', 'mitchells plain': 'cape town', 'khayelitsha': 'cape town',
    'wynberg': 'cape town', 'claremont': 'cape town', 'southern suburbs': 'cape town',
    # Johannesburg / Gauteng metro
    'sandton': 'johannesburg', 'randburg': 'johannesburg', 'midrand': 'johannesburg',
    'soweto': 'johannesburg', 'alberton': 'johannesburg', 'germiston': 'johannesburg',
    'benoni': 'johannesburg', 'boksburg': 'johannesburg', 'ekurhuleni': 'johannesburg',
    'kempton park': 'johannesburg', 'edenvale': 'johannesburg', 'roodepoort': 'johannesburg',
    'krugersdorp': 'johannesburg', 'randfontein': 'johannesburg',
    # Pretoria metro
    'centurion': 'pretoria', 'soshanguve': 'pretoria', 'mamelodi': 'pretoria',
    'hatfield': 'pretoria', 'menlyn': 'pretoria', 'tshwane': 'pretoria',
    # Other aliases
    'gqeberha': 'port elizabeth',
    'mbombela': 'nelspruit',
    'emalahleni': 'witbank',
    'pmb': 'pietermaritzburg',
    'bhisho': 'east london',
}


def _normalise_location(location: str) -> str:
    """Expand suburbs/aliases to their canonical city name so keyword matching works.
    Uses word-boundary matching so 'pe' never matches inside 'cape'."""
    loc = location.lower().strip()
    extras: list[str] = []
    for alias, canonical in _CITY_ALIASES.items():
        if _re.search(r'\b' + _re.escape(alias) + r'\b', loc) and canonical not in loc:
            extras.append(canonical)
    return loc + (' ' + ' '.join(extras) if extras else '')


# ---------------------------------------------------------------------------
# Route detection — maps keyword sets to route codes.
# Each inner set must be fully covered by the combined origin+destination
# strings for the route to be considered a match.
# ---------------------------------------------------------------------------

_ROUTE_KEYWORDS: dict[str, list[set[str]]] = {
    'N1': [
        {'cape town', 'johannesburg'},
        {'cape town', 'joburg'},
        {'cape town', 'jozi'},
        {'cape town', 'gauteng'},
        {'cape town', 'colesberg'},
        {'worcester', 'johannesburg'},
        {'beaufort west', 'johannesburg'},
        {'beaufort west', 'cape town'},
        {'touws river', 'cape town'},
        {'laingsburg', 'johannesburg'},
    ],
    'N2': [
        {'cape town', 'durban'},
        {'cape town', 'port elizabeth'},
        {'cape town', 'east london'},
        {'george', 'port elizabeth'},
        {'george', 'east london'},
        {'knysna', 'port elizabeth'},
        {'storms river', 'cape town'},
        {'tsitsikamma', 'cape town'},
    ],
    'N3': [
        {'johannesburg', 'durban'},
        {'joburg', 'durban'},
        {'jozi', 'durban'},
        {'gauteng', 'durban'},
        {'johannesburg', 'pietermaritzburg'},
        {'johannesburg', 'pmb'},
        {'harrismith', 'durban'},
        {'van reenen', 'durban'},
        {'mooi river', 'johannesburg'},
    ],
    'N4': [
        {'pretoria', 'maputo'},
        {'pretoria', 'komatipoort'},
        {'pretoria', 'nelspruit'},
        {'pretoria', 'mbombela'},
        {'johannesburg', 'maputo'},
        {'witbank', 'maputo'},
        {'middelburg', 'maputo'},
    ],
    'N14': [
        {'johannesburg', 'springbok'},
        {'joburg', 'springbok'},
        {'jozi', 'springbok'},
        {'rustenburg', 'johannesburg'},
        {'vryburg', 'johannesburg'},
        {'kuruman', 'johannesburg'},
        {'lichtenburg', 'johannesburg'},
    ],
    'N17': [
        {'johannesburg', 'ermelo'},
        {'joburg', 'ermelo'},
        {'johannesburg', 'swaziland'},
        {'johannesburg', 'eswatini'},
        {'johannesburg', 'secunda'},
        {'johannesburg', 'standerton'},
        {'springs', 'ermelo'},
    ],
    'R30': [
        {'bloemfontein', 'brandfort'},
        {'bloemfontein', 'winburg'},
        {'bloemfontein', 'theunissen'},
    ],
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TollBreakdownItem:
    plaza_name: str
    route: str
    location_km: Decimal
    tariff: Decimal                         # published tariff, VAT INCLUSIVE
    tariff_excl_vat: Optional[Decimal] = None
    plaza_type: str = 'mainline'
    operator: str = ''
    country: str = 'ZA'
    tariff_effective_from: Optional[object] = None   # date the charged tariff took effect
    route_sequence: int = 0                          # polyline segment where the route reaches it

    def __post_init__(self):
        if self.tariff_excl_vat is None:
            # SA input VAT is reclaimable on a SANRAL/concession toll slip; a
            # foreign toll (Mozambique's IVA) is not, so there the whole
            # amount is the carrier's cost.
            self.tariff_excl_vat = (tariff_excl_vat(self.tariff) if (self.country or 'ZA') == 'ZA'
                                    else Decimal(str(self.tariff)).quantize(_CENT))


@dataclass
class TollResult:
    origin: str
    destination: str
    truck_type: str
    vehicle_class: int
    routes_used: list[str]
    total_zar: Decimal
    breakdown: list[TollBreakdownItem] = field(default_factory=list)
    warning: Optional[str] = None
    # Set when the result is NOT a real toll figure (the R0 is a placeholder):
    # 'no_geometry' | 'no_toll_data'. None for a genuine result, including a
    # genuine R0 on a route with no plazas.
    unavailable_reason: Optional[str] = None

    @property
    def total_excl_vat(self) -> Decimal:
        """Sum of the per-plaza VAT-exclusive amounts (total_zar is VAT inclusive)."""
        return sum((b.tariff_excl_vat for b in self.breakdown), Decimal('0.00'))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_routes(origin: str, destination: str) -> list[str]:
    """Return list of route codes that connect origin to destination."""
    origin_lc = _normalise_location(origin)
    destination_lc = _normalise_location(destination)

    matched = []
    for route_code, keyword_pairs in _ROUTE_KEYWORDS.items():
        for kw_set in keyword_pairs:
            hits = sum(
                1 for kw in kw_set
                if kw in origin_lc or kw in destination_lc
            )
            if hits == len(kw_set):
                matched.append(route_code)
                break

    return matched


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_tolls(
    origin: str,
    destination: str,
    truck_type: str,
    trip_date=None,
) -> TollResult:
    """
    Calculate total SANRAL toll costs for a trip.

    Parameters
    ----------
    origin:       City/town name (e.g. ``"Cape Town"``)
    destination:  City/town name (e.g. ``"Johannesburg"``)
    truck_type:   One of ``'light'``, ``'medium'``, ``'heavy'``, ``'combination'``
                  (aliases ``'rigid'``, ``'semi'``, ``'interlink'`` also accepted)

    Returns
    -------
    :class:`TollResult` with ``total_zar`` and per-plaza ``breakdown``.

    Raises
    ------
    ValueError
        If *truck_type* is not a recognised value.
    """
    from core.models.toll_plaza import TollPlaza

    truck_type_lc = truck_type.lower().strip()
    if truck_type_lc not in TRUCK_TYPE_TO_CLASS:
        raise ValueError(
            f"Unknown truck_type {truck_type!r}. "
            f"Valid options: {sorted(TRUCK_TYPE_TO_CLASS)}"
        )

    vehicle_class = TRUCK_TYPE_TO_CLASS[truck_type_lc]
    routes = _detect_routes(origin, destination)

    if not routes:
        logger.warning(
            'No known SANRAL route found for %s → %s — returning zero tolls',
            origin, destination,
        )
        return TollResult(
            origin=origin,
            destination=destination,
            truck_type=truck_type,
            vehicle_class=vehicle_class,
            routes_used=[],
            total_zar=Decimal('0.00'),
            warning=f"No known SANRAL route between {origin!r} and {destination!r}",
        )

    # Mainline plazas only: a keyword corridor says nothing about which
    # interchange the trip uses, and a through trip pays the mainline.
    plazas = (
        TollPlaza.objects
        .filter(route__in=routes, is_active=True, plaza_type=TollPlaza.TYPE_MAINLINE)
        .order_by('route', 'location_km')
    )
    on_date = trip_date or _today()

    breakdown: list[TollBreakdownItem] = []
    total = Decimal('0.00')

    for plaza in plazas:
        tariff, effective_from = plaza.tariff_on(vehicle_class, on_date)
        breakdown.append(TollBreakdownItem(
            plaza_name=plaza.name,
            route=plaza.route,
            location_km=plaza.location_km,
            tariff=tariff,
            plaza_type=plaza.plaza_type,
            operator=plaza.operator,
            country=plaza.country,
            tariff_effective_from=effective_from,
        ))
        total += tariff

    logger.info(
        'Tolls %s → %s (%s / class %d): R%.2f across %d plaza(s) on %s',
        origin, destination, truck_type, vehicle_class,
        total, len(breakdown), ', '.join(routes),
    )

    return TollResult(
        origin=origin,
        destination=destination,
        truck_type=truck_type,
        vehicle_class=vehicle_class,
        routes_used=routes,
        total_zar=total,
        breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Geofence-based toll calculation (preferred when TomTom geometry is available)
# ---------------------------------------------------------------------------

# Max distance (metres) a plaza may sit from the driven route line to be charged.
# Tunable 200–500. Tighter than the old 500 m point radius because we now measure to
# the route LINE (not just vertices), which removes parallel-road false positives.
TOLL_MATCH_BUFFER_M = 300.0

# Bounding-box pre-filter margin (degrees) for the geofence match below — a
# generous superset of TOLL_MATCH_BUFFER_M (300m ≈ 0.003°) so it only ever
# discards segments that are definitely out of range.
_BBOX_PAD_DEG = 0.01


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in metres between two WGS84 points."""
    R = 6_371_000.0
    phi1, phi2 = _math.radians(lat1), _math.radians(lat2)
    dphi = _math.radians(lat2 - lat1)
    dlam = _math.radians(lon2 - lon1)
    a = _math.sin(dphi / 2) ** 2 + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2
    return R * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))


def _point_to_segment_m(plat, plng, alat, alng, blat, blng) -> float:
    """Shortest distance (metres) from point P to segment A→B.

    Uses a local equirectangular projection (x = lon·cos(lat), y = lat) scaled to
    metres — accurate to well under a metre over the ~km-scale segments of a road
    polyline, and dependency-free. Projects P onto the segment, clamps t∈[0,1] so
    the result is distance to the segment (not the infinite line), then measures the
    clamped foot with the exact haversine.
    """
    cos_lat = _math.cos(_math.radians(plat))
    ax, ay = alng * cos_lat, alat
    bx, by = blng * cos_lat, blat
    px, py = plng * cos_lat, plat
    dx, dy = bx - ax, by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0.0:
        return _haversine_m(plat, plng, alat, alng)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
    t = max(0.0, min(1.0, t))
    foot_lat = alat + t * (blat - alat)
    foot_lng = alng + t * (blng - alng)
    return _haversine_m(plat, plng, foot_lat, foot_lng)


def _route_index(route_points: list) -> list:
    """Polyline segments with their bounding boxes, built once per route."""
    pts = [(float(p['lat']), float(p['lon'])) for p in route_points]
    return [
        (min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1]), a, b, i)
        for i, (a, b) in enumerate(zip(pts, pts[1:]))
    ]


def _nearest_on_route(lat: float, lng: float, segment_boxes: list, first_point=None) -> tuple:
    """(distance in m, index of the nearest segment) — the index orders plazas
    in the sequence the route reaches them."""
    best = (float('inf'), 0)
    if not segment_boxes:
        if first_point:
            return _haversine_m(lat, lng, first_point[0], first_point[1]), 0
        return best
    for (lat_min, lat_max, lon_min, lon_max, a, b, i) in segment_boxes:
        if (lat_min - _BBOX_PAD_DEG <= lat <= lat_max + _BBOX_PAD_DEG
                and lon_min - _BBOX_PAD_DEG <= lng <= lon_max + _BBOX_PAD_DEG):
            d = _point_to_segment_m(lat, lng, a[0], a[1], b[0], b[1])
            if d < best[0]:
                best = (d, i)
    return best


def _distance_to_route_m(lat: float, lng: float, segment_boxes: list, first_point=None) -> float:
    """Shortest distance (m) from a point to the route polyline.

    Bounding-box pre-filter first: a route has thousands of segments and only
    the handful near the point can be within any buffer we use, so the trig
    runs on those alone. _BBOX_PAD_DEG (~1.1km) is a superset of every
    buffer, so the filter can never drop a real match.
    """
    return _nearest_on_route(lat, lng, segment_boxes, first_point)[0]


# A ramp plaza is NOT charged when the route passes every one of its
# through_points (on the mainline either side of the interchange) within this
# distance: the route stayed on the mainline and only drove PAST the ramp booth.
THROUGH_POINT_TOLERANCE_M = 50.0


def _today():
    try:
        from django.utils import timezone
        return timezone.localdate()
    except Exception:  # pragma: no cover - outside Django
        import datetime
        return datetime.date.today()


def parse_trip_date(value):
    """'2026-03-01' / '2026-03-01T08:00:00Z' / date / None → date (today when absent or unparseable)."""
    import datetime
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.date.fromisoformat(value.strip()[:10])
        except ValueError:
            logger.warning('Unparseable trip date %r for tolls; using today', value)
    return _today()


def calculate_tolls_by_geometry(
    route_points: list,
    truck_type: str,
    trip_date=None,
) -> TollResult:
    """Geofence-based toll calculation using TomTom route geometry.

    Parameters
    ----------
    route_points:
        List of ``{"lat": float, "lon": float}`` dicts from TomTom's polyline.
    truck_type:
        Same values as :func:`calculate_tolls`.
    trip_date:
        The day the trip runs (``datetime.date``). Tariffs are the ones in
        force on that day — a load quoted in February for a March trip pays
        the new 1 March tariff. Defaults to today.

    Returns
    -------
    :class:`TollResult` with every plaza the route drives through.

    Matching
    --------
    * A plaza matches when its nearest booth (lat/lng or any of
      ``match_points``) is within ``min(radius_meters, TOLL_MATCH_BUFFER_M)``
      of the route LINE (point-to-segment), so plazas on parallel or crossing
      roads are not charged.
    * A ramp plaza with ``through_points`` is skipped when the route passes
      all of them: it stayed on the mainline past the interchange.
    * Plazas sharing a ``plaza_group`` (a mainline plaza and the ramps at its
      interchange) are alternatives — a vehicle pays one of them. A matched
      ramp wins over its mainline (it passed the stricter ramp tests); among
      several, the nearest wins.
    """
    from core.models.toll_plaza import TollPlaza

    truck_type_lc = truck_type.lower().strip()
    if truck_type_lc not in TRUCK_TYPE_TO_CLASS:
        raise ValueError(
            f"Unknown truck_type {truck_type!r}. "
            f"Valid options: {sorted(TRUCK_TYPE_TO_CLASS)}"
        )
    vehicle_class = TRUCK_TYPE_TO_CLASS[truck_type_lc]
    on_date = trip_date or _today()

    if not route_points:
        return TollResult(
            origin='', destination='', truck_type=truck_type,
            vehicle_class=vehicle_class, routes_used=[],
            total_zar=Decimal('0.00'),
            warning='No route geometry provided — cannot geofence tolls',
            unavailable_reason='no_geometry',
        )

    plazas = list(
        TollPlaza.objects
        .filter(is_active=True)
        .exclude(lat__isnull=True)
        .exclude(lng__isnull=True)
        .prefetch_related('tariff_history')
    )
    if not plazas:
        return TollResult(
            origin='', destination='', truck_type=truck_type,
            vehicle_class=vehicle_class, routes_used=[],
            total_zar=Decimal('0.00'),
            warning='No toll plazas with GPS coordinates seeded — run seed_toll_data --force',
            unavailable_reason='no_toll_data',
        )

    segment_boxes = _route_index(route_points)
    first = (float(route_points[0]['lat']), float(route_points[0]['lon']))

    # (plaza, distance) for every plaza the route drives through.
    candidates: list[tuple] = []
    for plaza in plazas:
        # Per-plaza radius allowed (ramps use a tight one), capped at the buffer
        # so a stale 500 m radius can't re-introduce parallel-road false positives.
        buffer_m = min(float(plaza.radius_meters or TOLL_MATCH_BUFFER_M), TOLL_MATCH_BUFFER_M)
        dist, seq = min(_nearest_on_route(lat, lng, segment_boxes, first)
                        for lat, lng in plaza.all_match_points())
        if dist > buffer_m:
            continue
        through = plaza.through_points or []
        if through and all(
            _distance_to_route_m(float(tp[0]), float(tp[1]), segment_boxes, first) <= THROUGH_POINT_TOLERANCE_M
            for tp in through
        ):
            continue
        candidates.append((plaza, dist, seq))

    chosen: dict = {}
    for plaza, dist, seq in candidates:
        key = plaza.plaza_group or f'#{plaza.pk}'
        best = chosen.get(key)
        rank = (0 if plaza.plaza_type == TollPlaza.TYPE_RAMP else 1, dist)
        if best is None or rank < best[0]:
            chosen[key] = (rank, plaza, seq)

    matched: list[TollBreakdownItem] = []
    routes_hit: set[str] = set()
    total = Decimal('0.00')
    for _rank, plaza, seq in chosen.values():
        history = sorted(plaza.tariff_history.all(), key=lambda r: r.effective_from, reverse=True)
        tariff, effective_from = plaza.tariff_on(vehicle_class, on_date, history)
        matched.append(TollBreakdownItem(
            plaza_name=plaza.name,
            route=plaza.route,
            location_km=plaza.location_km,
            tariff=tariff,
            plaza_type=plaza.plaza_type,
            operator=plaza.operator,
            country=plaza.country,
            tariff_effective_from=effective_from,
            route_sequence=seq,
        ))
        routes_hit.add(plaza.route)
        total += tariff

    # In the order the route reaches them.
    matched.sort(key=lambda x: x.route_sequence)

    logger.info(
        'Geofence tolls (%s / class %d, %s): R%.2f across %d plaza(s) on %s '
        '(point-to-polyline, %d segments vs %d plazas)',
        truck_type, vehicle_class, on_date, total, len(matched),
        ', '.join(sorted(routes_hit)) or 'no toll routes',
        len(segment_boxes), len(plazas),
    )

    return TollResult(
        origin='', destination='', truck_type=truck_type,
        vehicle_class=vehicle_class,
        routes_used=sorted(routes_hit),
        total_zar=total,
        breakdown=matched,
    )
