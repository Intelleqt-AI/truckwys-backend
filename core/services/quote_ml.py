"""LightGBM-based quote margin prediction model for SA freight."""

import csv
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

# joblib (model persistence) ships with sklearn — import it ungated so the
# win-probability model can load/save even when the heavier margin stack is absent.
try:
    import joblib
except Exception:
    joblib = None

# Margin-model stack (LightGBM). Heavy; gates the margin regressor only.
try:
    import lightgbm as lgb
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import train_test_split
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

# Win-probability stack — only sklearn (logistic regression) + joblib, which are
# present even without LightGBM/pandas. Decoupled so the win model can actually
# learn from QuoteOutcome data and the sweet-spot curve stops being a heuristic.
try:
    from sklearn.linear_model import LogisticRegression as _WinLR  # noqa: F401
    WIN_ML_AVAILABLE = joblib is not None
except Exception:
    WIN_ML_AVAILABLE = False


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

# Process-local cache of resolved (model, scope, sample_count) by
# (scope, user_id_or_None), so a burst of /quotes/analyze/ calls under the
# 700ms frontend debounce doesn't re-hit disk for every keystroke. NOT
# Django's cache framework: this project's cache backend is Postgres
# DatabaseCache, fine for small serializable values but not for holding a
# live deserialized sklearn pipeline object in memory. TTL keeps a fresh
# retrain visible within WIN_MODEL_CACHE_TTL_SECONDS; retrains themselves are
# already debounced far longer than that, so this never becomes the bottleneck.
_MODEL_CACHE: Dict[Tuple[str, Optional[int]], Tuple[Any, float]] = {}


