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
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vehicle type → SANRAL class mapping
# ---------------------------------------------------------------------------

# NOTE on the column offset: TollPlaza tariff columns are named one higher than the
# SANRAL class they hold. tariff_class_2 = SANRAL Class 1 (light), tariff_class_3 =
# SANRAL Class 2 (2-axle), tariff_class_4 = SANRAL Class 3 (3-axle), tariff_class_5 =
# SANRAL Class 4 (4+/combination). get_tariff(n) returns column tariff_class_n, so the
# int below is the COLUMN index (2–5), i.e. SANRAL class + 1.
TRUCK_TYPE_TO_CLASS: dict[str, int] = {
    'light':       2,   # SANRAL Class 1 — LDV / light commercial      → tariff_class_2
    'medium':      3,   # SANRAL Class 2 — 2-axle rigid truck / bus     → tariff_class_3
    'heavy':       4,   # SANRAL Class 3 — 3-axle single unit           → tariff_class_4
    'combination': 5,   # SANRAL Class 4 — truck + trailer / semi / interlink → tariff_class_5
    # Aliases
    'rigid':       3,
    'semi':        5,
    'interlink':   5,
}

# Frontend vehicle_type → toll truck type (also used by cross_border service).
# Single source of truth — core/views.py imports this. Confirmed axle→class table:
# Box Truck is a 2-axle rigid (SANRAL Class 2); the rest are 4+ combinations (Class 4).
VEHICLE_TO_TOLL_TYPE_LOOKUP: dict[str, str] = {
    'Flatbed':      'combination',
    'Tautliner':    'combination',
    'Refrigerated': 'combination',
    'Tanker':       'combination',
    'Danger Load':  'combination',
    'Box Truck':    'medium',        # 2-axle rigid → SANRAL Class 2 → tariff_class_3
    # extra fleet types (map by axle count)
    'Light Truck':  'light',
    'Van':          'light',
    'Bakkie':       'light',
    'Rigid':        'heavy',         # 3-axle rigid → SANRAL Class 3
    'Interlink':    'combination',
    'Superlink':    'combination',
}

# Keyword rules for names that don't hit the exact-match dict above. Vehicle-type
# names come from the VehicleType table and free-form UI values (e.g. "Medium Truck
# (4–8 tonnes)", "Semi-Truck / Horse & Trailer (30 tonnes)", "Flatbed Truck"), so an
# exact dict can never cover them. First matching rule wins — ordered most-specific
# first. Unknown names fall back to 'combination' (safe over-estimate).
_TOLL_TYPE_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    (('interlink', 'b-train', 'superlink', 'super link'), 'combination'),
    (('semi', 'horse', 'trailer', 'articulated'),         'combination'),
    (('flatbed', 'tautliner', 'refrigerated', 'reefer', 'tanker', 'danger'), 'combination'),
    (('ldv', 'light delivery', 'bakkie', 'van ', ' van', 'light'), 'light'),
    (('box', 'medium', '4-8', '4–8', '5 ton', '5-ton'),   'medium'),
    (('heavy', 'rigid', '8-16', '8–16'),                  'heavy'),
]


