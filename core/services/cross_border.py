"""
Cross-border route calculation service.

Determines countries crossed, calculates border fees, weighbridge fees,
and non-SA tolls for international routes.

Mobile app equivalent: src/lib/services/additionalCostsService.ts
"""

import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# Multi-hop route definitions (SA = South Africa)
MULTI_HOP_ROUTES = {
    ('SA', 'ZM'): ['SA', 'ZW', 'ZM'],      # SA → Zimbabwe → Zambia
    ('SA', 'MW'): ['SA', 'ZW', 'MW'],      # SA → Zimbabwe → Malawi
    ('SA', 'TZ'): ['SA', 'ZW', 'ZM', 'TZ'], # SA → Zimbabwe → Zambia → Tanzania
    ('SA', 'KE'): ['SA', 'ZW', 'ZM', 'TZ', 'KE'], # SA → Zimbabwe → Zambia → Tanzania → Kenya
}

# Direct cross-border routes
DIRECT_ROUTES = ['ZW', 'MZ', 'BW', 'NA', 'LS', 'SZ']  # Zimbabwe, Mozambique, Botswana, Namibia, Lesotho, eSwatini

# Hardcoded cross-border costs (ZAR) - will be replaced with DB models when migration is ready
BORDER_FEES = {
    'SA-ZW': 850,
    'SA-BW': 650,
    'SA-NA': 600,
    'SA-MZ': 750,
    'SA-LS': 300,
    'SA-SZ': 250,
    'ZW-ZM': 900,
    'ZW-MW': 850,
    'ZM-TZ': 1200,
    'TZ-KE': 1100,
}

WEIGHBRIDGE_FEES = {
    'SA': 180,
    'ZW': 250,
    'BW': 200,
    'NA': 180,
    'MZ': 220,
    'ZM': 280,
    'MW': 260,
    'TZ': 320,
    'KE': 300,
}

# Average non-SA toll costs per km (ZAR)
NON_SA_TOLL_RATES = {
    'ZW': 0.45,
    'BW': 0.30,
    'NA': 0.25,
    'MZ': 0.40,
    'ZM': 0.50,
    'MW': 0.45,
    'TZ': 0.55,
    'KE': 0.60,
}


def detect_countries(origin: str, destination: str) -> Optional[List[str]]:
    """
    Detect which countries are crossed based on origin and destination.

    Args:
        origin: Origin location string (city or country code)
        destination: Destination location string (city or country code)

    Returns:
        List of country codes in route order, or None if domestic SA route
    """
    origin_upper = origin.upper().strip()
    dest_upper = destination.upper().strip()

    # Extract country codes from city names or direct codes
    origin_country = _extract_country(origin_upper)
    dest_country = _extract_country(dest_upper)

    # Domestic SA route
    if origin_country == 'SA' and dest_country == 'SA':
        return None

    # Check multi-hop routes
    route_key = (origin_country, dest_country)
    if route_key in MULTI_HOP_ROUTES:
        return MULTI_HOP_ROUTES[route_key]

    # Direct cross-border
    if origin_country == 'SA' and dest_country in DIRECT_ROUTES:
        return ['SA', dest_country]

    # Reverse direction
    if dest_country == 'SA' and origin_country in DIRECT_ROUTES:
        return [origin_country, 'SA']

    # Default: assume SA if not recognized
    return None


