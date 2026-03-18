"""Toll calculator service for calculating SANRAL toll costs on SA routes."""

from typing import Dict, List, Optional
from core.models import TollPlaza


class TollCalculatorService:
    """
    Service for calculating toll costs on South African national routes.

    Supports major city pairs and automatically determines the route and
    applicable toll plazas.
    """

    ROUTE_MAP = {
        ('JHB', 'CPT'): ('N1', ['Grasmere Toll Plaza', 'Vaal Toll Plaza', 'Vanderkloof Toll Plaza', 'Touwsrivier Toll Plaza', 'Huguenot Tunnel']),
        ('CPT', 'JHB'): ('N1', ['Huguenot Tunnel', 'Touwsrivier Toll Plaza', 'Vanderkloof Toll Plaza', 'Vaal Toll Plaza', 'Grasmere Toll Plaza']),
        ('JHB', 'DBN'): ('N3', ['Wilge Toll Plaza', 'Tugela Toll Plaza', 'Mooi River Toll Plaza', 'Lynnfield Park Toll Plaza', 'Mariannhill Toll Plaza']),
        ('DBN', 'JHB'): ('N3', ['Mariannhill Toll Plaza', 'Lynnfield Park Toll Plaza', 'Mooi River Toll Plaza', 'Tugela Toll Plaza', 'Wilge Toll Plaza']),
        ('JHB', 'MAPUTO'): ('N4', ['Machadodorp Toll Plaza', 'Middelburg Toll Plaza', 'Nkomazi Toll Plaza']),
        ('MAPUTO', 'JHB'): ('N4', ['Nkomazi Toll Plaza', 'Middelburg Toll Plaza', 'Machadodorp Toll Plaza']),
        ('JHB', 'BEITBRIDGE'): ('N1', ['Grasmere Toll Plaza', 'Vaal Toll Plaza']),
        ('BEITBRIDGE', 'JHB'): ('N1', ['Vaal Toll Plaza', 'Grasmere Toll Plaza']),
        ('CPT', 'DBN'): ('N2', ['Storms River Toll Plaza', 'Tsitsikamma Toll Plaza']),
        ('DBN', 'CPT'): ('N2', ['Tsitsikamma Toll Plaza', 'Storms River Toll Plaza']),
        ('CPT', 'PE'): ('N2', ['Storms River Toll Plaza']),
        ('PE', 'CPT'): ('N2', ['Storms River Toll Plaza']),
        ('JHB', 'PE'): ('N1', ['Grasmere Toll Plaza', 'Vaal Toll Plaza']),
        ('PE', 'JHB'): ('N1', ['Vaal Toll Plaza', 'Grasmere Toll Plaza']),
        ('DBN', 'PE'): ('N2', ['Tsitsikamma Toll Plaza']),
        ('PE', 'DBN'): ('N2', ['Tsitsikamma Toll Plaza']),
        ('JHB', 'BLOEMFONTEIN'): ('N1', ['Vaal Toll Plaza']),
        ('BLOEMFONTEIN', 'JHB'): ('N1', ['Vaal Toll Plaza']),
        # N14 — JHB to Springbok (Northern Cape)
        ('JHB', 'SPRINGBOK'): ('N14', ['Grasmere Toll Plaza', 'Vaal Toll Plaza']),
        ('SPRINGBOK', 'JHB'): ('N14', ['Vaal Toll Plaza', 'Grasmere Toll Plaza']),
        ('JHB', 'UPINGTON'): ('N14', ['Grasmere Toll Plaza', 'Vaal Toll Plaza']),
        ('UPINGTON', 'JHB'): ('N14', ['Vaal Toll Plaza', 'Grasmere Toll Plaza']),
        ('DBN', 'BLOEMFONTEIN'): ('N3', ['Mariannhill Toll Plaza', 'Lynnfield Park Toll Plaza', 'Mooi River Toll Plaza', 'Tugela Toll Plaza', 'Wilge Toll Plaza']),
        ('BLOEMFONTEIN', 'DBN'): ('N3', ['Wilge Toll Plaza', 'Tugela Toll Plaza', 'Mooi River Toll Plaza', 'Lynnfield Park Toll Plaza', 'Mariannhill Toll Plaza']),
    }

    CITY_ALIASES = {
        'JOHANNESBURG': 'JHB',
        'JOBURG': 'JHB',
        'JHBURG': 'JHB',
        'GAUTENG': 'JHB',
        'PRETORIA': 'JHB',  # Pretoria is in Gauteng, same toll corridor
        'TSHWANE': 'JHB',
        'CAPE TOWN': 'CPT',
        'CAPETOWN': 'CPT',
        'DURBAN': 'DBN',
        'ETHEKWINI': 'DBN',
        'PORT ELIZABETH': 'PE',
        'GQEBERHA': 'PE',
        'BEIT BRIDGE': 'BEITBRIDGE',
        'BEIT-BRIDGE': 'BEITBRIDGE',
        'SPRINGBOK': 'SPRINGBOK',
        'UPINGTON': 'UPINGTON',
    }

    @staticmethod
    def normalize_city(city: str) -> str:
        """
        Normalize city name to standard abbreviation.

        Args:
            city: City name or abbreviation

        Returns:
            str: Normalized city abbreviation
        """
        city_upper = city.upper().strip()
        return TollCalculatorService.CITY_ALIASES.get(city_upper, city_upper)

    @staticmethod
    def calculate_tolls(
        origin_city: str,
        destination_city: str,
        truck_class: int = 5
    ) -> Dict:
        """
        Calculate total toll costs for a route between two cities.

        Args:
            origin_city: Origin city name or abbreviation
            destination_city: Destination city name or abbreviation
            truck_class: SANRAL vehicle class (2-5), default 5 for semi-trailers

        Returns:
            dict: Contains total_zar, plazas (list of dicts), route

        Raises:
            ValueError: If route not found or truck_class invalid
        """
        if truck_class not in [2, 3, 4, 5]:
            raise ValueError(f"Invalid truck_class: {truck_class}. Must be 2, 3, 4, or 5.")

        origin = TollCalculatorService.normalize_city(origin_city)
        destination = TollCalculatorService.normalize_city(destination_city)

        route_key = (origin, destination)

        if route_key not in TollCalculatorService.ROUTE_MAP:
            return {
                'total_zar': 0.0,
                'plazas': [],
                'route': 'Unknown',
                'error': f'No route data available for {origin} to {destination}'
            }

        route_code, plaza_names = TollCalculatorService.ROUTE_MAP[route_key]

        plazas_data = []
        total_cost = 0.0

        for plaza_name in plaza_names:
            plaza = TollPlaza.objects.filter(name=plaza_name, route=route_code).first()

            if plaza:
                cost = plaza.get_cost_for_class(truck_class)
                total_cost += cost

                plazas_data.append({
                    'name': plaza.name,
                    'route': plaza.route,
                    'province': plaza.province,
                    'cost': float(cost)
                })

        return {
            'total_zar': round(total_cost, 2),
            'plazas': plazas_data,
            'route': route_code
        }

    @staticmethod
    def get_supported_routes() -> List[Dict]:
        """
        Get list of all supported city pairs and routes.

        Returns:
            list: List of dicts with origin, destination, route
        """
        routes = []
        seen = set()

        for (origin, destination), (route_code, _) in TollCalculatorService.ROUTE_MAP.items():
            route_key = (origin, destination)
            if route_key not in seen:
                routes.append({
                    'origin': origin,
                    'destination': destination,
                    'route': route_code
                })
                seen.add(route_key)

        return routes


