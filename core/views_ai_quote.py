"""
AI Quote & Revenue Guard API endpoints for Phase 2 desktop quoting.
"""

from datetime import date
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from core.services.fuel_price import fetch_fuel_prices
from core.services.quote_ml import QuoteMLModel


class FuelPriceCurrentView(APIView):
    """GET /api/v1/fuel-prices/current/ — returns current diesel inland price."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            fuel_price = fetch_fuel_prices()
            return Response({
                'success': True,
                'date': fuel_price.date.isoformat(),
                'diesel_inland': float(fuel_price.diesel_inland),
                'diesel_coastal': float(fuel_price.diesel_coastal),
                'petrol_95': float(fuel_price.petrol_95),
                'petrol_93': float(fuel_price.petrol_93),
                'source': fuel_price.source,
            })
        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AIQuoteSuggestionView(APIView):
    """POST /api/v1/quotes/suggest/ — AI-suggested margin and price."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Expects:
        {
          "distance_km": 1400,
          "truck_type": 0,  # e.g., 0 = Flatbed
          "load_type": 0,
          "load_weight": 15000,
          "fuel_cost": 5000,
          "toll_cost": 1200,
          "driver_cost": 800,
          "actual_cost": 10000
        }
        Returns AI suggestion or 503 if model not trained.
        """
        try:
            model = QuoteMLModel()
            if not model.is_trained():
                return Response({
                    'success': False,
                    'error': 'AI model training — try again later',
                }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            data = request.data
            actual_cost = float(data.get('actual_cost', 0))
            if actual_cost <= 0:
                return Response({
                    'success': False,
                    'error': 'actual_cost must be > 0',
                }, status=status.HTTP_400_BAD_REQUEST)

            # Build feature dict — fill in defaults for missing features
            features = {
                'route_id': int(data.get('route_id', 0)),
                'distance_km': float(data.get('distance_km', 0)),
                'truck_type': int(data.get('truck_type', 0)),
                'load_type': int(data.get('load_type', 0)),
                'load_weight': float(data.get('load_weight', 0)),
                'fuel_price': float(data.get('fuel_price', 22.0)),
                'toll_cost': float(data.get('toll_cost', 0)),
                'driver_cost': float(data.get('driver_cost', 0)),
                'client_tier': int(data.get('client_tier', 1)),
                'historical_acceptance_rate': float(data.get('historical_acceptance_rate', 0.7)),
                'day_of_week': int(date.today().weekday()),
                'month': int(date.today().month),
                'is_holiday': int(data.get('is_holiday', 0)),
                'is_return_load': int(data.get('is_return_load', 0)),
                'competitor_quote': float(data.get('competitor_quote', 0)),
                'urgency': int(data.get('urgency', 1)),
                'route_popularity': float(data.get('route_popularity', 0.5)),
                'weather_risk': float(data.get('weather_risk', 0)),
                'historical_margin_avg': float(data.get('historical_margin_avg', 0.18)),
                'fleet_utilization': float(data.get('fleet_utilization', 0.75)),
                'deadhead_prob': float(data.get('deadhead_prob', 0.3)),
                'load_value_zar': float(data.get('load_value_zar', 0)),
            }

            prediction = model.predict_optimal_margin(features, actual_cost, top_n=5)
            if not prediction:
                return Response({
                    'success': False,
                    'error': 'Prediction failed',
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            return Response({
                'success': True,
                'suggested_price': prediction.recommended_price,
                'margin_pct': prediction.predicted_margin_pct * 100,
                'confidence': prediction.confidence,
                'margin_range': {
                    'lower': prediction.margin_lower * 100,
                    'upper': prediction.margin_upper * 100,
                },
                'top_features': prediction.feature_importances,
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RevenueGuardView(APIView):
    """POST /api/v1/quotes/guard/ — Revenue Guard risk assessment."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Expects:
        {
          "total_cost": 10000,
          "quote_price": 12000,
          "distance_km": 1400,
          "fuel_cost": 5000,
          "toll_cost": 1200
        }
        Returns risk badge: SAFE, CAUTION, AT_RISK
        """
        try:
            data = request.data
            total_cost = float(data.get('total_cost', 0))
            quote_price = float(data.get('quote_price', 0))

            if total_cost <= 0 or quote_price <= 0:
                return Response({
                    'success': False,
                    'error': 'total_cost and quote_price must be > 0',
                }, status=status.HTTP_400_BAD_REQUEST)

            margin = (quote_price - total_cost) / quote_price
            margin_pct = margin * 100

            # Risk thresholds
            if margin_pct < 5:
                risk_level = 'AT_RISK'
                color = 'danger'
                warnings = ['Margin below 5% — high risk of loss', 'Consider increasing quote price']
            elif margin_pct < 12:
                risk_level = 'CAUTION'
                color = 'warning'
                warnings = ['Margin below 12% — limited buffer for unexpected costs']
            else:
                risk_level = 'SAFE'
                color = 'success'
                warnings = []

            return Response({
                'success': True,
                'risk_level': risk_level,
                'color': color,
                'margin_pct': round(margin_pct, 2),
                'warnings': warnings,
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