def _extract_country(location: str) -> str:
    """Extract country code from location string."""
    # South Africa cities
    if any(city in location for city in ['JHB', 'JOHANNESBURG', 'CPT', 'CAPE TOWN', 'CAPETOWN',
                                          'DUR', 'DURBAN', 'PE', 'PORT ELIZABETH', 'PTA', 'PRETORIA',
                                          'BFN', 'BLOEMFONTEIN', 'PLZ', 'PORT LOUIS']):
        return 'SA'

    # Zimbabwe
    if any(city in location for city in ['HARARE', 'BULAWAYO', 'ZW']):
        return 'ZW'

    # Zambia
    if any(city in location for city in ['LUSAKA', 'NDOLA', 'ZM']):
        return 'ZM'

    # Mozambique
    if any(city in location for city in ['MAPUTO', 'BEIRA', 'MZ']):
        return 'MZ'

    # Botswana
    if any(city in location for city in ['GABORONE', 'FRANCISTOWN', 'BW']):
        return 'BW'

    # Namibia
    if any(city in location for city in ['WINDHOEK', 'WALVIS', 'NA']):
        return 'NA'

    # Malawi
    if any(city in location for city in ['LILONGWE', 'BLANTYRE', 'MW']):
        return 'MW'

    # Tanzania
    if any(city in location for city in ['DAR ES SALAAM', 'DODOMA', 'TZ']):
        return 'TZ'

    # Kenya
    if any(city in location for city in ['NAIROBI', 'MOMBASA', 'KE']):
        return 'KE'

    # Lesotho
    if 'MASERU' in location or 'LS' in location:
        return 'LS'

    # eSwatini
    if 'MBABANE' in location or 'SZ' in location or 'SWAZILAND' in location:
        return 'SZ'

    # Default to SA
    return 'SA'


def calculate_cross_border_costs(
    countries: List[str],
    distance_km: float,
    vehicle_type: str = 'truck',
) -> Dict[str, Any]:
    """
    Calculate cross-border costs: border fees, weighbridge fees, non-SA tolls.

    Args:
        countries: List of country codes in route order
        distance_km: Total route distance in km
        vehicle_type: Vehicle type (truck, flatbed, etc.)

    Returns:
        Dict with border_fees, weighbridge_fees, non_sa_tolls (all in ZAR),
        and breakdown details
    """
    if not countries or len(countries) <= 1:
        return {
            'border_fees': 0,
            'weighbridge_fees': 0,
            'non_sa_tolls': 0,
            'total': 0,
            'breakdown': [],
        }

    border_fees = 0
    weighbridge_fees = 0
    non_sa_tolls = 0
    breakdown = []

    # Border crossing fees
    for i in range(len(countries) - 1):
        from_country = countries[i]
        to_country = countries[i + 1]
        border_key = f"{from_country}-{to_country}"

        fee = BORDER_FEES.get(border_key, 0)
        if fee > 0:
            border_fees += fee
            breakdown.append({
                'type': 'border_crossing',
                'description': f'{from_country} → {to_country} border crossing',
                'amount': fee,
            })

    # Weighbridge fees (one per country except SA)
    for country in countries:
        if country != 'SA':
            fee = WEIGHBRIDGE_FEES.get(country, 200)
            weighbridge_fees += fee
            breakdown.append({
                'type': 'weighbridge',
                'description': f'{country} weighbridge',
                'amount': fee,
            })

    # Non-SA toll costs (distance-based)
    # Estimate distance per country (simple division for now)
    non_sa_countries = [c for c in countries if c != 'SA']
    if non_sa_countries:
        distance_per_country = distance_km / len(countries)
        for country in non_sa_countries:
            rate = NON_SA_TOLL_RATES.get(country, 0.40)
            toll_cost = distance_per_country * rate
            non_sa_tolls += toll_cost
            breakdown.append({
                'type': 'non_sa_toll',
                'description': f'{country} tolls (~{int(distance_per_country)}km)',
                'amount': round(toll_cost, 2),
            })

    total = border_fees + weighbridge_fees + non_sa_tolls

    return {
        'border_fees': round(border_fees, 2),
        'weighbridge_fees': round(weighbridge_fees, 2),
        'non_sa_tolls': round(non_sa_tolls, 2),
        'total': round(total, 2),
        'breakdown': breakdown,
    }


def get_cross_border_warnings(countries: List[str]) -> List[str]:
    """Generate warnings for cross-border routes."""
    if not countries or len(countries) <= 1:
        return []

    warnings = []

    if 'ZW' in countries:
        warnings.append('Zimbabwe crossing: ensure cargo insurance and customs documentation')

    if any(c in countries for c in ['ZM', 'MW', 'TZ', 'KE']):
        warnings.append('Multi-country route: allow 2-3 days extra for border clearances')

    if len(countries) > 3:
        warnings.append('Long-haul cross-border: recommend experienced driver with valid passport')

    return warnings
