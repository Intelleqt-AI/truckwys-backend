"""
Cross-border route calculation service.

Determines countries crossed, calculates border fees, weighbridge fees,
and non-SA tolls for international routes.

Cost data is read from DB (BorderCrossingFee, CountryTransitRate) seeded by
seed_cross_border_data, with hardcoded fallbacks when DB has no matching record.

Fixes applied vs original:
  1. Detection now uses resolved TomTom labels (passed by caller).
  2. All costs read from DB — admin-editable without code deploys.
  3. Distance split uses per-country sa_border_distance_km instead of equal division.
  4. SA toll plazas on cross-border routes charged where seeded data exists (N4/MZ).
"""

import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Multi-hop route definitions
# ---------------------------------------------------------------------------
MULTI_HOP_ROUTES: dict[tuple[str, str], list[str]] = {
    ('SA', 'ZM'): ['SA', 'ZW', 'ZM'],
    ('SA', 'MW'): ['SA', 'ZW', 'MW'],
    ('SA', 'TZ'): ['SA', 'ZW', 'ZM', 'TZ'],
    ('SA', 'KE'): ['SA', 'ZW', 'ZM', 'TZ', 'KE'],
}

DIRECT_ROUTES = ['ZW', 'MZ', 'BW', 'NA', 'LS', 'SZ']

# ---------------------------------------------------------------------------
# Hardcoded fallbacks (used when DB has no matching record)
# ---------------------------------------------------------------------------
_FALLBACK_BORDER_FEES: dict[str, int] = {
    'SA-ZW': 850, 'SA-BW': 650, 'SA-NA': 600, 'SA-MZ': 750,
    'SA-LS': 300, 'SA-SZ': 250, 'ZW-ZM': 900, 'ZW-MW': 850,
    'ZM-TZ': 1200, 'TZ-KE': 1100,
}
_FALLBACK_WEIGHBRIDGE: dict[str, int] = {
    'ZW': 250, 'BW': 200, 'NA': 180, 'MZ': 220,
    'ZM': 280, 'MW': 260, 'TZ': 320, 'KE': 300, 'LS': 150, 'SZ': 160,
}
_FALLBACK_TOLL_RATE: dict[str, float] = {
    'ZW': 0.45, 'BW': 0.30, 'NA': 0.25, 'MZ': 0.40,
    'ZM': 0.50, 'MW': 0.45, 'TZ': 0.55, 'KE': 0.60, 'LS': 0.20, 'SZ': 0.22,
}
# Approximate km from Johannesburg to SA border post for each neighbour
_FALLBACK_SA_BORDER_KM: dict[str, float] = {
    'ZW': 580.0, 'MZ': 380.0, 'BW': 360.0, 'NA': 1400.0,
    'LS': 350.0, 'SZ': 380.0, 'ZM': 580.0, 'MW': 580.0,
    'TZ': 580.0, 'KE': 580.0,
}

# SA highway used on cross-border routes (for SA-side toll charging)
# Only routes where we have seeded TollPlaza data are listed.
_SA_HIGHWAY_FOR_BORDER: dict[str, str] = {
    'MZ': 'N4',  # Pretoria → Komatipoort — all 8 N4 plazas are before the border
    'SZ': 'N4',  # Pretoria → Oshoek — first 3 N4 plazas (≤100km) are in SA
}


# ---------------------------------------------------------------------------
# DB helpers (with fallback to hardcoded dicts)
# ---------------------------------------------------------------------------

def _get_border_fee(from_country: str, to_country: str) -> float:
    try:
        from core.models.border_crossing_fee import BorderCrossingFee
        fee = BorderCrossingFee.get_fee(from_country, to_country)
        if fee > 0:
            return float(fee)
    except Exception:
        pass
    return _FALLBACK_BORDER_FEES.get(f'{from_country}-{to_country}', 0)


def _get_country_rate(country: str) -> dict[str, float]:
    try:
        from core.models.country_transit_rate import CountryTransitRate
        r = CountryTransitRate.objects.get(country_code=country, is_active=True)
        return {
            'weighbridge': float(r.weighbridge_fee_zar),
            'toll_rate':   float(r.toll_rate_per_km),
            'sa_border_km': float(r.sa_border_distance_km),
        }
    except Exception:
        pass
    return {
        'weighbridge':  _FALLBACK_WEIGHBRIDGE.get(country, 200),
        'toll_rate':    _FALLBACK_TOLL_RATE.get(country, 0.40),
        'sa_border_km': _FALLBACK_SA_BORDER_KM.get(country, 500.0),
    }


# ---------------------------------------------------------------------------
# Country detection
# ---------------------------------------------------------------------------

