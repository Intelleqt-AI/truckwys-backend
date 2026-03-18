"""
XGBoost-based quote acceptance probability model for TruckWys Phase 2 Sprint 2 (T2.3).

Predicts P(quote accepted) based on price, client history, and route factors.
"""

import logging
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score

logger = logging.getLogger(__name__)

# Model storage path
MODEL_DIR = Path('media/ml_models')
MODEL_PATH = MODEL_DIR / 'quote_acceptance_xgb.pkl'


class QuoteAcceptanceModel:
    """
    Production XGBoost classifier for quote acceptance probability.

    Features (10):
    - price_vs_historical_avg_ratio: quote_price / client_avg_price
    - client_acceptance_rate_90d: client's recent acceptance rate
    - urgency_flag: 1 if urgent (deadline < 48h)
    - time_of_month: day of month (1-31, payment cycle effect)
    - route_demand_index: route popularity (0-1)
    - client_payment_score: payment reliability (0-100)
    - margin_pct: quoted margin %
    - load_type_enc: categorical load type (0-4)
    - truck_type_enc: categorical truck type (0-4)
    - distance_km: route distance

    Target: accepted (0/1)
    """

    FEATURE_NAMES = [
        "price_vs_historical_avg_ratio",
        "client_acceptance_rate_90d",
        "urgency_flag",
        "time_of_month",
        "route_demand_index",
        "client_payment_score",
        "margin_pct",
        "load_type_enc",
        "truck_type_enc",
        "distance_km"
    ]

    def __init__(self):
        """Initialize model (load from disk if exists)."""
        self.model: Optional[xgb.XGBClassifier] = None
        self.feature_importances_: Optional[dict] = None
        self.training_metrics_: Optional[dict] = None
        self.model_version = "1.0.0"

        if MODEL_PATH.exists():
            self.load()

    def train(self, training_data_path: str, test_size: float = 0.2, random_state: int = 42) -> dict:
        """
        Train XGBoost classifier on CSV with acceptance labels.

        Args:
            training_data_path: Path to CSV with 10 features + accepted target
            test_size: Fraction for test set
            random_state: Random seed

        Returns:
            dict: Training metrics {accuracy, precision, recall, roc_auc, training_samples}
        """
        logger.info(f'Training QuoteAcceptanceModel on {training_data_path}')

        # Load CSV
        df = pd.read_csv(training_data_path)
        logger.info(f'Loaded {len(df)} records from CSV')

        # Build features from available columns
        X = pd.DataFrame()

        # Derive price_vs_historical_avg_ratio (simplified: use competitor_quote as proxy)
        if 'competitor_quote' in df.columns and 'load_value_zar' in df.columns:
            avg_price = df['competitor_quote'].mean()
            X['price_vs_historical_avg_ratio'] = df['competitor_quote'] / avg_price
        else:
            X['price_vs_historical_avg_ratio'] = 1.0

        X['client_acceptance_rate_90d'] = df.get('historical_acceptance_rate', 0.68)
        X['urgency_flag'] = (df.get('urgency', 3) >= 4).astype(int)
        X['time_of_month'] = df.get('month', 15) % 31 + 1  # map month to day-of-month proxy
        X['route_demand_index'] = df.get('route_popularity', 0.5)

        # Derived client payment score
        if 'historical_acceptance_rate' in df.columns:
            X['client_payment_score'] = df['historical_acceptance_rate'] * 100
        else:
            X['client_payment_score'] = 68.0

        # Margin from actual_margin_pct
        X['margin_pct'] = df.get('actual_margin_pct', 0.15) * 100

        # Categorical encodings
        X['load_type_enc'] = df.get('load_type', 0)
        X['truck_type_enc'] = df.get('truck_type', 0)
        X['distance_km'] = df.get('distance_km', 500)

        # Target: convert 'accepted' string to int
        if 'accepted' in df.columns:
            y = pd.to_numeric(df['accepted'], errors='coerce').fillna(0).astype(int)
        else:
            # Fallback: simulate acceptance based on margin
            y = (df['actual_margin_pct'] > 0.12).astype(int)

        # Split
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=random_state, stratify=y
        )

        logger.info(f'Training set: {len(X_train)}, Test set: {len(X_test)}')
        logger.info(f'Class distribution: {y.value_counts().to_dict()}')

        # Train XGBoost
        self.model = xgb.XGBClassifier(
            n_estimators=100,
            learning_rate=0.05,
            max_depth=5,
            min_child_weight=1,
            random_state=random_state,
            eval_metric='logloss',
            use_label_encoder=False
        )

        self.model.fit(X_train, y_train)

        # Evaluate
        y_pred = self.model.predict(X_test)
        y_prob = self.model.predict_proba(X_test)[:, 1]

        accuracy = accuracy_score(y_test, y_pred)
        precision = precision_score(y_test, y_pred, zero_division=0)
        recall = recall_score(y_test, y_pred, zero_division=0)
        roc_auc = roc_auc_score(y_test, y_prob)

        # Feature importances
        importances = dict(zip(self.FEATURE_NAMES, self.model.feature_importances_))
        sorted_importances = sorted(importances.items(), key=lambda x: x[1], reverse=True)

        self.feature_importances_ = dict(sorted_importances)
        self.training_metrics_ = {
            'accuracy': float(accuracy),
            'precision': float(precision),
            'recall': float(recall),
            'roc_auc': float(roc_auc),
            'training_samples': len(X_train),
            'test_samples': len(X_test),
        }

        # Save
        self.save()

        logger.info(f'Training complete: Acc={accuracy:.3f}, Prec={precision:.3f}, Recall={recall:.3f}, AUC={roc_auc:.3f}')

        return {
            'accuracy': float(accuracy),
            'precision': float(precision),
            'recall': float(recall),
            'roc_auc': float(roc_auc),
            'feature_importances': [
                {'feature': k, 'importance': float(v)}
                for k, v in sorted_importances
            ],
            'training_samples': len(X_train),
            'test_samples': len(X_test),
        }

    def predict_probability(self, features: dict) -> float:
        """
        Predict P(quote accepted).

        Args:
            features: Dict with keys matching FEATURE_NAMES

        Returns:
            float: Probability [0.0, 1.0]
        """
        if self.model is None:
            raise RuntimeError('Model not trained. Call train() or load() first.')

        X = pd.DataFrame([features], columns=self.FEATURE_NAMES)
        prob = self.model.predict_proba(X)[0, 1]
        return float(prob)

    def suggest_optimal_price(
        self,
        base_features: dict,
        cost: float,
        margin_floor: float = 0.12
    ) -> dict:
        """
        Grid search over price range to find optimal price balancing margin and acceptance probability.

        Args:
            base_features: Base feature dict (without price_vs_historical_avg_ratio and margin_pct)
            cost: True cost of the quote
            margin_floor: Minimum acceptable margin (default 12%)

        Returns:
            dict: {optimal_price, expected_margin_pct, acceptance_probability, reasoning}
        """
        if self.model is None:
            raise RuntimeError('Model not trained.')

        # Price range: cost * (1 + margin_floor) to cost * 2.0
        min_price = cost * (1 + margin_floor)
        max_price = cost * 2.0

        # Grid search over 20 price points
        prices = np.linspace(min_price, max_price, 20)
        best_score = -1
        optimal_price = min_price
        best_prob = 0.0
        best_margin = margin_floor

        # Assume historical avg is current mid-range
        historical_avg = (min_price + max_price) / 2

        for price in prices:
            # Update features
            features = base_features.copy()
            features['price_vs_historical_avg_ratio'] = price / historical_avg
            margin_pct = (price - cost) / price * 100
            features['margin_pct'] = margin_pct

            # Predict acceptance probability
            prob = self.predict_probability(features)

            # Score: expected value = prob * margin
            score = prob * (margin_pct / 100)

            if score > best_score:
                best_score = score
                optimal_price = price
                best_prob = prob
                best_margin = margin_pct / 100

        reasoning = (
            f"Optimal price R{optimal_price:.2f} balances {best_margin*100:.1f}% margin "
            f"with {best_prob*100:.0f}% acceptance probability (expected value: {best_score:.3f})"
        )

        return {
            'optimal_price': float(optimal_price),
            'expected_margin_pct': float(best_margin * 100),
            'acceptance_probability': float(best_prob),
            'reasoning': reasoning,
        }

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
