"""
Revenue Guard Engine - Real-time quote safety checks to prevent revenue leakage.

Guards against:
- Low margins (<12%)
- High fuel ratios (>55%)
- Excessive deadhead (>35%)
- Slow payers (>60 day avg)
"""

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


class RevenueGuardEngine:
    """
    Real-time quote validation engine to flag risky quotes before acceptance.

    Rules:
    1. Margin < 12% → CRITICAL
    2. Fuel cost > 55% of price → WARNING
    3. Deadhead > 35% → WARNING
    4. Client avg payment days > 60 → CAUTION
    """

    MIN_MARGIN_PCT = 12.0
    MAX_FUEL_RATIO = 0.55
    MAX_DEADHEAD_FRACTION = 0.35
    MAX_AVG_PAYMENT_DAYS = 60

    RATING_SAFE = 'SAFE'
    RATING_CAUTION = 'CAUTION'
    RATING_WARNING = 'WARNING'
    RATING_CRITICAL = 'CRITICAL'

    def check(
        self,
        quote_price: float,
        distance_km: float,
        load_type: str,
        truck_type: str,
        client: Optional[any] = None,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        fuel_cost: Optional[float] = None,
        deadhead_km: Optional[float] = None,
    ) -> dict:
        """
        Run all Revenue Guard rules and return risk assessment.

        Args:
            quote_price: Quoted price in ZAR
            distance_km: Route distance
            load_type: Load type string
            truck_type: Truck type string
            client: Optional client instance (for payment history)
            origin: Origin city
            destination: Destination city
            fuel_cost: Optional fuel cost (calculated if not provided)
            deadhead_km: Optional deadhead distance

        Returns:
            dict: {
                safe: bool,
                risk_score: float (0-100, higher = riskier),
                rating: str (SAFE|CAUTION|WARNING|CRITICAL),
                warnings: [{code, message, severity}, ...],
                suggested_safe_price: float
            }
        """
        warnings = []
        risk_score = 0.0

        # Calculate true cost (simplified - use margin calculator in production)
        # For now, estimate: fuel 40%, driver 20%, tolls 10%, maintenance 10%, overhead 8%
        if fuel_cost is None:
            fuel_cpk = 6.5  # Default ZAR per km
            fuel_cost = distance_km * fuel_cpk

        estimated_cost = fuel_cost / 0.40  # Fuel is ~40% of total cost

        # Rule 1: Check margin
        margin = quote_price - estimated_cost
        margin_pct = (margin / quote_price) * 100 if quote_price > 0 else 0

        if margin_pct < self.MIN_MARGIN_PCT:
            risk_score += 40
            warnings.append({
                'code': 'LOW_MARGIN',
                'message': f'Margin {margin_pct:.1f}% below minimum {self.MIN_MARGIN_PCT}%',
                'severity': 'CRITICAL',
            })

        # Rule 2: Check fuel ratio
        fuel_ratio = fuel_cost / quote_price if quote_price > 0 else 1.0

        if fuel_ratio > self.MAX_FUEL_RATIO:
            risk_score += 25
            warnings.append({
                'code': 'HIGH_FUEL_RATIO',
                'message': f'Fuel cost {fuel_ratio*100:.0f}% of price (max {self.MAX_FUEL_RATIO*100:.0f}%)',
                'severity': 'WARNING',
            })

        # Rule 3: Check deadhead
        if deadhead_km is not None and distance_km > 0:
            deadhead_fraction = deadhead_km / distance_km

            if deadhead_fraction > self.MAX_DEADHEAD_FRACTION:
                risk_score += 20
                warnings.append({
                    'code': 'HIGH_DEADHEAD',
                    'message': f'Deadhead {deadhead_fraction*100:.0f}% of trip (max {self.MAX_DEADHEAD_FRACTION*100:.0f}%)',
                    'severity': 'WARNING',
                })

        # Rule 4: Check client payment history
        if client is not None:
            avg_payment_days = getattr(client, 'avg_days_to_pay', None)
            if avg_payment_days and avg_payment_days > self.MAX_AVG_PAYMENT_DAYS:
                risk_score += 15
                warnings.append({
                    'code': 'SLOW_PAYER',
                    'message': f'Client averages {avg_payment_days:.0f} days to pay (max {self.MAX_AVG_PAYMENT_DAYS})',
                    'severity': 'CAUTION',
                })

        # Determine rating
        if risk_score >= 40:
            rating = self.RATING_CRITICAL
        elif risk_score >= 25:
            rating = self.RATING_WARNING
        elif risk_score >= 10:
            rating = self.RATING_CAUTION
        else:
            rating = self.RATING_SAFE

        # Suggest safe price
        safe_margin_pct = max(self.MIN_MARGIN_PCT, margin_pct + 5)  # Add 5% buffer
        suggested_safe_price = estimated_cost / (1 - safe_margin_pct / 100)

        return {
            'safe': len(warnings) == 0,
            'risk_score': float(risk_score),
            'rating': rating,
            'warnings': warnings,
            'suggested_safe_price': float(suggested_safe_price),
            'margin_pct': float(margin_pct),
            'fuel_ratio_pct': float(fuel_ratio * 100),
        }
