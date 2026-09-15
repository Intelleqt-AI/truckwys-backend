"""The boundary between the training/storage layer (core.services.quote_ml,
quote_training) and everything that serves a prediction (margin_optimizer,
quote_analysis, views_ai_quote). Owns:

- The heuristic sigmoid fallback (the ONE place it's defined — previously
  hand-copied into margin_optimizer.py and AIQuoteSuggestionView).
- Tiered model resolution (user -> global -> unavailable), wrapped as a
  PredictionContext so callers never have to branch on whether a real model
  is behind the callable they got back.
- Two-tier progress reporting for the "N/40 outcomes" UI chip.

Requirement (from the redesign spec): never let the heuristic be presented as
a trained AI prediction. PredictionContext.available is exactly that signal —
callers gate any "ai_prediction" API block on it, never on whether
predict_proba happens to return a number (it always does, heuristic or not).
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PredictionContext:
    available: bool
    scope: Optional[str]              # 'user' | 'global' | None
    sample_count: int
    predict_proba: Callable[[dict], float]   # ALWAYS callable — heuristic when unavailable


def heuristic_win_proba(features: dict) -> float:
    """Smooth heuristic used until a real trained model qualifies. A logistic
    (sigmoid) curve in price-vs-market: at market price (ratio = 1.0) win ~
    0.5, falling smoothly as you price above market and rising below it.
    Being smooth & monotonic (no hardcoded buckets) means the margin optimiser
    finds a genuine interior optimum instead of snapping to a bucket edge.
    Then nudge by the real signals available even without a trained model.

    Deliberately only reads price_ratio/days_until_departure/client_tier/
    historical_acceptance_rate — the newer v2 features (route_popularity,
    seasonality, user/lane signals, etc.) have no natural hand-tuned
    coefficient without real data to fit one from; they only affect the
    prediction once an actual model is trained. This is a continuation of the
    original heuristic's own design, not a new gap.
    """
    import math

    price_ratio = float(features.get('price_ratio', 1.0) or 1.0)
    days_until_departure = int(features.get('days_until_departure', 2) or 2)
    client_tier = int(features.get('client_tier', 0) or 0)
    historical_acceptance_rate = float(features.get('historical_acceptance_rate', 0.7) or 0.7)

    STEEPNESS = 7.0  # how sharply win-prob reacts to price vs market
    base_prob = 1.0 / (1.0 + math.exp(STEEPNESS * (price_ratio - 1.0)))

    # Urgency: the closer to departure, the more a shipper will accept
    # (capacity gets scarce). Bounded nudge.
    urgency_adj = max(-0.05, min(0.10, (7 - days_until_departure) * 0.01))

    # Client tier: VIP relationships convert better, new clients worse.
    tier_adj = {0: -0.05, 1: 0.0, 2: 0.07}.get(client_tier, 0.0)

    # Anchor mildly toward the client's own historical acceptance rate.
    hist_adj = (historical_acceptance_rate - 0.5) * 0.10

    prob = base_prob + urgency_adj + tier_adj + hist_adj
    return max(0.02, min(0.98, prob))


def resolve_prediction_context(user, company) -> PredictionContext:
    """Try the user's model, then the global model, then unavailable. Never
    raises. `user` may be None (unauthenticated/public flow) -> unavailable."""
    user_id = getattr(user, 'id', None)
    try:
        from core.services.quote_ml import WinProbabilityModel
        model, scope, sample_count = WinProbabilityModel.resolve_for_user(user_id)
    except Exception as exc:
        logger.warning('win model resolution failed: %s', exc)
        return PredictionContext(False, None, 0, heuristic_win_proba)

    if model is None:
        return PredictionContext(False, None, sample_count or 0, heuristic_win_proba)
    return PredictionContext(True, scope, sample_count, model.predict_proba)


def model_progress(user, company) -> dict:
    """Two-tier progress for the UI's 'still learning' chip — computed
    directly from QuoteOutcome counts, independent of whether a model FILE
    actually exists yet, so the progress bar is meaningful even at 0/40."""
    from django.conf import settings
    from core.models import QuoteOutcome

    user_needed = int(getattr(settings, 'WIN_MODEL_USER_MIN_SAMPLES', 40))
    global_needed = int(getattr(settings, 'WIN_MODEL_GLOBAL_MIN_SAMPLES', 40))

    def _counts(qs, needed):
        n = qs.count()
        return {
            'outcomes_collected': n,
            'outcomes_needed': needed,
            'progress_pct': min(100, round(n / needed * 100)) if needed else 0,
            'qualifies': n >= needed,
        }

    base = QuoteOutcome.objects.filter(outcome__in=['accepted', 'rejected'])

    user_id = getattr(user, 'id', None)
    user_qs = base.filter(created_by_id=user_id) if user_id else base.none()

    # Global tier is platform-wide, not gated by any single company's own
    # ai_training_started_at reset — one tenant resetting their own clock
    # shouldn't hide the rest of the platform's contribution to the shared model.
    return {
        'user': _counts(user_qs, user_needed),
        'global': _counts(base, global_needed),
    }