def _extract_country(location: str) -> str:
    loc = location.upper().strip()

    if any(kw in loc for kw in [
        'JOHANNESBURG', 'JHB', 'CAPE TOWN', 'CPT', 'DURBAN', 'DBN',
        'PRETORIA', 'PTA', 'BLOEMFONTEIN', 'BFN', 'PORT ELIZABETH',
        'GQEBERHA', 'EAST LONDON', 'NELSPRUIT', 'MBOMBELA', 'POLOKWANE',
        'RUSTENBURG', 'KIMBERLEY', 'GEORGE', 'SOUTH AFRICA',
        # Major SA cities/areas not in the short list above
        'KEMPTON PARK', 'EKURHULENI', 'SANDTON', 'MIDRAND', 'CENTURION',
        'BOKSBURG', 'GERMISTON', 'BENONI', 'SOWETO', 'ROODEPOORT',
        'RANDBURG', 'KRUGERSDORP', 'WITBANK', 'EMALAHLENI', 'SECUNDA',
        'PIETERMARITZBURG', 'PMB', 'RICHARDS BAY', 'NEWCASTLE', 'VEREENIGING',
        'VANDERBIJLPARK', 'SASOLBURG', 'UPINGTON', 'SPRINGBOK', 'VREDENDAL',
        'WORCESTER', 'STELLENBOSCH', 'PAARL', 'MALMESBURY', 'BEAUFORT WEST',
        'OR TAMBO', 'KING SHAKA', 'BRAM FISCHER', 'LANSERIA',
    ]):
        return 'SA'

    # NOTE: Do NOT use short country-code suffixes like ', KE' or ', ZW' here —
    # they are substrings of SA place names (e.g. ', KE' matches ', KEMPTON',
    # ', ZW' matches ', ZWELITSHA', ', NA' matches ', NAPIER'). Use full city
    # or country names only.
    if any(kw in loc for kw in ['HARARE', 'BULAWAYO', 'MUTARE', 'ZIMBABWE']):
        return 'ZW'
    if any(kw in loc for kw in ['LUSAKA', 'NDOLA', 'KITWE', 'LIVINGSTONE', 'ZAMBIA']):
        return 'ZM'
    if any(kw in loc for kw in ['MAPUTO', 'BEIRA', 'NAMPULA', 'MOZAMBIQUE']):
        return 'MZ'
    if any(kw in loc for kw in ['GABORONE', 'FRANCISTOWN', 'MAUN', 'BOTSWANA']):
        return 'BW'
    if any(kw in loc for kw in ['WINDHOEK', 'WALVIS BAY', 'SWAKOPMUND', 'NAMIBIA']):
        return 'NA'
    if any(kw in loc for kw in ['LILONGWE', 'BLANTYRE', 'MALAWI']):
        return 'MW'
    if any(kw in loc for kw in ['DAR ES SALAAM', 'DODOMA', 'ARUSHA', 'TANZANIA']):
        return 'TZ'
    if any(kw in loc for kw in ['NAIROBI', 'MOMBASA', 'KISUMU', 'KENYA']):
        return 'KE'
    if any(kw in loc for kw in ['MASERU', 'LESOTHO']):
        return 'LS'
    if any(kw in loc for kw in ['MBABANE', 'MANZINI', 'ESWATINI', 'SWAZILAND']):
        return 'SZ'

    return 'SA'


