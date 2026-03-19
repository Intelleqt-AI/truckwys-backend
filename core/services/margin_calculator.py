"""
True Margin Calculator — profitability engine for TruckWys quotes.

Computes the real cost of a trip including fuel, driver, tolls,
tyre wear, maintenance, and deadhead, returning the true margin
against a quoted price.

Default cost parameters (articulated truck, inland ZAR):
  - Fuel consumption : 2.8 km/litre
  - Driver           : R3.50/km
  - Tyre wear        : R0.45/km
  - Maintenance      : R0.65/km
  - Deadhead factor  : 1.3  (30% empty return leg)
  - Diesel price     : sourced from FuelPrice model (inland)
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default cost parameters
# ---------------------------------------------------------------------------

DEFAULT_KM_PER_LITRE = Decimal('2.8')        # articulated truck
DEFAULT_DRIVER_RATE_PER_KM = Decimal('3.50')
DEFAULT_TYRE_WEAR_PER_KM = Decimal('0.45')
DEFAULT_MAINTENANCE_PER_KM = Decimal('0.65')
DEFAULT_DEADHEAD_FACTOR = Decimal('1.3')

# Fuel consumption overrides by truck type (km/litre)
_FUEL_BY_TYPE: dict[str, Decimal] = {
    'articulated': Decimal('2.8'),
    'semi':        Decimal('2.8'),
    'tautliner':   Decimal('2.8'),
    'flatbed':     Decimal('2.8'),
    'interlink':   Decimal('2.5'),
    'tanker':      Decimal('2.6'),
    'refrigerated': Decimal('2.4'),
    'reefer':      Decimal('2.4'),
    'rigid':       Decimal('4.5'),
}

# Hardcoded fallback diesel price (ZAR/litre) used only when no FuelPrice
# records exist in the database.
_FALLBACK_DIESEL_PRICE = Decimal('21.18')


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class MarginResult:
    """Result of a true margin calculation."""

    true_cost: Decimal
    margin_zar: Decimal
    margin_pct: Decimal
    cost_breakdown: dict
    fuel_price_used: Decimal
    distance_km: Decimal
    effective_distance_km: Decimal  # distance_km * deadhead_factor


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_true_margin(
    route: dict,
    truck_type: str,
    load_type: str,
    quote_price: Decimal,
    client_id: Optional[int] = None,
) -> MarginResult:
    """
    Calculate the true margin for a trip against a quoted price.

    Parameters
    ----------
    route : dict
        Must contain ``distance_km``.  May also contain:
        - ``tolls_zar``       – rand value of tolls for the loaded leg (default 0)
        - ``deadhead_factor`` – override the default 1.3 empty-return multiplier
    truck_type : str
        Truck category controlling fuel consumption, e.g. ``'articulated'``,
        ``'rigid'``.  Unknown values fall back to the articulated default.
    load_type : str
        Cargo type — reserved for future load-specific adjustments.
    quote_price : Decimal
        Price quoted to the client (ZAR, excl. VAT).
    client_id : int, optional
        Reserved for future client-specific pricing rules.

    Returns
    -------
    MarginResult
    """
    distance_km = Decimal(str(route.get('distance_km', 0)))
    if distance_km <= 0:
        raise ValueError('route must contain a positive distance_km')

    tolls_zar = Decimal(str(route.get('tolls_zar', 0)))
    deadhead_factor = Decimal(str(route.get('deadhead_factor', DEFAULT_DEADHEAD_FACTOR)))
    quote_price = Decimal(str(quote_price))

    effective_km = distance_km * deadhead_factor

    # Fuel
    km_per_litre = _FUEL_BY_TYPE.get(
        (truck_type or '').lower(),
        DEFAULT_KM_PER_LITRE,
    )
    diesel_price = _get_current_diesel_price()
    fuel_cost = (effective_km / km_per_litre) * diesel_price

    # Per-km costs applied to effective (deadheaded) distance
    driver_cost = effective_km * DEFAULT_DRIVER_RATE_PER_KM
    tyre_wear_cost = effective_km * DEFAULT_TYRE_WEAR_PER_KM
    maintenance_cost = effective_km * DEFAULT_MAINTENANCE_PER_KM

    # Tolls apply to the actual loaded route (not the empty return leg)
    toll_cost = tolls_zar

    true_cost = fuel_cost + driver_cost + toll_cost + tyre_wear_cost + maintenance_cost

    margin_zar = quote_price - true_cost
    if quote_price != 0:
        margin_pct = (margin_zar / quote_price * Decimal('100')).quantize(Decimal('0.01'))
    else:
        margin_pct = Decimal('0')

    cost_breakdown = {
        'fuel':           fuel_cost.quantize(Decimal('0.01')),
        'driver':         driver_cost.quantize(Decimal('0.01')),
        'tolls':          toll_cost.quantize(Decimal('0.01')),
        'tyre_wear':      tyre_wear_cost.quantize(Decimal('0.01')),
        'maintenance':    maintenance_cost.quantize(Decimal('0.01')),
        'deadhead_factor': deadhead_factor,
        'km_per_litre':   km_per_litre,
    }

    return MarginResult(
        true_cost=true_cost.quantize(Decimal('0.01')),
        margin_zar=margin_zar.quantize(Decimal('0.01')),
        margin_pct=margin_pct,
        cost_breakdown=cost_breakdown,
        fuel_price_used=diesel_price,
        distance_km=distance_km,
        effective_distance_km=effective_km.quantize(Decimal('0.01')),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_current_diesel_price() -> Decimal:
    """Return the most recent inland diesel price from FuelPrice records.

    Falls back to a hardcoded conservative value if no records exist.
    """
    from core.models.fuel_price import FuelPrice

    latest = FuelPrice.objects.order_by('-date').first()
    if latest:
        return latest.diesel_inland

    logger.warning(
        'No FuelPrice records found — using hardcoded fallback diesel price R%s',
        _FALLBACK_DIESEL_PRICE,
    )
    return _FALLBACK_DIESEL_PRICE
