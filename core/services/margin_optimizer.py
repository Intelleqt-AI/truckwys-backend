"""
Margin / price optimizer for SA freight quotes.

Pure, dependency-light service that searches the price axis and picks the price
that MAXIMISES EXPECTED PROFIT = (price - cost) * P(win | price).

The candidate-price band is anchored on the market rate (0.75x .. 1.35x) with a
floor at cost * (1 + min_margin), so the optimum can sit BELOW the caller's
current price when the market supports it — the cost basis only defines the
profit baseline, not the search space.

Win probability is sourced from the existing WinProbabilityModel in
core.services.quote_ml so behaviour stays consistent with the rest of the
quoting stack (it transparently falls back to a heuristic ladder when no
trained model is on disk).

The module never raises: every public path is wrapped and returns a sane
fallback dict so API callers can rely on a stable shape.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Price band anchors relative to the market rate.
MARKET_BAND_LOW = 0.75
MARKET_BAND_HIGH = 1.35


# Map the human-friendly client_tier strings used across the quoting API to the
# integer codes the WinProbabilityModel expects (0=new, 1=regular, 2=vip).
_TIER_TO_INT = {
    'new': 0,
    'standard': 1,
    'regular': 1,
    'vip': 2,
    'premium': 2,
}


def _tier_to_int(client_tier: Any) -> int:
    """Coerce a client_tier (str or int) to the model's integer code."""
    if isinstance(client_tier, (int, float)):
        try:
            return max(0, min(2, int(client_tier)))
        except (ValueError, TypeError):
            return 1
    return _TIER_TO_INT.get(str(client_tier).strip().lower(), 1)


def _fallback(total_cost: float, min_margin: float, max_margin: float) -> Dict[str, Any]:
    """Return a no-curve, mid-margin result when optimisation can't run."""
    mid_margin = (min_margin + max_margin) / 2.0
    price = total_cost * (1.0 + mid_margin)
    profit = price - total_cost
    return {
        'optimal_price': round(price, 2),
        'optimal_margin_pct': round(mid_margin * 100.0, 1),
        'win_probability_at_optimal': None,
        'expected_profit': round(profit, 2),
        'curve': [],
    }


def _route_popularity(origin: Optional[str], destination: Optional[str]) -> float:
    """Lane quote volume over the past 90 days, normalized against the busiest
    lane. Lane identity is canonicalized (DUR == DBN etc.) on both the numerator
    and the denominator so historical spellings never fragment a lane.
    Snapshotted onto QuoteOutcome at capture time so training sees the exact
    same definition. Returns 0.5 when unknown."""
    if not origin or not destination:
        return 0.5
    try:
        from datetime import timedelta
        from django.utils import timezone
        from django.db.models import Count
        from core.models import Quote
        from core.services.lane_benchmark import canon_code, _lane_q

        since = timezone.now() - timedelta(days=90)
        lane_n = Quote.objects.filter(
            _lane_q('origin', origin), _lane_q('destination', destination),
            created_at__gte=since,
        ).count()
        # Busiest lane, grouped on CANONICAL codes so alias spellings pool.
        by_lane: dict = {}
        rows = (
            Quote.objects.filter(created_at__gte=since)
            .exclude(origin='').exclude(destination='')
            .values('origin', 'destination')
            .annotate(n=Count('id'))
        )
        for row in rows:
            key = (canon_code(row['origin']), canon_code(row['destination']))
            by_lane[key] = by_lane.get(key, 0) + row['n']
        top_n = max(by_lane.values()) if by_lane else 0
        if not top_n:
            return 0.5
        return max(0.0, min(1.0, lane_n / top_n))
    except Exception as exc:
        logger.debug('route popularity lookup failed: %s', exc)
        return 0.5


