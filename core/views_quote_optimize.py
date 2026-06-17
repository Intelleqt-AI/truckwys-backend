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

            result = optimize_price(
                total_cost=total_cost,
                market_rate=market_rate,
                client_tier=client_tier,
                days_until_departure=days_until_departure,
                historical_acceptance_rate=historical_acceptance_rate,
            )

            response_data = {'success': True}
            response_data.update(result)
            return Response(response_data)

        except Exception as e:
            logger.exception('AIPriceOptimizeView failed: %s', e)
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