class WinProbabilityModel:
    """
    Classifier that predicts P(quote accepted | features) — see
    core.services.quote_features for the full v2 feature set. Two storage
    scopes coexist:
      - scope='user', user_id=N: trained only on that quoting user's own
        QuoteOutcome rows.
      - scope='global' (default): trained on every company's pooled rows,
        the fallback tier when a user doesn't have enough of their own data.

    Training lives in core.services.quote_training (build_win_training_matrix_
    for_scope / retrain_win_model_for_scope); this class only loads/saves/
    serves whatever artifact training produced.
    """

    def __init__(self, scope: str = 'global', user_id: Optional[int] = None):
        # Only needs sklearn + joblib (not the LightGBM margin stack).
        if not WIN_ML_AVAILABLE:
            raise ImportError("Win-probability ML libraries (sklearn/joblib) not available")

        self.scope = scope
        self.user_id = user_id if scope == 'user' else None
        base = Path(settings.MEDIA_ROOT) / 'ml_models'
        self.MODEL_DIR = (base / 'users' / str(self.user_id)) if scope == 'user' else (base / 'global')
        self.MODEL_PATH = self.MODEL_DIR / 'win_probability_model.pkl'
        self.METADATA_PATH = self.MODEL_DIR / 'win_probability_metadata.json'

        self.model = None
        self.metadata = {}
        self.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self._load_model()

    def _migrate_legacy_global_file(self) -> bool:
        """One-time lazy migration: the pre-two-tier layout wrote a single
        flat ml_models/win_probability_model.pkl. If that legacy file exists
        and the new global/ location doesn't yet, adopt it (load + immediately
        re-save into the new path via the existing atomic save) rather than
        requiring a separate deploy-time migration step. No-ops for user scope
        or once the new location exists. Returns True if it migrated something."""
        if self.scope != 'global' or self.MODEL_PATH.exists():
            return False
        legacy_path = Path(settings.MEDIA_ROOT) / 'ml_models' / 'win_probability_model.pkl'
        legacy_meta = Path(settings.MEDIA_ROOT) / 'ml_models' / 'win_probability_metadata.json'
        if not legacy_path.exists():
            return False
        try:
            self.model = joblib.load(legacy_path)
            if legacy_meta.exists():
                with open(legacy_meta, 'r') as f:
                    self.metadata = json.load(f)
            # Legacy artifacts predate feature_version/feature_names entirely —
            # the schema-compatibility check in _load_model() will correctly
            # refuse to serve them until the next real retrain, but we still
            # physically relocate the file so it isn't silently orphaned.
            self._save_model()
            logger.info('Migrated legacy global win-model file to %s', self.MODEL_PATH)
            return True
        except Exception as exc:
            logger.warning('Legacy win-model migration failed: %s', exc)
            self.model = None
            self.metadata = {}
            return False

    def _schema_compatible(self) -> bool:
        """A model is only servable if its recorded feature_names exactly
        match what quote_features currently considers CORE or FULL for its
        own training_sample_count — anything else (an old 7-feature pickle, a
        mid-migration artifact) is treated as untrained rather than risking a
        shape-mismatch crash (or worse, a silently misaligned vector) inside
        predict_proba(). This is what makes future feature-list changes safe
        by construction."""
        names = self.metadata.get('feature_names')
        if not names:
            return False
        from core.services import quote_features
        n = int(self.metadata.get('training_sample_count') or 0)
        return list(names) == list(quote_features.feature_tier_for(n))

    def _load_model(self):
        """Load saved model and metadata if available and schema-compatible."""
        if not self.MODEL_PATH.exists():
            if not self._migrate_legacy_global_file():
                return
        else:
            try:
                self.model = joblib.load(self.MODEL_PATH)
                if self.METADATA_PATH.exists():
                    with open(self.METADATA_PATH, 'r') as f:
                        self.metadata = json.load(f)
            except Exception as exc:
                logger.warning('Failed to load WinProbabilityModel: %s', exc)
                self.model = None
                return

        if self.model is not None and not self._schema_compatible():
            logger.info(
                'Discarding %s win-model (scope=%s, user=%s): feature schema '
                'no longer matches quote_features — falls back to heuristic '
                'until the next retrain.', self.MODEL_PATH, self.scope, self.user_id,
            )
            self.model = None

    def _save_model(self):
        """Save model and metadata to disk atomically (temp file + os.replace)
        so a concurrent worker never reads a half-written pickle.

        Training itself lives in core.services.quote_training
        (retrain_win_model_for_scope) — the single training path, which
        assigns model/metadata and calls this.
        """
        import os
        import tempfile
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.MODEL_DIR, suffix='.pkl.tmp')
            os.close(fd)
            try:
                joblib.dump(self.model, tmp_path)
                os.replace(tmp_path, self.MODEL_PATH)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            fd, tmp_meta = tempfile.mkstemp(dir=self.MODEL_DIR, suffix='.json.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(self.metadata, f, indent=2)
                os.replace(tmp_meta, self.METADATA_PATH)
            finally:
                if os.path.exists(tmp_meta):
                    os.unlink(tmp_meta)

            logger.info('Saved WinProbabilityModel to %s', self.MODEL_PATH)
        except Exception as exc:
            logger.error('Failed to save WinProbabilityModel: %s', exc)

    def predict_proba(self, features: dict) -> float:
        """Predict P(accepted) from a core.services.quote_features feature
        dict. Falls back to the heuristic sigmoid (core.services.win_prediction.
        heuristic_win_proba) when untrained — kept here too, verbatim, so this
        class stays independently usable; win_prediction is the one true home
        for it and everything else should import from there."""
        if self.model is None:
            from core.services.win_prediction import heuristic_win_proba
            return heuristic_win_proba(features)

        from core.services import quote_features
        feature_names = self.metadata.get('feature_names') or quote_features.CORE_FEATURES
        X = np.array([quote_features.vectorize(features, feature_names)])
        prob = float(self.model.predict_proba(X)[0, 1])
        return max(0.0, min(1.0, prob))

    def is_trained(self) -> bool:
        return self.model is not None

    @staticmethod
    def resolve_for_user(user_id: Optional[int]) -> Tuple[Optional['WinProbabilityModel'], Optional[str], int]:
        """(model, scope, sample_count). Tries the user's own model first,
        then the global model, then (None, None, 0) — the caller degrades to
        "AI unavailable", never to the heuristic silently labeled as AI.
        scope is 'user' | 'global' | None. Cached per-process (see
        _MODEL_CACHE) for WIN_MODEL_CACHE_TTL_SECONDS."""
        if not WIN_ML_AVAILABLE:
            return None, None, 0
        import time as _time
        ttl = float(getattr(settings, 'WIN_MODEL_CACHE_TTL_SECONDS', 60))

        def _cached(cache_key, min_samples):
            hit = _MODEL_CACHE.get(cache_key)
            if hit is not None and (_time.monotonic() - hit[1]) < ttl:
                model = hit[0]
            else:
                scope, uid = cache_key
                try:
                    model = WinProbabilityModel(scope=scope, user_id=uid)
                except Exception as exc:
                    logger.warning('resolve_for_user: failed to construct %s model: %s', scope, exc)
                    model = None
                _MODEL_CACHE[cache_key] = (model, _time.monotonic())
            if model is None or not model.is_trained():
                return None, 0
            n = int(model.metadata.get('training_sample_count') or 0)
            if n < min_samples:
                return None, n
            return model, n

        if user_id:
            model, n = _cached(('user', user_id), int(getattr(settings, 'WIN_MODEL_USER_MIN_SAMPLES', 40)))
            if model is not None:
                return model, 'user', n

        model, n = _cached(('global', None), int(getattr(settings, 'WIN_MODEL_GLOBAL_MIN_SAMPLES', 40)))
        if model is not None:
            return model, 'global', n

        return None, None, 0