def optimize_price(
    total_cost: float,
    market_rate: float,
    client_tier: str = 'standard',
    days_until_departure: int = 7,
    historical_acceptance_rate: float = 0.5,
    min_margin: float = 0.05,
    max_margin: float = 0.45,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Find the price that maximises expected profit over a market-anchored band.

    Args:
        total_cost: Carrier's direct operating cost for the job (ZAR). Must be > 0.
        market_rate: Benchmark market price for the lane (ZAR). If <= 0, the
            win curve cannot be computed and a mid-margin fallback is returned.
        client_tier: 'new' | 'standard'/'regular' | 'vip'/'premium' (or int 0-2).
        days_until_departure: Urgency in days (lower = more urgent).
        historical_acceptance_rate: Client's past acceptance rate [0, 1].
        min_margin: Markup floor over cost — no candidate price sits below
            total_cost * (1 + min_margin).
        max_margin: Markup ceiling used only when the market cannot anchor the
            band (cost at/above market, or the mid-margin fallback).
        origin/destination: Optional lane codes for the route-popularity feature.

    Returns:
        {
            'optimal_price': float,
            'optimal_margin_pct': float,            # markup over total_cost, e.g. 18.0
            'win_probability_at_optimal': float|None,
            'expected_profit': float,
            'curve': [
                {'price', 'margin_pct', 'win_probability', 'expected_profit'},
                ...  # ~12 downsampled points for the UI
            ],
        }
    """
    try:
        total_cost = float(total_cost)
        market_rate = float(market_rate)

        if total_cost <= 0:
            # Nothing sensible to optimise around — return a zeroed shape.
            return {
                'optimal_price': 0.0,
                'optimal_margin_pct': round(((min_margin + max_margin) / 2.0) * 100.0, 1),
                'win_probability_at_optimal': None,
                'expected_profit': 0.0,
                'curve': [],
            }

        # Without a positive market rate we cannot form a price_ratio, so we
        # cannot build a win curve. Fall back to a mid-margin price.
        if market_rate <= 0:
            return _fallback(total_cost, min_margin, max_margin)

        # Defensive: keep the band ordered and sane.
        if max_margin < min_margin:
            min_margin, max_margin = max_margin, min_margin

        tier_int = _tier_to_int(client_tier)
        days = int(days_until_departure) if days_until_departure is not None else 7
        try:
            hist = float(historical_acceptance_rate)
        except (TypeError, ValueError):
            hist = 0.5
        hist = max(0.0, min(1.0, hist))

        # Build the win-probability model. When the ML libraries are missing,
        # WinProbabilityModel.__init__ raises before it can serve its built-in
        # heuristic. The heuristic branch of predict_proba only reads
        # ``self.model`` (which is None when untrained), so we reuse that exact
        # code by binding predict_proba to a tiny stand-in carrying model=None.
        # This keeps win-probability behaviour identical to the rest of the
        # quoting stack without importing numpy/sklearn here.
        try:
            from core.services.quote_ml import WinProbabilityModel
        except Exception as exc:
            logger.warning('margin_optimizer: cannot import WinProbabilityModel (%s); using fallback', exc)
            return _fallback(total_cost, min_margin, max_margin)

        try:
            win_model = WinProbabilityModel()
        except Exception as exc:  # ML libs missing — fall back to the heuristic.
            logger.info('margin_optimizer: WinProbabilityModel init failed (%s); using its heuristic', exc)

            class _HeuristicWinModel:
                model = None

            win_model = _HeuristicWinModel()
            win_model.predict_proba = WinProbabilityModel.predict_proba.__get__(win_model)

        # Candidate prices are anchored on the market rate, floored at cost plus
        # the minimum markup. When cost sits at/above the market band, fall back
        # to sweeping the markup band over cost so the band is never inverted.
        price_lo = max(total_cost * (1.0 + min_margin), market_rate * MARKET_BAND_LOW)
        price_hi = max(market_rate * MARKET_BAND_HIGH, total_cost * (1.0 + max_margin))

        # Point-in-time context features so a trained model sees the same
        # feature distribution it was fitted on (no hardcoded defaults).
        from django.utils import timezone
        now = timezone.now()
        month, day_of_week = now.month, now.weekday()
        popularity = _route_popularity(origin, destination)

        steps = 40
        full_curve: List[Dict[str, float]] = []
        best = None  # (expected_profit, point_dict)

        for i in range(steps + 1):
            frac = i / steps
            price = price_lo + frac * (price_hi - price_lo)
            margin = (price - total_cost) / total_cost
            price_ratio = price / market_rate

            try:
                p_win = win_model.predict_proba(
                    price_ratio=price_ratio,
                    client_tier=tier_int,
                    days_until_departure=days,
                    historical_acceptance_rate=hist,
                    month=month,
                    day_of_week=day_of_week,
                    route_popularity=popularity,
                )
            except Exception:
                p_win = 0.0
            p_win = max(0.0, min(1.0, float(p_win)))

            expected_profit = (price - total_cost) * p_win

            point = {
                'price': round(price, 2),
                'margin_pct': round(margin * 100.0, 1),
                'win_probability': round(p_win, 4),
                'expected_profit': round(expected_profit, 2),
            }
            full_curve.append(point)

            if best is None or expected_profit > best[0]:
                best = (expected_profit, point, price, margin, p_win)

        if best is None:
            return _fallback(total_cost, min_margin, max_margin)

        _, best_point, best_price, best_margin, best_pwin = best

        # Downsample the curve to ~12 evenly spaced points for the UI, always
        # keeping the first and last points.
        target_points = 12
        if len(full_curve) <= target_points:
            curve = full_curve
        else:
            n = len(full_curve)
            idxs = sorted({
                round(j * (n - 1) / (target_points - 1)) for j in range(target_points)
            })
            curve = [full_curve[k] for k in idxs]

        return {
            'optimal_price': round(best_price, 2),
            'optimal_margin_pct': round(best_margin * 100.0, 1),
            'win_probability_at_optimal': round(best_pwin, 4),
            'expected_profit': round((best_price - total_cost) * best_pwin, 2),
            'curve': curve,
        }

    except Exception as exc:
        logger.exception('margin_optimizer.optimize_price failed: %s', exc)
        try:
            return _fallback(float(total_cost), min_margin, max_margin)
        except Exception:
            return {
                'optimal_price': 0.0,
                'optimal_margin_pct': 0.0,
                'win_probability_at_optimal': None,
                'expected_profit': 0.0,
                'curve': [],
            }
