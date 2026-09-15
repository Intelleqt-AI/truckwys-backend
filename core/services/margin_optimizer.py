"""
Margin / price optimizer for SA freight quotes.

Pure, dependency-light service that searches the price axis and picks the price
that MAXIMISES EXPECTED PROFIT = (price - cost) * P(win | price), subject to
optional business-constraint floors/ceilings (see min_win_probability and
max_market_deviation below).

The candidate-price band is anchored on the market rate (0.75x .. 1.35x, the
upper multiple overridable via max_market_deviation) with a floor at
cost * (1 + min_margin), so the optimum can sit BELOW the caller's current
price when the market supports it — the cost basis only defines the profit
baseline, not the search space.

Win probability comes from an injected `predict_proba_fn(features: dict) ->
float` — this module has NO opinion on where that callable comes from (a
resolved per-user model, a global model, or the heuristic fallback in
core.services.win_prediction) and never constructs a WinProbabilityModel
itself; core.services.win_prediction.resolve_prediction_context() is the one
place that decision gets made. Callers that don't pass one get the heuristic
directly — never silently a "trained" model, and never presented as one.

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


def _route_popularity(origin: Optional[str], destination: Optional[str], as_of=None) -> float:
    """Lane quote volume over the 90 days before `as_of` (defaults to now),
    normalized against the busiest lane in that same window. Lane identity is
    canonicalized (DUR == DBN etc.) on both the numerator and the denominator
    so historical spellings never fragment a lane. Snapshotted onto
    QuoteOutcome at capture time so training sees the exact same definition.
    Returns 0.5 when unknown.

    `as_of` bounds the window on BOTH ends (not just the 90-day floor) so a
    historical training row reconstructed against this function never sees
    lane activity that happened after the quote it's describing — see
    core.services.quote_features for why this cutoff matters.
    """
    if not origin or not destination:
        return 0.5
    try:
        from datetime import timedelta
        from django.utils import timezone
        from django.db.models import Count
        from core.models import Quote
        from core.services.lane_benchmark import canon_code, _lane_q

        as_of = as_of or timezone.now()
        since = as_of - timedelta(days=90)
        # created_at__lte (not __lt): on a coarse system clock, rows created in
        # rapid succession just before `as_of` is captured can share its exact
        # timestamp -- strict '<' would then wrongly exclude a genuinely-prior
        # row (confirmed: this collapsed to the same microsecond on Windows in
        # a fast-running test). Safe either way since the row this prediction
        # is FOR is always excluded separately, by id, not by this cutoff.
        lane_n = Quote.objects.filter(
            _lane_q('origin', origin), _lane_q('destination', destination),
            created_at__gte=since, created_at__lte=as_of,
        ).count()
        # Busiest lane, grouped on CANONICAL codes so alias spellings pool.
        by_lane: dict = {}
        rows = (
            Quote.objects.filter(created_at__gte=since, created_at__lte=as_of)
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
    predict_proba_fn=None,
    base_features: Optional[Dict[str, Any]] = None,
    min_win_probability: float = 0.0,
    max_market_deviation: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Find the price that maximises expected profit over a market-anchored band,
    subject to optional constraints.

    Args:
        total_cost: Carrier's direct operating cost for the job (ZAR). Must be > 0.
        market_rate: Benchmark market price for the lane (ZAR). If <= 0, the
            win curve cannot be computed and a mid-margin fallback is returned.
        client_tier: 'new' | 'standard'/'regular' | 'vip'/'premium' (or int 0-2).
            Ignored when base_features is given (its 'client_tier' wins).
        days_until_departure: Urgency in days (lower = more urgent). Ignored
            when base_features is given.
        historical_acceptance_rate: Client's past acceptance rate [0, 1].
            Ignored when base_features is given.
        min_margin: Markup floor over cost — STRUCTURAL: no candidate price is
            ever generated below total_cost * (1 + min_margin), so this can
            never be violated by the result (unlike the two constraints below).
        max_margin: Markup ceiling used only when the market cannot anchor the
            band (cost at/above market, or the mid-margin fallback).
        origin/destination: Optional lane codes for the route-popularity
            feature. Ignored when base_features is given.
        predict_proba_fn: features:dict -> win probability. Defaults to
            core.services.win_prediction.heuristic_win_proba when omitted —
            this function never constructs or resolves a real trained model
            itself; that decision belongs to whoever calls it.
        base_features: The FULL feature dict for this quote (see
            core.services.quote_features.compute_features), used as-is except
            'price_ratio' and 'quoted_margin_pct', which get recomputed for
            EVERY swept candidate price (both are direct functions of the
            price being evaluated — leaving them frozen at whatever the
            caller's original total_amount produced would feed the model an
            internally-contradictory row for every other candidate, e.g. a
            price 38% above market still reporting the 0% margin of today's
            unedited total). When omitted, a minimal dict is built from the
            named params above
            (client_tier/days_until_departure/historical_acceptance_rate/
            route_popularity/current month & weekday) — this is what keeps
            existing callers that only pass the handful of legacy params
            working unchanged.
        min_win_probability: Soft floor in [0, 1]. The optimum is chosen only
            from candidates meeting this floor; if NONE do (e.g. an
            unrealistic floor for this market), the unconstrained argmax is
            used instead and the result reports constraints_relaxed=True —
            this function always returns a price, never "no recommendation".
        max_market_deviation: Overrides the upper price-band multiple (default
            MARKET_BAND_HIGH=1.35, i.e. 35%) as a fraction over market_rate,
            e.g. 0.35. The lower bound (MARKET_BAND_LOW) is not configurable —
            pricing below market is already governed by min_margin.

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
            'constraints_applied': {...},
            'constraints_relaxed': bool,
            'constraint_notes': [str, ...],
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

        if predict_proba_fn is None:
            from core.services.win_prediction import heuristic_win_proba
            predict_proba_fn = heuristic_win_proba

        if base_features is not None:
            features: Dict[str, Any] = dict(base_features)
        else:
            from core.services.quote_features import _cyclical
            from django.utils import timezone

            tier_int = _tier_to_int(client_tier)
            days = int(days_until_departure) if days_until_departure is not None else 7
            try:
                hist = float(historical_acceptance_rate)
            except (TypeError, ValueError):
                hist = 0.5
            hist = max(0.0, min(1.0, hist))
            now = timezone.now()
            month_sin, month_cos = _cyclical(now.month, 12)
            dow_sin, dow_cos = _cyclical(now.weekday(), 7)
            features = {
                'client_tier': tier_int,
                'days_until_departure': days,
                'historical_acceptance_rate': hist,
                'month_sin': month_sin, 'month_cos': month_cos,
                'dow_sin': dow_sin, 'dow_cos': dow_cos,
                'route_popularity': _route_popularity(origin, destination),
            }

        # Candidate prices are anchored on the market rate, floored at cost plus
        # the minimum markup (structural — see docstring). When cost sits
        # at/above the market band, fall back to sweeping the markup band over
        # cost so the band is never inverted.
        band_high_mult = (1.0 + max_market_deviation) if max_market_deviation is not None else MARKET_BAND_HIGH
        price_lo = max(total_cost * (1.0 + min_margin), market_rate * MARKET_BAND_LOW)
        price_hi = max(market_rate * band_high_mult, total_cost * (1.0 + max_margin))

        min_win_probability = max(0.0, min(1.0, float(min_win_probability or 0.0)))

        steps = 40
        full_curve: List[Dict[str, float]] = []
        unconstrained_best = None  # (expected_profit, point_dict, price, margin, p_win)
        constrained_best = None

        for i in range(steps + 1):
            frac = i / steps
            price = price_lo + frac * (price_hi - price_lo)
            margin = (price - total_cost) / total_cost
            price_ratio = price / market_rate

            features['price_ratio'] = price_ratio
            # quoted_margin_pct is margin-on-PRICE (not margin-on-cost like
            # `margin` above) — matches quote_features.compute_features's own
            # definition exactly, so a trained model sees the same quantity
            # at serving time it saw at training time.
            features['quoted_margin_pct'] = ((price - total_cost) / price * 100.0) if price > 0 else 0.0
            try:
                p_win = predict_proba_fn(features)
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

            candidate = (expected_profit, point, price, margin, p_win)
            if unconstrained_best is None or expected_profit > unconstrained_best[0]:
                unconstrained_best = candidate
            if p_win >= min_win_probability:
                if constrained_best is None or expected_profit > constrained_best[0]:
                    constrained_best = candidate

        if unconstrained_best is None:
            return _fallback(total_cost, min_margin, max_margin)

        constraints_relaxed = constrained_best is None
        best = constrained_best if constrained_best is not None else unconstrained_best
        _, best_point, best_price, best_margin, best_pwin = best

        constraint_notes = []
        if constraints_relaxed and min_win_probability > 0:
            constraint_notes.append(
                f'No price in the market-anchored band reached the configured minimum win '
                f'probability of {min_win_probability * 100:.0f}% — showing the best '
                f'profit-maximising price without that floor.'
            )

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
            'constraints_applied': {
                'min_margin_pct': round(min_margin * 100.0, 2),
                'max_margin_pct': round(max_margin * 100.0, 2),
                'min_win_probability_pct': round(min_win_probability * 100.0, 2),
                'max_market_deviation_pct': round((band_high_mult - 1.0) * 100.0, 2),
            },
            'constraints_relaxed': constraints_relaxed,
            'constraint_notes': constraint_notes,
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
