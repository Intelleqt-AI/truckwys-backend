"""
AI Price/Margin Optimizer API endpoint.

POST /api/v1/quotes/optimize/ — searches the price axis and returns the price
that maximises expected profit = (price - cost) * P(win), along with a
downsampled win/profit curve for the UI.

Reuses the existing WinProbabilityModel (via core.services.margin_optimizer)
so win-probability behaviour matches the rest of the quoting stack.
"""

import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from core.services.margin_optimizer import optimize_price

logger = logging.getLogger(__name__)


# UNREACHABLE FROM LIVE UI — no reference in frontend/src as of the two-tier
# AI pricing redesign (grepped repo-wide). Left in place rather than deleted
# (an external/internal caller could still curl it directly); it already
# calls optimize_price() with no new kwargs, so it transparently gets the
# heuristic fallback and is unaffected by the two-tier resolver work.
class AIPriceOptimizeView(APIView):
    """POST /api/v1/quotes/optimize/ — expected-profit-maximising price + curve."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Body:
        {
          "total_cost": 20000,                  # required, > 0
          "market_rate": 28000,                 # benchmark lane price (ZAR)
          "client_tier": "standard",            # optional: new|standard|vip (or 0-2)
          "days_until_departure": 7,            # optional
          "historical_acceptance_rate": 0.5     # optional, [0, 1]
        }
        Returns the optimizer result dict plus {"success": true}.
        """
        try:
            data = request.data

            try:
                total_cost = float(data.get('total_cost', 0))
            except (TypeError, ValueError):
                total_cost = 0.0

            if total_cost <= 0:
                return Response({
                    'success': False,
                    'error': 'total_cost must be > 0',
                }, status=status.HTTP_400_BAD_REQUEST)

            try:
                market_rate = float(data.get('market_rate', 0))
            except (TypeError, ValueError):
                market_rate = 0.0

            client_tier = data.get('client_tier', 'standard')

            try:
                days_until_departure = int(data.get('days_until_departure', 7))
            except (TypeError, ValueError):
                days_until_departure = 7

            try:
                historical_acceptance_rate = float(
                    data.get('historical_acceptance_rate', 0.5)
                )
            except (TypeError, ValueError):
                historical_acceptance_rate = 0.5

            # The market is the pricing analysis' range (QUOTE-RULES §8 and
            # privacy): platform tier only from >= 10 accepted quotes by >= 3
            # operators other than the caller, rounded to R500; else the
            # caller's own accepted quotes (>= 5). A client-sent figure is the
            # caller's own number. Never an invented cost x 1,25 anchor.
            market_rate_source = 'client' if market_rate > 0 else 'none'
            origin = str(data.get('origin') or '').strip()
            destination = str(data.get('destination') or '').strip()
            vehicle_type = str(data.get('vehicle_type') or '').strip()
            if origin and destination:
                try:
                    from core.views import resolve_user_company
                    from core.services.lane_benchmark import resolve_market_range
                    rng = resolve_market_range(origin, destination, vehicle_type or None,
                                               company=resolve_user_company(request.user))
                    if rng.get('available') and not rng.get('is_estimate') and rng.get('median'):
                        market_rate = float(rng['median'])
                        market_rate_source = rng['tier']
                except Exception as exc:
                    logger.warning('optimize: market range resolve failed: %s', exc)

            if market_rate <= 0:
                return Response({
                    'success': True,
                    'market_rate': None,
                    'market_rate_source': 'none',
                    'optimal_price': None, 'optimal_margin_pct': None,
                    'win_probability_at_optimal': None, 'expected_profit': None, 'curve': [],
                    'used_heuristic_fallback': False, 'win_probability_source': None,
                    'reason': 'no_market',
                    'message': 'No market data for this lane yet, so no price is optimised.',
                })

            result = optimize_price(
                total_cost=total_cost,
                market_rate=market_rate,
                client_tier=client_tier,
                days_until_departure=days_until_departure,
                historical_acceptance_rate=historical_acceptance_rate,
                origin=origin or None,
                destination=destination or None,
            )
            if result.get('used_heuristic_fallback') or result.get('win_probability_source') != 'model':
                # A heuristic is not a chance to win: no %, and no expected
                # profit built on it. The price is labelled heuristic.
                result['win_probability_at_optimal'] = None
                result['expected_profit'] = None
                result['curve'] = [{**pt, 'win_probability': None, 'expected_profit': None}
                                   for pt in result.get('curve') or []]
                result['win_probability_source'] = 'heuristic'
                result['price_basis'] = 'heuristic'

            response_data = {
                'success': True,
                'market_rate': round(market_rate, 2),
                'market_rate_source': market_rate_source,
            }
            response_data.update(result)
            return Response(response_data)

        except Exception as e:
            logger.exception('AIPriceOptimizeView failed: %s', e)
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
