"""LightGBM-based quote margin prediction model for SA freight."""

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from django.conf import settings

# ML imports — installed separately
try:
    import joblib
    import lightgbm as lgb
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import train_test_split
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False


# Feature names in the exact order expected by the model
FEATURE_NAMES: List[str] = [
    'route_id',
    'distance_km',
    'truck_type',
    'load_type',
    'load_weight',
    'fuel_price',
    'toll_cost',
    'driver_cost',
    'client_tier',
    'historical_acceptance_rate',
    'day_of_week',
    'month',
    'is_holiday',
    'is_return_load',
    'competitor_quote',
    'urgency',
    'route_popularity',
    'weather_risk',
    'historical_margin_avg',
    'fleet_utilization',
    'deadhead_prob',
    'load_value_zar',
]

# Categorical feature indices (for LightGBM)
CATEGORICAL_FEATURES: List[int] = [
    FEATURE_NAMES.index('route_id'),
    FEATURE_NAMES.index('truck_type'),
    FEATURE_NAMES.index('load_type'),
    FEATURE_NAMES.index('client_tier'),
    FEATURE_NAMES.index('day_of_week'),
    FEATURE_NAMES.index('month'),
    FEATURE_NAMES.index('is_holiday'),
    FEATURE_NAMES.index('is_return_load'),
    FEATURE_NAMES.index('urgency'),
]

TARGET = 'actual_margin_pct'


@dataclass
class MarginPrediction:
    """Result from predict_optimal_margin()."""
    predicted_margin_pct: float       # e.g. 0.18 = 18%
    confidence: float                  # 0–1 scale
    recommended_price: float           # ZAR quote price
    margin_lower: float                # 5th percentile estimate
    margin_upper: float                # 95th percentile estimate
    feature_importances: List[Dict]    # top-N feature importances

    def to_dict(self) -> Dict[str, Any]:
        return {
            'predicted_margin_pct': round(self.predicted_margin_pct, 4),
            'predicted_margin_pct_display': f'{self.predicted_margin_pct * 100:.1f}%',
            'confidence': round(self.confidence, 4),
            'recommended_price': round(self.recommended_price, 2),
            'margin_lower': round(self.margin_lower, 4),
            'margin_upper': round(self.margin_upper, 4),
            'feature_importances': self.feature_importances,
        }


