"""True margin calculator service integrating fuel, tolls, and RFA benchmarks."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, Optional

from core.services.fuel_price import FuelPriceService
from core.services.toll_calculator import TollCalculatorService
from core.services.rfa_benchmarks import RFABenchmarkService
from core.models import Company


# ──────────────────────────────────────────────────────────────────────────────
# Module-level constants (for test compatibility)
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_DEADHEAD_FACTOR = Decimal('1.30')  # 30% deadhead
DEFAULT_DRIVER_RATE_PER_KM = Decimal('1.20')
DEFAULT_KM_PER_LITRE = Decimal('2.80')  # articulated default
DEFAULT_MAINTENANCE_PER_KM = Decimal('0.85')
DEFAULT_TYRE_WEAR_PER_KM = Decimal('0.60')
_FALLBACK_DIESEL_PRICE = Decimal('22.00')


@dataclass
class MarginResult:
    """Result of margin calculation."""
    true_cost: Decimal
    margin_zar: Decimal
    margin_pct: Decimal
    cost_breakdown: Dict[str, Decimal]
    fuel_price_used: Decimal
    margin_status: str
    distance_km: Decimal = Decimal('0')
    effective_distance_km: Decimal = Decimal('0')


class TrueMarginCalculatorService:
    """
    Service for calculating true margin on freight quotes.

    Integrates:
    - Current fuel prices (FuelPriceService)
    - SANRAL toll costs (TollCalculatorService)
    - RFA vehicle cost benchmarks (RFABenchmarkService)
    - Deadhead calculations
    - Load type premiums
    """

    LOAD_TYPE_MULTIPLIERS = {
        'refrigerated': Decimal('1.15'),
        'hazmat': Decimal('1.25'),
        'general': Decimal('1.00'),
        'bulk': Decimal('1.00'),
        'abnormal': Decimal('1.00'),
    }

    @staticmethod
    def calculate(
        origin: str,
        destination: str,
        distance_km: float,
        truck_type: str,
        load_type: str,
        quote_price: float,
        has_return_load: bool = False,
        company: Optional[Company] = None
    ) -> Dict:
        """
        Calculate true margin and cost breakdown for a freight quote.

        Args:
            origin: Origin city
            destination: Destination city
            distance_km: Total distance in kilometers
            truck_type: Truck type (semi_34t, rigid_8t, flatbed, tipper, reefer, tanker)
            load_type: Load type (general, refrigerated, hazmat, bulk, abnormal)
            quote_price: Quoted price in ZAR
            has_return_load: Whether there's a return load (affects deadhead cost)
            company: Optional company for cost profile overrides

        Returns:
            dict: Contains true_cost, margin_zar, margin_pct, cost_breakdown,
                  fuel_price_used, margin_status
        """
        cost_profile = RFABenchmarkService.get_profile(truck_type, company)

        if not cost_profile:
            return {
                'error': f'No cost profile found for truck type: {truck_type}',
                'true_cost': 0.0,
                'margin_zar': 0.0,
                'margin_pct': 0.0,
                'cost_breakdown': {},
                'fuel_price_used': 0.0,
                'margin_status': 'unknown'
            }

        fuel_price_record = FuelPriceService.get_latest()
        if not fuel_price_record:
            fuel_price_record = FuelPriceService.get_price_for_date(date.today())

        fuel_price_per_liter = float(fuel_price_record.diesel_inland) if fuel_price_record else 17.59

        toll_data = TollCalculatorService.calculate_tolls(
            origin_city=origin,
            destination_city=destination,
            truck_class=5
        )
        toll_cost = toll_data.get('total_zar', 0.0)

        distance_decimal = Decimal(str(distance_km))

        fuel_cost = float(cost_profile.fuel_cpk * distance_decimal)
        tyre_wear = float(cost_profile.tyre_cpk * distance_decimal)
        maintenance = float(cost_profile.maintenance_cpk * distance_decimal)

        avg_speed_kmh = 80
        hours_on_road = distance_km / avg_speed_kmh
        days_on_road = max(1, hours_on_road / 10)

        load_multiplier = TrueMarginCalculatorService.LOAD_TYPE_MULTIPLIERS.get(
            load_type.lower(),
            Decimal('1.00')
        )
        driver_cost = float(cost_profile.driver_cost_per_day * Decimal(str(days_on_road)) * load_multiplier)

        if has_return_load:
            deadhead_cost = 0.0
        else:
            deadhead_distance = distance_km * 0.30
            deadhead_cost = float(cost_profile.fuel_cpk * Decimal(str(deadhead_distance)))

        true_cost = fuel_cost + driver_cost + toll_cost + tyre_wear + maintenance + deadhead_cost

        margin_zar = quote_price - true_cost
        margin_pct = (margin_zar / quote_price * 100) if quote_price > 0 else 0.0

        if margin_pct >= 18:
            margin_status = 'healthy'
        elif margin_pct >= 12:
            margin_status = 'caution'
        else:
            margin_status = 'at_risk'

        return {
            'true_cost': round(true_cost, 2),
            'margin_zar': round(margin_zar, 2),
            'margin_pct': round(margin_pct, 2),
            'cost_breakdown': {
                'fuel_cost': round(fuel_cost, 2),
                'driver_cost': round(driver_cost, 2),
                'toll_cost': round(toll_cost, 2),
                'tyre_wear': round(tyre_wear, 2),
                'maintenance': round(maintenance, 2),
                'deadhead_cost': round(deadhead_cost, 2),
            },
            'fuel_price_used': round(fuel_price_per_liter, 2),
            'margin_status': margin_status
        }


# ──────────────────────────────────────────────────────────────────────────────
# Module-level function (for test compatibility)
# ──────────────────────────────────────────────────────────────────────────────

def calculate_true_margin(
    route: dict,
    truck_type: str,
    load_type: str,
    quote_price: Decimal,
    client_id: Optional[int] = None
) -> MarginResult:
    """
    Calculate true margin for a freight quote.

    This is a test-compatible wrapper around the service implementation.

    Args:
        route: Dict with distance_km, optional tolls_zar, optional deadhead_factor
        truck_type: Truck type (articulated, rigid, etc.)
        load_type: Load type (general, hazmat, refrigerated, bulk, abnormal)
        quote_price: Quoted price in ZAR
        client_id: Optional client ID (reserved for future use)

    Returns:
        MarginResult: Margin calculation result

    Raises:
        ValueError: If distance_km is invalid
    """
    from core.models import FuelPrice

    distance_km = Decimal(str(route.get('distance_km', 0)))

    if distance_km <= 0:
        raise ValueError('distance_km must be positive')

    tolls_zar = Decimal(str(route.get('tolls_zar', 0)))
    deadhead_factor = Decimal(str(route.get('deadhead_factor', DEFAULT_DEADHEAD_FACTOR)))

    # Calculate effective distance with deadhead
    effective_distance_km = distance_km * deadhead_factor

    # Get fuel price
    latest_fuel = FuelPrice.objects.order_by('-date').first()
    if latest_fuel:
        diesel_price = latest_fuel.diesel_inland
    else:
        diesel_price = _FALLBACK_DIESEL_PRICE

    # Determine km per litre based on truck type
    km_per_litre_map = {
        'rigid': Decimal('4.50'),
        'articulated': DEFAULT_KM_PER_LITRE,
    }
    km_per_litre = km_per_litre_map.get(truck_type, DEFAULT_KM_PER_LITRE)

    # Calculate costs
    fuel_cost = (effective_distance_km / km_per_litre * diesel_price).quantize(Decimal('0.01'))
    driver_cost = (effective_distance_km * DEFAULT_DRIVER_RATE_PER_KM).quantize(Decimal('0.01'))
    tyre_wear = (effective_distance_km * DEFAULT_TYRE_WEAR_PER_KM).quantize(Decimal('0.01'))
    maintenance = (effective_distance_km * DEFAULT_MAINTENANCE_PER_KM).quantize(Decimal('0.01'))

    cost_breakdown = {
        'fuel': fuel_cost,
        'driver': driver_cost,
        'tolls': tolls_zar.quantize(Decimal('0.01')),
        'tyre_wear': tyre_wear,
        'maintenance': maintenance,
    }

    true_cost = sum(cost_breakdown.values()).quantize(Decimal('0.01'))
    margin_zar = (quote_price - true_cost).quantize(Decimal('0.01'))
    margin_pct = (margin_zar / quote_price * Decimal('100')).quantize(Decimal('0.01')) if quote_price > 0 else Decimal('0.00')

    # Determine margin status
    if margin_pct >= 18:
        margin_status = 'healthy'
    elif margin_pct >= 12:
        margin_status = 'caution'
    else:
        margin_status = 'at_risk'

    return MarginResult(
        true_cost=true_cost,
        margin_zar=margin_zar,
        margin_pct=margin_pct,
        cost_breakdown=cost_breakdown,
        fuel_price_used=diesel_price,
        margin_status=margin_status,
        distance_km=distance_km,
        effective_distance_km=effective_distance_km.quantize(Decimal('0.01'))
    )
