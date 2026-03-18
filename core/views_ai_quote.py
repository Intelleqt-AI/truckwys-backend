"""
AI-powered quote suggestion and guard API endpoints (Phase 2 Sprint 2).

Endpoints:
- POST /api/v1/quotes/suggest/ - AI-powered quote price suggestion
- POST /api/v1/quotes/guard/ - Revenue Guard safety check
"""

import logging
from decimal import Decimal

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Customer
from core.services.quote_ml_lgbm import QuoteMarginModel
from core.services.quote_acceptance_model import QuoteAcceptanceModel
from core.services.quote_explainer import QuoteExplainer
from core.services.revenue_guard import RevenueGuardEngine
from core.services.true_margin_calculator import TrueMarginCalculatorService

logger = logging.getLogger(__name__)


class QuoteSuggestView(APIView):
    """
    AI-powered quote price suggestion.

    POST /api/v1/quotes/suggest/

    Request:
    {
        "distance_km": 570,
        "load_type": "general",
        "truck_type": "semi",
        "client_id": 123,
        "load_weight_tons": 25,
        "urgency": 3,
        "return_load_available": false,
        "origin": "Johannesburg",
        "destination": "Durban"
    }

    Response:
    {
        "suggested_price": 45000,
        "confidence": 0.82,
        "margin_pct": 18.5,
        "price_range_min": 42000,
        "price_range_max": 48000,
        "true_cost": 36600,
        "cost_breakdown": {...},
        "acceptance_probability": 0.71,
        "shap_top_factors": [...]
    }
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        """Generate AI-powered quote suggestion."""
        try:
            # Parse request
            distance_km = float(request.data.get('distance_km', 0))
            load_type = request.data.get('load_type', 'general')
            truck_type = request.data.get('truck_type', 'semi')
            client_id = request.data.get('client_id')
            load_weight_tons = float(request.data.get('load_weight_tons', 20))
            urgency = int(request.data.get('urgency', 3))
            return_load = bool(request.data.get('return_load_available', False))
            origin = request.data.get('origin', 'JHB')
            destination = request.data.get('destination', 'CPT')

            # Calculate true cost
            margin_calc = TrueMarginCalculatorService()
            cost_result = margin_calc.calculate_true_cost(
                distance_km=distance_km,
                truck_type=truck_type,
                load_type=load_type,
                origin=origin,
                destination=destination,
                has_return_load=return_load,
            )

            true_cost = cost_result['total_cost']

            # Build features for margin model
            margin_features = {
                'distance_km': distance_km,
                'load_type_enc': {'general': 0, 'refrigerated': 1, 'hazmat': 2, 'bulk': 3, 'abnormal': 4}.get(load_type, 0),
                'truck_type_enc': {'rigid_8t': 0, 'rigid_16t': 1, 'horse_trailer': 2, 'interlink': 3, 'semi': 3}.get(truck_type, 0),
                'fuel_price_inland': 22.0,
                'client_payment_score': 70.0,
                'client_tenure_months': 12,
                'time_of_year_month': 6,
                'deadhead_fraction': 0.0 if return_load else 0.3,
                'toll_cost_zar': cost_result['toll_cost'],
                'driver_cost_per_trip': cost_result['driver_cost'],
                'load_weight_tons': load_weight_tons,
                'num_stops': 1,
                'border_crossing': 0,
                'urgency_flag': 1 if urgency >= 4 else 0,
                'return_load_available': 1 if return_load else 0,
                'spot_vs_contract_enc': 1,
                'route_hijack_risk_score': 0.1,
                'fleet_fuel_cpk_actual': 6.5,
                'fleet_driver_score': 75.0,
                'vehicle_age_years': 5.0,
                'seasonal_demand_index': 0.6,
                'competitor_density_enc': 1,
            }

            # Predict margin (if model trained)
            if QuoteMarginModel.is_trained():
                margin_model = QuoteMarginModel()
                margin_pred = margin_model.predict(margin_features)
                predicted_margin_pct = margin_pred['predicted_margin_pct']
            else:
                predicted_margin_pct = 15.0  # Default fallback

            # Calculate suggested price
            suggested_price = true_cost / (1 - predicted_margin_pct / 100)

            # Build features for acceptance model
            acceptance_features = {
                'price_vs_historical_avg_ratio': 1.0,
                'client_acceptance_rate_90d': 0.68,
                'urgency_flag': 1 if urgency >= 4 else 0,
                'time_of_month': 15,
                'route_demand_index': 0.6,
                'client_payment_score': 70.0,
                'margin_pct': predicted_margin_pct,
                'load_type_enc': margin_features['load_type_enc'],
                'truck_type_enc': margin_features['truck_type_enc'],
                'distance_km': distance_km,
            }

            # Predict acceptance probability
            acceptance_prob = 0.68  # default
            if QuoteAcceptanceModel.is_trained():
                acceptance_model = QuoteAcceptanceModel()
                acceptance_prob = acceptance_model.predict_probability(acceptance_features)

            # SHAP explanations
            shap_factors = []
            if QuoteMarginModel.is_trained():
                explainer = QuoteExplainer()
                shap_factors = explainer.explain_margin_prediction(margin_features, margin_model)

            # Price range (±10%)
            price_range_min = suggested_price * 0.90
            price_range_max = suggested_price * 1.10

            return Response({
                'suggested_price': round(suggested_price, 2),
                'confidence': 0.80,
                'margin_pct': round(predicted_margin_pct, 2),
                'price_range_min': round(price_range_min, 2),
                'price_range_max': round(price_range_max, 2),
                'true_cost': round(true_cost, 2),
                'cost_breakdown': cost_result,
                'acceptance_probability': round(acceptance_prob, 2),
                'shap_top_factors': shap_factors[:5],
            })

        except Exception as e:
            logger.exception('Error in QuoteSuggestView')
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class QuoteGuardView(APIView):
    """
    Revenue Guard safety check.

    POST /api/v1/quotes/guard/

    Request:
    {
        "quote_price": 42000,
        "distance_km": 570,
        "load_type": "general",
        "truck_type": "semi",
        "client_id": 123,
        "origin": "Johannesburg",
        "destination": "Durban"
    }

    Response:
    {
        "safe": false,
        "risk_score": 45,
        "rating": "CRITICAL",
        "warnings": [
            {"code": "LOW_MARGIN", "message": "...", "severity": "CRITICAL"}
        ],
        "suggested_safe_price": 48000
    }
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        """Run Revenue Guard checks."""
        try:
            quote_price = float(request.data.get('quote_price', 0))
            distance_km = float(request.data.get('distance_km', 0))
            load_type = request.data.get('load_type', 'general')
            truck_type = request.data.get('truck_type', 'semi')
            client_id = request.data.get('client_id')
            origin = request.data.get('origin', 'JHB')
            destination = request.data.get('destination', 'CPT')

            # Fetch client if provided
            client = None
            if client_id:
                try:
                    client = Customer.objects.get(pk=client_id, company=request.user.company)
                except Customer.DoesNotExist:
                    pass

            # Run guard checks
            guard = RevenueGuardEngine()
            result = guard.check(
                quote_price=quote_price,
                distance_km=distance_km,
                load_type=load_type,
                truck_type=truck_type,
                client=client,
                origin=origin,
                destination=destination,
            )

            return Response(result)

        except Exception as e:
            logger.exception('Error in QuoteGuardView')
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