class QuoteMLModel:
    """
    LightGBM regression model that predicts the optimal margin percentage
    for a SA freight quote given 22 input features.

    Usage:
        model = QuoteMLModel()
        # after training:
        result = model.predict_optimal_margin(features_dict, actual_cost=50000)
    """

    MODEL_DIR = Path(settings.MEDIA_ROOT) / 'ml_models'
    MODEL_PATH = MODEL_DIR / 'quote_margin_model.pkl'
    METADATA_PATH = MODEL_DIR / 'quote_margin_metadata.json'

    def __init__(self):
        if not ML_AVAILABLE:
            raise ImportError(
                "ML libraries not available. Install with: "
                "pip install lightgbm scikit-learn joblib pandas"
            )

        self.model: Optional[lgb.LGBMRegressor] = None
        self.metadata: Dict[str, Any] = {}
        self.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self._load_model()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def train(
        self,
        csv_path: str,
        test_size: float = 0.20,
        random_state: int = 42,
    ) -> Dict[str, Any]:
        """
        Train LightGBM regressor from the CSV produced by generate_quote_training_data.

        Args:
            csv_path: Path to the training CSV file.
            test_size: Fraction of data held out for testing (default 0.20).
            random_state: Reproducibility seed.

        Returns:
            Dict with success flag, metrics, feature importances, and metadata.
        """
        X, y = self._load_csv(csv_path)

        if len(X) < 100:
            raise ValueError(f"Insufficient training data: {len(X)} rows (need ≥ 100)")

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=random_state
        )

        self.model = lgb.LGBMRegressor(
            n_estimators=500,
            max_depth=7,
            learning_rate=0.05,
            num_leaves=63,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_samples=20,
            reg_alpha=0.1,
            reg_lambda=0.1,
            random_state=random_state,
            n_jobs=-1,
            verbose=-1,
        )

        self.model.fit(
            X_train, y_train,
            categorical_feature=CATEGORICAL_FEATURES,
            eval_set=[(X_test, y_test)],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(period=-1)],
        )

        metrics = self._evaluate(X_test, y_test)
        feature_importances = self._feature_importances()

        self.metadata = {
            'training_date': datetime.now().isoformat(),
            'sample_count': len(X),
            'train_count': len(X_train),
            'test_count': len(X_test),
            'feature_names': FEATURE_NAMES,
            'feature_count': len(FEATURE_NAMES),
            'target': TARGET,
            'best_iteration': int(self.model.best_iteration_) if hasattr(self.model, 'best_iteration_') else None,
            'metrics': metrics,
            'feature_importances': feature_importances,
        }

        self._save_model()

        return {
            'success': True,
            'metrics': metrics,
            'feature_importances': feature_importances,
            'metadata': self.metadata,
        }

    def predict_optimal_margin(
        self,
        features: Dict[str, Any],
        actual_cost: float,
        top_n: int = 5,
    ) -> Optional[MarginPrediction]:
        """
        Predict the optimal margin percentage for a freight quote.

        Args:
            features: Dict mapping each of the 22 FEATURE_NAMES to a numeric value.
            actual_cost: The carrier's actual cost for the trip (ZAR).  Used to
                         compute the recommended_price from the predicted margin.
            top_n: Number of top feature importances to include in result.

        Returns:
            MarginPrediction or None if model not trained.
        """
        if self.model is None:
            return None

        X = self._features_to_array(features)
        predicted = float(self.model.predict(X)[0])
        # Clamp to sensible freight range
        predicted = max(0.05, min(0.45, predicted))

        # Confidence: based on how far from the risky extremes (0.05 / 0.45)
        mid = 0.25
        confidence = 1.0 - abs(predicted - mid) / mid
        confidence = float(max(0.0, min(1.0, confidence)))

        # Approximate prediction interval using ±1 std of training residuals
        std = float(self.metadata.get('metrics', {}).get('rmse', 0.03))
        margin_lower = max(0.05, predicted - 1.645 * std)   # ~90% PI lower
        margin_upper = min(0.45, predicted + 1.645 * std)

        recommended_price = actual_cost / (1.0 - predicted) if predicted < 1.0 else actual_cost * 1.2

        top_importances = self._feature_importances(top_n=top_n)

        return MarginPrediction(
            predicted_margin_pct=predicted,
            confidence=confidence,
            recommended_price=recommended_price,
            margin_lower=margin_lower,
            margin_upper=margin_upper,
            feature_importances=top_importances,
        )

    def is_trained(self) -> bool:
        return self.model is not None

    def get_model_info(self) -> Optional[Dict[str, Any]]:
        if self.model is None:
            return None
        return {
            'trained': True,
            'training_date': self.metadata.get('training_date'),
            'sample_count': self.metadata.get('sample_count'),
            'feature_count': self.metadata.get('feature_count'),
            'metrics': self.metadata.get('metrics'),
            'model_type': 'LightGBM Regressor',
            'target': TARGET,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_csv(self, csv_path: str) -> Tuple[np.ndarray, np.ndarray]:
        """Read training CSV, return (X, y) numpy arrays."""
        X_rows = []
        y_rows = []

        with open(csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    x = [float(row[name]) for name in FEATURE_NAMES]
                    y_val = float(row[TARGET])
                    X_rows.append(x)
                    y_rows.append(y_val)
                except (KeyError, ValueError):
                    continue  # skip malformed rows

        return np.array(X_rows, dtype=np.float32), np.array(y_rows, dtype=np.float32)

    def _features_to_array(self, features: Dict[str, Any]) -> np.ndarray:
        """Convert feature dict to (1, n_features) numpy array."""
        row = [float(features[name]) for name in FEATURE_NAMES]
        return np.array([row], dtype=np.float32)

    def _evaluate(self, X_test: np.ndarray, y_test: np.ndarray) -> Dict[str, float]:
        y_pred = self.model.predict(X_test)
        rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
        mae = float(mean_absolute_error(y_test, y_pred))
        r2 = float(r2_score(y_test, y_pred))
        # Mean absolute percentage error (avoid div-by-zero)
        mape = float(np.mean(np.abs((y_test - y_pred) / np.clip(np.abs(y_test), 1e-6, None))) * 100)
        return {
            'rmse': round(rmse, 6),
            'mae': round(mae, 6),
            'r2': round(r2, 6),
            'mape': round(mape, 4),
        }

    def _feature_importances(self, top_n: Optional[int] = None) -> List[Dict]:
        if self.model is None:
            return []
        importances = self.model.feature_importances_
        total = importances.sum() or 1
        pairs = sorted(
            zip(FEATURE_NAMES, importances),
            key=lambda x: x[1],
            reverse=True,
        )
        if top_n:
            pairs = pairs[:top_n]
        return [
            {'feature': name, 'importance': int(imp), 'importance_pct': round(imp / total * 100, 2)}
            for name, imp in pairs
        ]

    def _load_model(self) -> bool:
        try:
            if self.MODEL_PATH.exists():
                self.model = joblib.load(self.MODEL_PATH)
                if self.METADATA_PATH.exists():
                    with open(self.METADATA_PATH, 'r', encoding='utf-8') as f:
                        self.metadata = json.load(f)
                return True
        except Exception as e:
            print(f"QuoteMLModel: could not load model from disk: {e}")
            self.model = None
        return False

    def _save_model(self) -> None:
        joblib.dump(self.model, self.MODEL_PATH)
        with open(self.METADATA_PATH, 'w', encoding='utf-8') as f:
            json.dump(self.metadata, f, indent=2)


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def predict_optimal_margin(
    features: Dict[str, Any],
    actual_cost: float,
) -> Dict[str, Any]:
    """
    Convenience wrapper — load the trained model and return a prediction dict.

    Returns a dict with keys:
        predicted_margin_pct, confidence, recommended_price,
        margin_lower, margin_upper, feature_importances
    Or raises RuntimeError if model not trained.
    """
    pipeline = QuoteMLModel()
    if not pipeline.is_trained():
        raise RuntimeError(
            "Quote ML model not trained. Run: python manage.py train_quote_model"
        )
    result = pipeline.predict_optimal_margin(features, actual_cost)
    if result is None:
        raise RuntimeError("Prediction failed unexpectedly.")
    return result.to_dict()


# ============================================================================
# Sprint 1: Win Probability Model
# ============================================================================

class WinProbabilityModel:
    """
    Logistic regression classifier that predicts P(quote accepted | features).

    Features:
    - price_ratio: proposed_price / market_rate
    - client_tier: 0=new, 1=regular, 2=vip
    - days_until_departure: urgency (lower = more urgent = higher win prob)
    - historical_acceptance_rate_for_client: past accepts / total quotes
    - month: seasonality (1-12)
    - day_of_week: 0=Mon, 6=Sun
    - route_popularity: quotes on this lane in past 90 days

    Trained on QuoteOutcome records (accepted=1, rejected=0).
    """

    MODEL_DIR = Path(settings.MEDIA_ROOT) / 'ml_models'
    MODEL_PATH = MODEL_DIR / 'win_probability_model.pkl'
    METADATA_PATH = MODEL_DIR / 'win_probability_metadata.json'

    def __init__(self):
        if not ML_AVAILABLE:
            raise ImportError("ML libraries not available")

        self.model = None
        self.metadata = {}
        self.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self._load_model()

    def _load_model(self):
        """Load saved model and metadata if available."""
        if not self.MODEL_PATH.exists():
            return

        try:
            self.model = joblib.load(self.MODEL_PATH)
            if self.METADATA_PATH.exists():
                with open(self.METADATA_PATH, 'r') as f:
                    self.metadata = json.load(f)
        except Exception as exc:
            logger.warning('Failed to load WinProbabilityModel: %s', exc)

    def _save_model(self):
        """Save model and metadata to disk."""
        try:
            joblib.dump(self.model, self.MODEL_PATH)
            with open(self.METADATA_PATH, 'w') as f:
                json.dump(self.metadata, f, indent=2)
            logger.info('Saved WinProbabilityModel to %s', self.MODEL_PATH)
        except Exception as exc:
            logger.error('Failed to save WinProbabilityModel: %s', exc)

    def train(self, outcomes_df) -> Dict[str, Any]:
        """
        Train logistic regression on QuoteOutcome data.

        Args:
            outcomes_df: DataFrame with columns:
                - outcome: 'accepted' or 'rejected'
                - price_ratio, client_tier, days_until_departure, etc.

        Returns:
            Dict with training metrics
        """
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, roc_auc_score

        # Prepare features
        feature_cols = [
            'price_ratio', 'client_tier', 'days_until_departure',
            'historical_acceptance_rate', 'month', 'day_of_week',
            'route_popularity'
        ]

        X = outcomes_df[feature_cols].values
        y = (outcomes_df['outcome'] == 'accepted').astype(int).values

        if len(X) < 50:
            logger.warning('Insufficient data for win probability training (<50 outcomes)')
            # Use synthetic bootstrapping or skip training
            return {'success': False, 'error': 'Insufficient data (<50 outcomes)'}

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )

        self.model = LogisticRegression(
            max_iter=1000,
            random_state=42,
            penalty='l2',
            C=1.0
        )
        self.model.fit(X_train, y_train)

        # Evaluate
        y_pred = self.model.predict(X_test)
        y_pred_proba = self.model.predict_proba(X_test)[:, 1]

        accuracy = accuracy_score(y_test, y_pred)
        try:
            auc = roc_auc_score(y_test, y_pred_proba)
        except:
            auc = 0.5

        self.metadata = {
            'trained_at': datetime.now().isoformat(),
            'training_count': len(X_train),
            'test_count': len(X_test),
            'accuracy': float(accuracy),
            'auc': float(auc),
            'feature_names': feature_cols,
            'version': '1.0.0',
        }

        self._save_model()

        return {
            'success': True,
            'accuracy': accuracy,
            'auc': auc,
            'training_count': len(X_train),
        }

    def predict_proba(
        self,
        price_ratio: float,
        client_tier: int = 0,
        days_until_departure: int = 2,
        historical_acceptance_rate: float = 0.7,
        month: int = 3,
        day_of_week: int = 1,
        route_popularity: float = 0.5,
    ) -> float:
        """
        Predict P(accepted) for a quote.

        Args:
            price_ratio: proposed_price / market_rate (e.g., 0.95 = 5% below market)
            client_tier: 0=new, 1=regular, 2=vip
            days_until_departure: urgency in days
            historical_acceptance_rate: client's past acceptance rate
            month: 1-12
            day_of_week: 0-6 (Mon-Sun)
            route_popularity: normalized route popularity

        Returns:
            Probability [0.0, 1.0]
        """
        if self.model is None:
            # Return heuristic if model not trained
            # Simple heuristic: lower price = higher win prob
            base_prob = 0.5
            if price_ratio < 0.9:
                base_prob = 0.75
            elif price_ratio < 0.95:
                base_prob = 0.65
            elif price_ratio > 1.1:
                base_prob = 0.35
            elif price_ratio > 1.05:
                base_prob = 0.45

            # Adjust for client tier
            if client_tier == 2:  # VIP
                base_prob += 0.05
            elif client_tier == 0:  # New
                base_prob -= 0.05

            return max(0.0, min(1.0, base_prob))

        # Use trained model
        X = np.array([[
            price_ratio,
            client_tier,
            days_until_departure,
            historical_acceptance_rate,
            month,
            day_of_week,
            route_popularity,
        ]])

        prob = float(self.model.predict_proba(X)[0, 1])
        return max(0.0, min(1.0, prob))

    def is_trained(self) -> bool:
        return self.model is not None
