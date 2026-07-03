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

TRUCK_TYPE_TO_CLASS: dict[str, int] = {
    'light':       2,   # LDV / light commercial
    'medium':      3,   # 2-axle rigid truck or bus
    'heavy':       4,   # 3+ axle single unit
    'combination': 5,   # truck + trailer / semi-truck
    # Aliases
    'rigid':       3,
    'semi':        5,
    'interlink':   5,
}

# Frontend vehicle_type → toll truck type (also used by cross_border service)
VEHICLE_TO_TOLL_TYPE_LOOKUP: dict[str, str] = {
    'Flatbed':      'combination',
    'Tautliner':    'combination',
    'Refrigerated': 'combination',
    'Tanker':       'combination',
    'Danger Load':  'combination',
    'Box Truck':    'heavy',
}

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

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in metres between two WGS84 points."""
    R = 6_371_000.0
    phi1, phi2 = _math.radians(lat1), _math.radians(lat2)
    dphi = _math.radians(lat2 - lat1)
    dlam = _math.radians(lon2 - lon1)
    a = _math.sin(dphi / 2) ** 2 + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2
    return R * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))


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
    Each plaza is checked at most once — the first route point inside its
    ``radius_meters`` triggers it and the loop moves on, preventing double-charging.
    To handle sparse TomTom polyline segments (rural stretches can be 300–500 m apart)
    consecutive points are interpolated at 250 m intervals before matching.
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

    # Densify the polyline: insert midpoints on segments longer than 250 m so
    # sparse rural stretches do not skip over a plaza's geofence.
    INTERP_STEP_M = 250.0
    dense: list[tuple[float, float]] = []
    for i, pt in enumerate(route_points):
        lat, lon = float(pt['lat']), float(pt['lon'])
        dense.append((lat, lon))
        if i + 1 < len(route_points):
            nxt = route_points[i + 1]
            nlat, nlon = float(nxt['lat']), float(nxt['lon'])
            seg_m = _haversine_m(lat, lon, nlat, nlon)
            steps = int(seg_m // INTERP_STEP_M)
            for s in range(1, steps):
                frac = s / (steps)
                dense.append((lat + frac * (nlat - lat), lon + frac * (nlon - lon)))

    matched: list[TollBreakdownItem] = []
    routes_hit: set[str] = set()
    total = Decimal('0.00')

    for plaza in plazas:
        plaza_lat = float(plaza.lat)
        plaza_lng = float(plaza.lng)
        radius = plaza.radius_meters or 500

        for (lat, lon) in dense:
            if _haversine_m(lat, lon, plaza_lat, plaza_lng) <= radius:
                tariff = plaza.get_tariff(vehicle_class)
                matched.append(TollBreakdownItem(
                    plaza_name=plaza.name,
                    route=plaza.route,
                    location_km=plaza.location_km,
                    tariff=tariff,
                ))
                routes_hit.add(plaza.route)
                total += tariff
                break  # plaza matched — move to next plaza, no double-charge

    matched.sort(key=lambda x: (x.route, x.location_km))

    logger.info(
        'Geofence tolls (%s / class %d): R%.2f across %d plaza(s) on %s '
        '(checked %d densified points vs %d plazas)',
        truck_type, vehicle_class, total, len(matched),
        ', '.join(sorted(routes_hit)) or 'no SANRAL routes',
        len(dense), len(plazas),
    )

    return TollResult(
        origin='', destination='', truck_type=truck_type,
        vehicle_class=vehicle_class,
        routes_used=sorted(routes_hit),
        total_zar=total,
        breakdown=matched,
    )
