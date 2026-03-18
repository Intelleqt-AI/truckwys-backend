"""
LightGBM-based quote margin prediction model for TruckWys Phase 2 Sprint 2 (T2.2).

Predicts actual_margin_pct based on 22 operational features.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

logger = logging.getLogger(__name__)

# Model storage path
MODEL_DIR = Path('media/ml_models')
MODEL_PATH = MODEL_DIR / 'quote_margin_lgbm.pkl'


class QuoteMarginModel:
    """
    Production LightGBM model for quote margin prediction.

    Features (22):
    - distance_km, load_type_enc, truck_type_enc, fuel_price_inland
    - client_payment_score, client_tenure_months, time_of_year_month
    - deadhead_fraction, toll_cost_zar, driver_cost_per_trip
    - load_weight_tons, num_stops, border_crossing, urgency_flag
    - return_load_available, spot_vs_contract_enc, route_hijack_risk_score
    - fleet_fuel_cpk_actual, fleet_driver_score, vehicle_age_years
    - seasonal_demand_index, competitor_density_enc

    Target: actual_margin_pct (0.05-0.45)
    """

    FEATURE_NAMES = [
        "distance_km", "load_type_enc", "truck_type_enc", "fuel_price_inland",
        "client_payment_score", "client_tenure_months", "time_of_year_month",
        "deadhead_fraction", "toll_cost_zar", "driver_cost_per_trip",
        "load_weight_tons", "num_stops", "border_crossing", "urgency_flag",
        "return_load_available", "spot_vs_contract_enc", "route_hijack_risk_score",
        "fleet_fuel_cpk_actual", "fleet_driver_score", "vehicle_age_years",
        "seasonal_demand_index", "competitor_density_enc"
    ]

    def __init__(self):
        """Initialize model (load from disk if exists)."""
        self.model: Optional[lgb.LGBMRegressor] = None
        self.feature_importances_: Optional[dict] = None
        self.training_metrics_: Optional[dict] = None
        self.model_version = "1.0.0"

        if MODEL_PATH.exists():
            self.load()

    def train(self, training_data_path: str, test_size: float = 0.2, random_state: int = 42) -> dict:
        """
        Train LightGBM regressor on CSV from generate_quote_training_data.

        Args:
            training_data_path: Path to CSV with 22 features + actual_margin_pct target
            test_size: Fraction of data for test set
            random_state: Random seed for reproducibility

        Returns:
            dict: Training metrics {mae, rmse, r2, feature_importances, training_samples}
        """
        logger.info(f'Training QuoteMarginModel on {training_data_path}')

        # Load CSV
        df = pd.read_csv(training_data_path)
        logger.info(f'Loaded {len(df)} records from CSV')

        # Map old feature names from generate_quote_training_data to new names
        # The CSV has: route_id, distance_km, truck_type, load_type, load_weight, fuel_price, etc.
        # We need to create our 22 features from these

        X = pd.DataFrame()

        # Direct mappings
        if 'distance_km' in df.columns:
            X['distance_km'] = df['distance_km']
        if 'load_type' in df.columns:
            X['load_type_enc'] = df['load_type']
        if 'truck_type' in df.columns:
            X['truck_type_enc'] = df['truck_type']
        if 'fuel_price' in df.columns:
            X['fuel_price_inland'] = df['fuel_price']
        if 'toll_cost' in df.columns:
            X['toll_cost_zar'] = df['toll_cost']
        if 'driver_cost' in df.columns:
            X['driver_cost_per_trip'] = df['driver_cost']
        if 'load_weight' in df.columns:
            X['load_weight_tons'] = df['load_weight'] / 1000.0  # kg to tons
        if 'urgency' in df.columns:
            X['urgency_flag'] = (df['urgency'] >= 4).astype(int)
        if 'is_return_load' in df.columns:
            X['return_load_available'] = df['is_return_load']
        if 'month' in df.columns:
            X['time_of_year_month'] = df['month']
        if 'deadhead_prob' in df.columns:
            X['deadhead_fraction'] = df['deadhead_prob']

        # Derived/synthetic features (use defaults or random for missing)
        X['client_payment_score'] = df.get('historical_acceptance_rate', 0.68) * 100
        X['client_tenure_months'] = np.random.randint(1, 60, len(df))
        X['num_stops'] = np.random.randint(1, 5, len(df))
        X['border_crossing'] = 0
        X['spot_vs_contract_enc'] = np.random.randint(0, 2, len(df))
        X['route_hijack_risk_score'] = df.get('weather_risk', 0.1)
        X['fleet_fuel_cpk_actual'] = 6.5
        X['fleet_driver_score'] = 75.0
        X['vehicle_age_years'] = np.random.uniform(2, 10, len(df))
        X['seasonal_demand_index'] = df.get('route_popularity', 0.5)
        X['competitor_density_enc'] = df.get('client_tier', 1)

        y = df['actual_margin_pct']

        # Split
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=random_state
        )

        logger.info(f'Training set: {len(X_train)}, Test set: {len(X_test)}')

        # Train LightGBM
        self.model = lgb.LGBMRegressor(
            n_estimators=100,
            learning_rate=0.05,
            max_depth=6,
            num_leaves=31,
            min_child_samples=20,
            random_state=random_state,
            verbose=-1
        )

        self.model.fit(X_train, y_train)

        # Evaluate
        y_pred = self.model.predict(X_test)
        mae = mean_absolute_error(y_test, y_pred)
        rmse = np.sqrt(mean_squared_error(y_test, y_pred))
        r2 = r2_score(y_test, y_pred)

        # Feature importances
        importances = dict(zip(self.FEATURE_NAMES, self.model.feature_importances_))
        sorted_importances = sorted(importances.items(), key=lambda x: x[1], reverse=True)

        self.feature_importances_ = dict(sorted_importances)
        self.training_metrics_ = {
            'mae': float(mae),
            'rmse': float(rmse),
            'r2': float(r2),
            'training_samples': len(X_train),
            'test_samples': len(X_test),
        }

        # Save model
        self.save()

        logger.info(f'Training complete: MAE={mae:.4f}, RMSE={rmse:.4f}, R²={r2:.4f}')

        return {
            'mae': float(mae),
            'rmse': float(rmse),
            'r2': float(r2),
            'feature_importances': [
                {'feature': k, 'importance': float(v)}
                for k, v in sorted_importances
            ],
            'training_samples': len(X_train),
            'test_samples': len(X_test),
        }

    def predict(self, features: dict) -> dict:
        """
        Predict margin for a single quote.

        Args:
            features: Dict with keys matching FEATURE_NAMES

        Returns:
            dict: {predicted_margin_pct, confidence, model_version}
        """
        if self.model is None:
            raise RuntimeError('Model not trained. Call train() or load() first.')

        # Convert to DataFrame with correct feature order
        X = pd.DataFrame([features], columns=self.FEATURE_NAMES)

        predicted_margin = self.model.predict(X)[0]

        # Confidence based on historical variance (simplified)
        confidence = 0.75  # Placeholder: could use model.predict with return_std

        return {
            'predicted_margin_pct': float(predicted_margin),
            'confidence': float(confidence),
            'model_version': self.model_version,
        }

    def get_feature_importances(self) -> list[dict]:
        """
        Get sorted feature importances.

        Returns:
            list: [{feature, importance}, ...] sorted by importance desc
        """
        if self.feature_importances_ is None:
            return []

        return [
            {'feature': k, 'importance': float(v)}
            for k, v in self.feature_importances_.items()
        ]

    def save(self) -> None:
        """Save model to disk."""
        MODEL_DIR.mkdir(parents=True, exist_ok=True)

        model_data = {
            'model': self.model,
            'feature_importances': self.feature_importances_,
            'training_metrics': self.training_metrics_,
            'model_version': self.model_version,
        }

        joblib.dump(model_data, MODEL_PATH)
        logger.info(f'Model saved to {MODEL_PATH}')

    def load(self) -> None:
        """Load model from disk."""
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f'Model file not found: {MODEL_PATH}')

        model_data = joblib.load(MODEL_PATH)
        self.model = model_data['model']
        self.feature_importances_ = model_data.get('feature_importances')
        self.training_metrics_ = model_data.get('training_metrics')
        self.model_version = model_data.get('model_version', '1.0.0')

        logger.info(f'Model loaded from {MODEL_PATH}')

    @classmethod
    def is_trained(cls) -> bool:
        """Check if model file exists."""
        return MODEL_PATH.exists()
