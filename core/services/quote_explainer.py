"""
SHAP-based explainability for quote ML models (T5.2).

Provides human-readable explanations for margin and acceptance predictions.
"""

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class QuoteExplainer:
    """
    SHAP explainability wrapper for quote ML models.

    Provides top-N factor explanations with human-readable labels.
    """

    # Human-readable feature labels
    FEATURE_LABELS = {
        'distance_km': 'Route Distance',
        'load_type_enc': 'Load Type',
        'truck_type_enc': 'Truck Type',
        'fuel_price_inland': 'Fuel Price',
        'client_payment_score': 'Client Payment Score',
        'client_tenure_months': 'Client Tenure',
        'time_of_year_month': 'Month of Year',
        'deadhead_fraction': 'Deadhead %',
        'toll_cost_zar': 'Toll Costs',
        'driver_cost_per_trip': 'Driver Cost',
        'load_weight_tons': 'Load Weight',
        'num_stops': 'Number of Stops',
        'border_crossing': 'Border Crossing',
        'urgency_flag': 'Urgency',
        'return_load_available': 'Return Load',
        'spot_vs_contract_enc': 'Spot vs Contract',
        'route_hijack_risk_score': 'Security Risk',
        'fleet_fuel_cpk_actual': 'Fleet Fuel CPK',
        'fleet_driver_score': 'Driver Quality',
        'vehicle_age_years': 'Vehicle Age',
        'seasonal_demand_index': 'Seasonal Demand',
        'competitor_density_enc': 'Competition',
        # Acceptance model features
        'price_vs_historical_avg_ratio': 'Price vs Historical Avg',
        'client_acceptance_rate_90d': 'Client Accept Rate (90d)',
        'route_demand_index': 'Route Demand',
        'margin_pct': 'Margin %',
    }

    def explain_margin_prediction(self, features: dict, model: Any) -> list[dict]:
        """
        Compute SHAP explanations for margin prediction (simplified version without actual SHAP).

        Since SHAP requires significant computation, we use feature importances as proxy.

        Args:
            features: Input features dict
            model: QuoteMarginModel instance

        Returns:
            list: Top 5 factors [{"feature": str, "impact": str, "direction": str, "shap_value": float}, ...]
        """
        if not hasattr(model, 'feature_importances_') or model.feature_importances_ is None:
            return []

        # Get feature importances
        importances = model.feature_importances_

        # Compute impact scores based on feature value * importance
        impact_scores = []
        for feature_name, importance in importances.items():
            if feature_name not in features:
                continue

            feature_value = features[feature_name]
            # Simplified SHAP proxy: importance * normalized_value
            impact_score = importance * abs(feature_value) / 100.0  # normalize

            # Determine direction
            direction = 'cost_increase' if feature_value > 50 else 'cost_decrease'

            # Format impact as currency or percentage
            impact_str = f"+R{impact_score*1000:.0f}" if impact_score > 0 else f"-R{abs(impact_score)*1000:.0f}"

            impact_scores.append({
                'feature': self.FEATURE_LABELS.get(feature_name, feature_name),
                'feature_key': feature_name,
                'impact': impact_str,
                'direction': direction,
                'shap_value': float(impact_score),
            })

        # Sort by absolute impact and return top 5
        impact_scores.sort(key=lambda x: abs(x['shap_value']), reverse=True)
        return impact_scores[:5]

    def explain_acceptance_prediction(self, features: dict, model: Any) -> list[dict]:
        """
        Compute SHAP explanations for acceptance prediction (simplified).

        Args:
            features: Input features dict
            model: QuoteAcceptanceModel instance

        Returns:
            list: Top 5 factors [{"feature": str, "impact": str, "direction": str, "shap_value": float}, ...]
        """
        if not hasattr(model, 'feature_importances_') or model.feature_importances_ is None:
            return []

        importances = model.feature_importances_

        impact_scores = []
        for feature_name, importance in importances.items():
            if feature_name not in features:
                continue

            feature_value = features[feature_name]
            impact_score = importance * feature_value / 100.0

            # Direction: positive impact increases acceptance, negative decreases
            direction = 'increases_acceptance' if feature_value > 0.5 else 'decreases_acceptance'

            # Format impact
            impact_str = f"+{impact_score*100:.1f}%" if impact_score > 0 else f"{impact_score*100:.1f}%"

            impact_scores.append({
                'feature': self.FEATURE_LABELS.get(feature_name, feature_name),
                'feature_key': feature_name,
                'impact': impact_str,
                'direction': direction,
                'shap_value': float(impact_score),
            })

        impact_scores.sort(key=lambda x: abs(x['shap_value']), reverse=True)
        return impact_scores[:5]