def detect_countries(origin: str, destination: str) -> list[str] | None:
    """
    Detect route countries from resolved address strings.

    Returns list of country codes in order, or None for domestic SA.
    """
    origin_country = _extract_country(origin)
    dest_country   = _extract_country(destination)

    if origin_country == 'SA' and dest_country == 'SA':
        return None

    route_key = (origin_country, dest_country)
    if route_key in MULTI_HOP_ROUTES:
        return MULTI_HOP_ROUTES[route_key]

    if origin_country == 'SA' and dest_country in DIRECT_ROUTES:
        return ['SA', dest_country]
    if dest_country == 'SA' and origin_country in DIRECT_ROUTES:
        return [origin_country, 'SA']

    # Unknown combination — treat as domestic
    logger.warning('Cannot determine cross-border route for %r → %r', origin, destination)
    return None


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def calculate_cross_border_costs(
    countries: list[str],
    distance_km: float,
    vehicle_type: str = 'truck',
) -> dict[str, Any]:
    if not countries or len(countries) <= 1:
        return {'border_fees': 0, 'weighbridge_fees': 0, 'non_sa_tolls': 0, 'total': 0, 'breakdown': []}

    breakdown: list[dict] = []
    border_fees    = 0.0
    weighbridge_fees = 0.0
    non_sa_tolls   = 0.0

    # --- Border crossing fees ---
    for i in range(len(countries) - 1):
        fc, tc = countries[i], countries[i + 1]
        fee = _get_border_fee(fc, tc)
        if fee > 0:
            border_fees += fee
            breakdown.append({'type': 'border_crossing', 'description': f'{fc} → {tc} border crossing', 'amount': round(fee, 2)})

    # --- Weighbridge fees (one per non-SA country) ---
    for country in countries:
        if country == 'SA':
            continue
        rate = _get_country_rate(country)
        fee = rate['weighbridge']
        weighbridge_fees += fee
        breakdown.append({'type': 'weighbridge', 'description': f'{country} weighbridge', 'amount': round(fee, 2)})

    # --- Non-SA toll costs with accurate distance splitting ---
    # Use sa_border_distance_km to estimate the SA portion of the journey,
    # then distribute the remainder across non-SA countries equally.
    non_sa_countries = [c for c in countries if c != 'SA']
    if non_sa_countries:
        first_foreign = non_sa_countries[0]
        rate_info     = _get_country_rate(first_foreign)
        sa_border_km  = min(rate_info['sa_border_km'], distance_km * 0.9)
        non_sa_total_km = max(distance_km - sa_border_km, distance_km * 0.1)
        dist_per_foreign = non_sa_total_km / len(non_sa_countries)

        for country in non_sa_countries:
            r    = _get_country_rate(country)
            cost = dist_per_foreign * r['toll_rate']
            non_sa_tolls += cost
            breakdown.append({
                'type': 'non_sa_toll',
                'description': f'{country} tolls (~{int(dist_per_foreign)} km)',
                'amount': round(cost, 2),
            })

    total = border_fees + weighbridge_fees + non_sa_tolls
    return {
        'border_fees':      round(border_fees, 2),
        'weighbridge_fees': round(weighbridge_fees, 2),
        'non_sa_tolls':     round(non_sa_tolls, 2),
        'total':            round(total, 2),
        'breakdown':        breakdown,
    }


def calculate_sa_tolls_for_cross_border(
    countries: list[str],
    vehicle_type: str,
) -> dict[str, Any]:
    """
    Return SA-side SANRAL toll cost for cross-border routes.

    Only covers routes where we have seeded TollPlaza data on the SA
    departure highway (currently N4 → MZ and partial N4 → SZ).
    Returns toll_zar=0 and empty breakdown for all other corridors.
    """
    if not countries or len(countries) <= 1:
        return {'toll_zar': 0.0, 'breakdown': [], 'route': None}

    # Determine which foreign country is first
    non_sa = next((c for c in countries if c != 'SA'), None)
    if not non_sa:
        return {'toll_zar': 0.0, 'breakdown': [], 'route': None}

    highway = _SA_HIGHWAY_FOR_BORDER.get(non_sa)
    if not highway:
        return {'toll_zar': 0.0, 'breakdown': [], 'route': None}

    from core.services.toll_calculator import TRUCK_TYPE_TO_CLASS, VEHICLE_TO_TOLL_TYPE_LOOKUP
    try:
        from core.models.toll_plaza import TollPlaza

        toll_truck = VEHICLE_TO_TOLL_TYPE_LOOKUP.get(vehicle_type, 'combination')
        vehicle_class = TRUCK_TYPE_TO_CLASS.get(toll_truck, 5)

        # For SZ via N4 only charge plazas within SA portion (~100km before Oshoek)
        sa_border_km = _FALLBACK_SA_BORDER_KM.get(non_sa, 999)
        plazas = (
            TollPlaza.objects
            .filter(route=highway, is_active=True, location_km__lte=sa_border_km)
            .order_by('location_km')
        )

        total = Decimal('0.00')
        breakdown = []
        for plaza in plazas:
            tariff = plaza.get_tariff(vehicle_class)
            total += tariff
            breakdown.append({'plaza': plaza.name, 'route': plaza.route, 'tariff': float(tariff)})

        return {'toll_zar': float(total), 'breakdown': breakdown, 'route': highway}
    except Exception as exc:
        logger.warning('SA cross-border toll lookup failed: %s', exc)
        return {'toll_zar': 0.0, 'breakdown': [], 'route': None}


def get_cross_border_warnings(countries: list[str]) -> list[str]:
    if not countries or len(countries) <= 1:
        return []
    warnings = []
    if 'ZW' in countries:
        warnings.append('Zimbabwe crossing: ensure cargo insurance and customs documentation')
    if any(c in countries for c in ['ZM', 'MW', 'TZ', 'KE']):
        warnings.append('Multi-country route: allow 2–3 days extra for border clearances')
    if len(countries) > 3:
        warnings.append('Long-haul cross-border: recommend experienced driver with valid passport')
    return warnings
