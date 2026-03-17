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
        {'cape town', 'pe'},
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
    origin_lc = origin.lower().strip()
    destination_lc = destination.lower().strip()

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