def resolve_toll_truck_type(vehicle_type: str) -> str:
    """Vehicle-type name (any source) → toll truck type ('light'/'medium'/'heavy'/'combination').

    Exact dict hit first, then case-insensitive keyword rules, then 'combination'
    as the safe (highest-tariff) default for unrecognised names.
    """
    if not vehicle_type:
        return 'combination'
    exact = VEHICLE_TO_TOLL_TYPE_LOOKUP.get(vehicle_type)
    if exact:
        return exact
    name = vehicle_type.casefold()
    for keywords, toll_type in _TOLL_TYPE_KEYWORD_RULES:
        if any(k in name for k in keywords):
            return toll_type
    logger.warning('Unrecognised vehicle_type %r for toll class — defaulting to combination', vehicle_type)
    return 'combination'

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
    tariff: Decimal


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

    plazas = (
        TollPlaza.objects
        .filter(route__in=routes, is_active=True)
        .order_by('route', 'location_km')
    )

    breakdown: list[TollBreakdownItem] = []
    total = Decimal('0.00')

    for plaza in plazas:
        tariff = plaza.get_tariff(vehicle_class)
        breakdown.append(TollBreakdownItem(
            plaza_name=plaza.name,
            route=plaza.route,
            location_km=plaza.location_km,
            tariff=tariff,
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


def calculate_tolls_by_geometry(
    route_points: list,
    truck_type: str,
) -> TollResult:
    """Geofence-based toll calculation using TomTom route geometry.

    Parameters
    ----------
    route_points:
        List of ``{"lat": float, "lon": float}`` dicts from TomTom's polyline.
    truck_type:
        Same values as :func:`calculate_tolls`.

    Returns
    -------
    :class:`TollResult` with every plaza whose geofence the route passes through.
    Falls back to an empty result (R0, warning) when no points are provided or
    no plaza has coordinates seeded yet.

    Notes
    -----
    A plaza is charged once, when its shortest distance to the route polyline is
    within ``TOLL_MATCH_BUFFER_M``. Distance is measured plaza→nearest-segment
    (point-to-line), so plazas on parallel/crossing roads near the route are NOT
    charged, and only plazas the route actually passes count.
    """
    from core.models.toll_plaza import TollPlaza

    truck_type_lc = truck_type.lower().strip()
    if truck_type_lc not in TRUCK_TYPE_TO_CLASS:
        raise ValueError(
            f"Unknown truck_type {truck_type!r}. "
            f"Valid options: {sorted(TRUCK_TYPE_TO_CLASS)}"
        )
    vehicle_class = TRUCK_TYPE_TO_CLASS[truck_type_lc]

    if not route_points:
        return TollResult(
            origin='', destination='', truck_type=truck_type,
            vehicle_class=vehicle_class, routes_used=[],
            total_zar=Decimal('0.00'),
            warning='No route geometry provided — cannot geofence tolls',
        )

    plazas = list(
        TollPlaza.objects
        .filter(is_active=True)
        .exclude(lat__isnull=True)
        .exclude(lng__isnull=True)
    )
    if not plazas:
        return TollResult(
            origin='', destination='', truck_type=truck_type,
            vehicle_class=vehicle_class, routes_used=[],
            total_zar=Decimal('0.00'),
            warning='No toll plazas with GPS coordinates seeded — run seed_toll_data --force',
        )

    # Build the list of consecutive polyline segments once, with each one's
    # bounding box precomputed up front — these don't depend on the plaza
    # being tested, so computing them fresh inside the per-plaza loop below
    # (as before) redundantly re-ran the same min/max over every segment for
    # every plaza.
    pts = [(float(p['lat']), float(p['lon'])) for p in route_points]
    segments = list(zip(pts, pts[1:]))
    segment_boxes = [
        (min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1]), a, b)
        for a, b in segments
    ]

    matched: list[TollBreakdownItem] = []
    routes_hit: set[str] = set()
    total = Decimal('0.00')

    for plaza in plazas:
        plaza_lat = float(plaza.lat)
        plaza_lng = float(plaza.lng)
        # Per-plaza override allowed, but cap at the buffer so a stale 500 m radius
        # can't re-introduce parallel-road false positives.
        buffer_m = min(float(plaza.radius_meters or TOLL_MATCH_BUFFER_M), TOLL_MATCH_BUFFER_M)

        # Distance from the plaza to the DRIVEN LINE (nearest segment), not just to a
        # vertex. A plaza only charges when the route actually passes it — this both
        # kills parallel/crossing-road false positives and windows the charge to the
        # trip's own segment (no whole-route summing).
        if segments:
            # Cheap bounding-box pre-filter before the expensive trig-heavy
            # point-to-segment math: a route can have thousands of polyline
            # points, so checking every plaza against every segment directly
            # (plazas × segments haversine calls) dominates request time on
            # long routes. PAD_DEG (~1.1km) is a generous superset of the
            # 300m buffer, so this only skips segments that are provably too
            # far — it can never miss a real match, just cheaply reject the
            # vast majority of segments before doing the real distance math.
            candidates = [
                (a, b) for (lat_min, lat_max, lon_min, lon_max, a, b) in segment_boxes
                if (lat_min - _BBOX_PAD_DEG <= plaza_lat <= lat_max + _BBOX_PAD_DEG
                    and lon_min - _BBOX_PAD_DEG <= plaza_lng <= lon_max + _BBOX_PAD_DEG)
            ]
            dist = min(
                (_point_to_segment_m(plaza_lat, plaza_lng, a[0], a[1], b[0], b[1]) for a, b in candidates),
                default=float('inf'),
            )
        else:
            dist = _haversine_m(plaza_lat, plaza_lng, pts[0][0], pts[0][1])

        if dist <= buffer_m:
            tariff = plaza.get_tariff(vehicle_class)
            matched.append(TollBreakdownItem(
                plaza_name=plaza.name,
                route=plaza.route,
                location_km=plaza.location_km,
                tariff=tariff,
            ))
            routes_hit.add(plaza.route)
            total += tariff

    matched.sort(key=lambda x: (x.route, x.location_km))

    logger.info(
        'Geofence tolls (%s / class %d): R%.2f across %d plaza(s) on %s '
        '(point-to-polyline, buffer %.0fm, %d segments vs %d plazas)',
        truck_type, vehicle_class, total, len(matched),
        ', '.join(sorted(routes_hit)) or 'no SANRAL routes',
        TOLL_MATCH_BUFFER_M, len(segments), len(plazas),
    )

    return TollResult(
        origin='', destination='', truck_type=truck_type,
        vehicle_class=vehicle_class,
        routes_used=sorted(routes_hit),
        total_zar=total,
        breakdown=matched,
    )