# ──────────────────────────────────────────────────────────────────────────────
# Module-level constants and functions (for test compatibility)
# ──────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass
from decimal import Decimal

TRUCK_TYPE_TO_CLASS = {
    'light': 2,
    'medium': 3,
    'heavy': 4,
    'combination': 5,
    'rigid': 3,
    'semi': 5,
    'interlink': 5,
}


@dataclass
class TollBreakdownItem:
    """Individual toll plaza cost."""
    plaza_name: str
    route: str
    cost_zar: Decimal
    location_km: Decimal = Decimal('0')


@dataclass
class TollResult:
    """Result of toll calculation."""
    total_zar: Decimal
    breakdown: List[TollBreakdownItem]
    routes_used: List[str]
    origin: str = ''
    destination: str = ''
    truck_type: str = ''
    vehicle_class: int = 0
    warning: Optional[str] = None


def _detect_routes(origin: str, destination: str) -> List[str]:
    """
    Detect routes for a city pair.

    Args:
        origin: Origin city
        destination: Destination city

    Returns:
        List of route codes (e.g., ['N3'])
    """
    origin_norm = TollCalculatorService.normalize_city(origin)
    dest_norm = TollCalculatorService.normalize_city(destination)

    key = (origin_norm, dest_norm)
    route_info = TollCalculatorService.ROUTE_MAP.get(key)

    if route_info:
        return [route_info[0]]
    return []


def calculate_tolls(origin: str, destination: str, truck_type: str) -> TollResult:
    """
    Calculate tolls for a route.

    Args:
        origin: Origin city
        destination: Destination city
        truck_type: Truck type string (maps to SANRAL class via TRUCK_TYPE_TO_CLASS)

    Returns:
        TollResult: Toll calculation result

    Raises:
        ValueError: If truck_type is unknown
    """
    if truck_type not in TRUCK_TYPE_TO_CLASS:
        raise ValueError(f'Unknown truck type: {truck_type}')

    vehicle_class = TRUCK_TYPE_TO_CLASS[truck_type]

    origin_norm = TollCalculatorService.normalize_city(origin)
    dest_norm = TollCalculatorService.normalize_city(destination)

    key = (origin_norm, dest_norm)

    if key not in TollCalculatorService.ROUTE_MAP:
        return TollResult(
            total_zar=Decimal('0.00'),
            breakdown=[],
            routes_used=[],
            origin=origin,
            destination=destination,
            truck_type=truck_type,
            vehicle_class=vehicle_class,
            warning=f'No route data for {origin} to {destination}'
        )

    route_code, plaza_names = TollCalculatorService.ROUTE_MAP[key]

    breakdown = []
    total = Decimal('0.00')

    for plaza_name in plaza_names:
        plaza = TollPlaza.objects.filter(name=plaza_name, route=route_code).first()

        if plaza and plaza.is_active:
            cost = plaza.get_tariff(vehicle_class)
            total += cost

            breakdown.append(TollBreakdownItem(
                plaza_name=plaza.name,
                route=plaza.route,
                cost_zar=cost,
                location_km=plaza.location_km
            ))

    return TollResult(
        total_zar=total,
        breakdown=breakdown,
        routes_used=[route_code],
        origin=origin,
        destination=destination,
        truck_type=truck_type,
        vehicle_class=vehicle_class,
        warning=None
    )
