"""ML pipeline for risk prediction using XGBoost."""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
import numpy as np

from django.conf import settings

# ML imports - will be installed separately
try:
    import joblib
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import (
        roc_auc_score, accuracy_score, precision_score,
        recall_score, f1_score, confusion_matrix
    )
    from sklearn.preprocessing import StandardScaler
    import xgboost as xgb
    import shap
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False


@dataclass
class RiskPrediction:
    """Risk prediction result from ML model."""
    probability: float  # Probability of high risk (>30 days late)
    expected_days_late: float  # Expected days late
    confidence: float  # Model confidence (0-1)
    tier: str  # Risk tier: LOW, MEDIUM, HIGH, VERY_HIGH
    prediction_date: datetime
    shap_explanations: Optional[list] = None  # SHAP feature explanations

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        result = {
            'probability': self.probability,
            'expected_days_late': self.expected_days_late,
            'confidence': self.confidence,
            'tier': self.tier,
            'prediction_date': self.prediction_date.isoformat(),
        }
        if self.shap_explanations is not None:
            result['shap_explanations'] = self.shap_explanations
        return result


class RiskMLPipeline:
    """
    ML pipeline for invoice payment risk prediction.

    Uses XGBoost classifier to predict high-risk invoices (>30 days late).
    Includes feature scaling, model training, prediction, and SHAP explanations.
    """

    # Model paths
    MODEL_DIR = Path(settings.MEDIA_ROOT) / 'ml_models'
    MODEL_PATH = MODEL_DIR / 'risk_model.joblib'
    SCALER_PATH = MODEL_DIR / 'risk_scaler.joblib'
    METADATA_PATH = MODEL_DIR / 'risk_metadata.json'

    # Risk tier thresholds
    TIER_THRESHOLDS = {
        'LOW': 0.25,
        'MEDIUM': 0.50,
        'HIGH': 0.75,
        'VERY_HIGH': 1.00,
    }

    def __init__(self):
        """Initialize ML pipeline."""
        if not ML_AVAILABLE:
            raise ImportError(
                "ML libraries not available. Install with: "
                "pip install scikit-learn xgboost joblib shap"
            )

        self.model: Optional[xgb.XGBClassifier] = None
        self.scaler: Optional[StandardScaler] = None
        self.metadata: Dict[str, Any] = {}
        self.feature_names: list[str] = []
        self.explainer: Optional[shap.TreeExplainer] = None

        # Create model directory if it doesn't exist
        self.MODEL_DIR.mkdir(parents=True, exist_ok=True)

        # Try to load existing model
        self._load_model()

    def _load_model(self) -> bool:
        """
        Load trained model from disk.

        Returns:
            True if model loaded successfully, False otherwise
        """
        try:
            if self.MODEL_PATH.exists() and self.SCALER_PATH.exists():
                self.model = joblib.load(self.MODEL_PATH)
                self.scaler = joblib.load(self.SCALER_PATH)

                if self.METADATA_PATH.exists():
                    with open(self.METADATA_PATH, 'r') as f:
                        self.metadata = json.load(f)
                        self.feature_names = self.metadata.get('feature_names', [])

                # Initialize SHAP explainer for the loaded model
                if self.model is not None:
                    self.explainer = shap.TreeExplainer(self.model)

                return True
        except Exception as e:
            print(f"Error loading model: {e}")
            self.model = None
            self.scaler = None
            self.explainer = None

        return False

    def _save_model(self) -> None:
        """Save trained model to disk."""
        try:
            joblib.dump(self.model, self.MODEL_PATH)
            joblib.dump(self.scaler, self.SCALER_PATH)

            with open(self.METADATA_PATH, 'w') as f:
                json.dump(self.metadata, f, indent=2)

        except Exception as e:
            print(f"Error saving model: {e}")
            raise

    def train(self, payment_outcomes_qs, test_size: float = 0.2, random_state: int = 42) -> Dict[str, Any]:
        """
        Train the ML model on payment outcomes.

        Args:
            payment_outcomes_qs: QuerySet of PaymentOutcome instances with feature_snapshot
            test_size: Fraction of data to use for testing
            random_state: Random seed for reproducibility

        Returns:
            Dictionary with training metrics and results
        """
        if not ML_AVAILABLE:
            raise ImportError("ML libraries not installed")

        # Extract features and labels
        X = []
        y = []
        feature_names = None

        # First pass: determine the feature set from the most common structure
        feature_sets = {}
        for outcome in payment_outcomes_qs:
            if outcome.has_complete_data:
                features = outcome.feature_snapshot
                feature_key = tuple(sorted(features.keys()))
                feature_sets[feature_key] = feature_sets.get(feature_key, 0) + 1

        if not feature_sets:
            raise ValueError("No valid payment outcomes found")

        # Use the most common feature set
        most_common_features = max(feature_sets.items(), key=lambda x: x[1])[0]
        feature_names = list(most_common_features)

        print(f"Using feature set with {len(feature_names)} features: {feature_names}")
        print(f"Found {feature_sets[most_common_features]} outcomes with this feature set")

        # Second pass: extract data only from outcomes with matching feature set
        for outcome in payment_outcomes_qs:
            if outcome.has_complete_data:
                features = outcome.feature_snapshot
                feature_key = tuple(sorted(features.keys()))

                # Only use outcomes with the most common feature set
                if feature_key == most_common_features:
                    # Ensure features are in the correct order
                    X.append([features[name] for name in feature_names])
                    y.append(1 if outcome.is_high_risk else 0)  # Binary: high risk or not

        if len(X) < 50:
            raise ValueError(f"Insufficient training data: {len(X)} samples (need at least 50)")

        X = np.array(X)
        y = np.array(y)
        self.feature_names = feature_names

        # Split data
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=random_state, stratify=y
        )

        # Scale features
        self.scaler = StandardScaler()
        X_train_scaled = self.scaler.fit_transform(X_train)
        X_test_scaled = self.scaler.transform(X_test)

        # Train XGBoost model
        self.model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=6,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=random_state,
            eval_metric='auc',
            use_label_encoder=False,
        )

        self.model.fit(X_train_scaled, y_train)

        # Evaluate on test set
        y_pred = self.model.predict(X_test_scaled)
        y_pred_proba = self.model.predict_proba(X_test_scaled)[:, 1]

        metrics = {
            'accuracy': float(accuracy_score(y_test, y_pred)),
            'precision': float(precision_score(y_test, y_pred, zero_division=0)),
            'recall': float(recall_score(y_test, y_pred, zero_division=0)),
            'f1': float(f1_score(y_test, y_pred, zero_division=0)),
            'auc': float(roc_auc_score(y_test, y_pred_proba)),
            'confusion_matrix': confusion_matrix(y_test, y_pred).tolist(),
        }

        # Store metadata
        self.metadata = {
            'training_date': datetime.now().isoformat(),
            'sample_count': len(X),
            'train_count': len(X_train),
            'test_count': len(X_test),
            'high_risk_count': int(np.sum(y)),
            'high_risk_rate': float(np.mean(y)),
            'feature_count': len(feature_names),
            'feature_names': feature_names,
            'metrics': metrics,
        }

        # Initialize SHAP explainer
        self.explainer = shap.TreeExplainer(self.model)

        # Save model
        self._save_model()

        return {
            'success': True,
            'message': f'Model trained on {len(X)} samples',
            'metrics': metrics,
            'metadata': self.metadata,
        }

    def predict(self, features: Dict[str, Any], include_shap: bool = True, top_n: int = 5) -> Optional[RiskPrediction]:
        """
        Predict payment risk for invoice features.

        Args:
            features: Dictionary of feature values
            include_shap: Whether to include SHAP explanations (default True)
            top_n: Number of top SHAP features to include (default 5)

        Returns:
            RiskPrediction object or None if model not trained
        """
        if self.model is None or self.scaler is None:
            return None

        try:
            # Convert features dict to array in correct order
            X = np.array([[features[name] for name in self.feature_names]])

            # Scale features
            X_scaled = self.scaler.transform(X)

            # Get prediction probability
            proba = self.model.predict_proba(X_scaled)[0, 1]  # Probability of high risk

            # Estimate expected days late based on probability
            # Simple linear mapping: 0% = 0 days, 100% = 60 days
            expected_days_late = proba * 60.0

            # Calculate confidence based on how far from decision boundary (0.5)
            confidence = abs(proba - 0.5) * 2.0  # Scale to 0-1

            # Determine tier
            tier = self._probability_to_tier(proba)

            # Generate SHAP explanations if requested
            shap_explanations = None
            if include_shap and self.explainer is not None:
                shap_explanations = self._get_shap_explanations(X_scaled, top_n)

            return RiskPrediction(
                probability=float(proba),
                expected_days_late=float(expected_days_late),
                confidence=float(confidence),
                tier=tier,
                prediction_date=datetime.now(),
                shap_explanations=shap_explanations,
            )

        except Exception as e:
            print(f"Error in prediction: {e}")
            return None

    def explain(self, features: Dict[str, Any], top_n: int = 10) -> list[Tuple[str, float, str]]:
        """
        Explain prediction using feature importances.

        Args:
            features: Dictionary of feature values
            top_n: Number of top features to return

        Returns:
            List of (feature_name, importance, direction) tuples
        """
        if self.model is None:
            return []

        try:
            # Get feature importances from XGBoost
            importances = self.model.feature_importances_

            # Combine with feature names and values
            explanations = []
            for name, importance, value in zip(self.feature_names, importances, features.values()):
                # Determine direction based on feature value
                # Higher values typically = higher risk for most features
                direction = 'INCREASES_RISK' if value > 0.5 else 'DECREASES_RISK'
                explanations.append((name, float(importance), direction))

            # Sort by importance and return top N
            explanations.sort(key=lambda x: x[1], reverse=True)
            return explanations[:top_n]

        except Exception as e:
            print(f"Error in explanation: {e}")
            return []

    def get_model_info(self) -> Optional[Dict[str, Any]]:
        """
        Get information about the trained model.

        Returns:
            Dictionary with model metadata or None if not trained
        """
        if self.model is None:
            return None

        return {
            'trained': True,
            'training_date': self.metadata.get('training_date'),
            'sample_count': self.metadata.get('sample_count'),
            'feature_count': self.metadata.get('feature_count'),
            'metrics': self.metadata.get('metrics'),
            'high_risk_rate': self.metadata.get('high_risk_rate'),
            'model_type': 'XGBoost Classifier',
        }

    def _get_shap_explanations(self, X_scaled: np.ndarray, top_n: int = 5) -> list:
        """
        Generate SHAP explanations for prediction.

        Args:
            X_scaled: Scaled feature array (single sample)
            top_n: Number of top features to return

        Returns:
            List of dictionaries with feature, impact, and direction
        """
        try:
            # Calculate SHAP values
            shap_values = self.explainer.shap_values(X_scaled)

            # For binary classification, shap_values may be a list [class_0, class_1]
            # We want the SHAP values for the positive class (high risk)
            if isinstance(shap_values, list):
                shap_values = shap_values[1]  # Class 1 (high risk)

            # Get SHAP values for this single prediction
            shap_vals = shap_values[0] if shap_values.ndim > 1 else shap_values

            # Create list of (feature_name, shap_value) tuples
            feature_impacts = []
            for i, feature_name in enumerate(self.feature_names):
                shap_value = float(shap_vals[i])
                feature_impacts.append({
                    'feature': feature_name,
                    'impact': abs(shap_value),  # Absolute impact for ranking
                    'shap_value': shap_value,  # Actual SHAP value (can be negative)
                    'direction': 'INCREASES_RISK' if shap_value > 0 else 'DECREASES_RISK'
                })

            # Sort by absolute impact (descending)
            feature_impacts.sort(key=lambda x: x['impact'], reverse=True)

            # Return top N features
            top_features = feature_impacts[:top_n]

            # Format for API response
            return [
                {
                    'feature': f['feature'],
                    'impact': round(f['shap_value'], 4),  # Use actual SHAP value
                    'direction': f['direction']
                }
                for f in top_features
            ]

        except Exception as e:
            print(f"Error generating SHAP explanations: {e}")
            return []

    def _probability_to_tier(self, probability: float) -> str:
        """
        Convert probability to risk tier.

        Args:
            probability: Risk probability (0-1)

        Returns:
            Risk tier string
        """
        if probability < self.TIER_THRESHOLDS['LOW']:
            return 'LOW'
        elif probability < self.TIER_THRESHOLDS['MEDIUM']:
            return 'MEDIUM'
        elif probability < self.TIER_THRESHOLDS['HIGH']:
            return 'HIGH'
        else:
            return 'VERY_HIGH'

    def is_model_trained(self) -> bool:
        """Check if model is trained and ready for predictions."""
        return self.model is not None and self.scaler is not None
