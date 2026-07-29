"""Close the quote ML flywheel: retrain the win-probability model from real
QuoteOutcome data, using only sklearn + numpy (no LightGBM/pandas needed).

The win model drives the profit sweet-spot curve on the quote screen. Until it
has learned from real accepted/rejected outcomes it falls back to a heuristic
ladder; this module is what turns captured outcomes into a fitted model and
keeps it fresh as more quotes are decided.
"""
import logging
from datetime import datetime

import numpy as np
from django.conf import settings
from django.db.models import F, Q

logger = logging.getLogger(__name__)

WIN_FEATURE_COLS = [
    'price_ratio', 'client_tier', 'days_until_departure',
    'historical_acceptance_rate', 'month', 'day_of_week', 'route_popularity',
]

_TIER_MAP = {'new': 0, 'regular': 1, 'standard': 1, 'vip': 2, 'premium': 2}


def _min_samples() -> int:
    return int(getattr(settings, 'WIN_MODEL_MIN_SAMPLES', 40))


def _lane_market_rate(origin, destination, vehicle_type, cache):
    """Best-effort market rate for a lane, cached per (o,d,vt)."""
    key = (origin or '', destination or '', (vehicle_type or '').lower())
    if key in cache:
        return cache[key]
    rate = None
    try:
        from core.services.lane_benchmark import compute_lane_benchmark
        b = compute_lane_benchmark(origin, destination, vehicle_type)
        if not b.get('available'):
            b = compute_lane_benchmark(origin, destination)
        if b.get('available'):
            rate = float(b.get('market_avg_rate') or 0) or None
    except Exception:
        rate = None
    cache[key] = rate
    return rate


def build_win_training_matrix():
    """Return (X, y, n) numpy arrays engineered from QuoteOutcome. Never raises.

    Prefers the point-in-time feature snapshots stamped on each QuoteOutcome by
    quote_outcome_capture (price_ratio, days_until_departure, quote month/dow,
    historical_acceptance_rate). Legacy rows without snapshots are
    reconstructed best-effort from the quote itself — never from the
    outcome-marking click time, and never with hardcoded constants.
    """
    from core.models import QuoteOutcome

    outcomes = list(
        QuoteOutcome.objects.filter(outcome__in=['accepted', 'rejected'])
        # Exclude pre-launch/test outcomes for companies that reset their
        # training clock (Company.ai_training_started_at) — see win_model_status.
        .filter(
            Q(quote__company__ai_training_started_at__isnull=True)
            | Q(created_at__gte=F('quote__company__ai_training_started_at'))
        )
        .select_related('quote', 'quote__customer')
    )
    if not outcomes:
        return np.empty((0, len(WIN_FEATURE_COLS))), np.empty((0,)), 0

    from core.services.lane_benchmark import canon_code

    # Precompute legacy fallbacks: per-customer acceptance rate and per-lane
    # popularity (keyed on CANONICAL lane codes so DUR/DBN spellings pool).
    cust_total, cust_acc, lane_count = {}, {}, {}
    finals = []
    for o in outcomes:
        cid = getattr(o.quote, 'customer_id', None)
        if cid is not None:
            cust_total[cid] = cust_total.get(cid, 0) + 1
            if o.outcome == 'accepted':
                cust_acc[cid] = cust_acc.get(cid, 0) + 1
        lane = (canon_code(o.origin), canon_code(o.destination))
        lane_count[lane] = lane_count.get(lane, 0) + 1
        if o.final_price:
            finals.append(float(o.final_price))
    max_lane = max(lane_count.values()) if lane_count else 1
    global_median = float(np.median(finals)) if finals else 0.0

    rate_cache = {}
    rows, labels = [], []
    for o in outcomes:
        final = float(o.final_price) if o.final_price else 0.0
        if final <= 0:
            continue

        # price_ratio: snapshot first, else reconstruct against today's benchmark.
        if o.price_ratio:
            price_ratio = float(o.price_ratio)
        else:
            market = _lane_market_rate(o.origin, o.destination, o.vehicle_type, rate_cache) or global_median
            if not market or market <= 0:
                continue
            price_ratio = final / market

        cid = getattr(o.quote, 'customer_id', None)
        if o.historical_acceptance_rate is not None:
            hist = float(o.historical_acceptance_rate)
        else:
            # Leave-one-out: the row's own label must never sit inside its own
            # feature. No prior history -> 0.5, matching the serving-time
            # default in AIQuoteAnalyzeView._derive_client_features.
            prior_total = cust_total.get(cid, 0) - 1 if cid is not None else 0
            prior_acc = cust_acc.get(cid, 0) - (1 if o.outcome == 'accepted' else 0)
            hist = (prior_acc / prior_total) if prior_total > 0 else 0.5

        tier = _TIER_MAP.get((o.client_tier or '').lower(), 0)

        # Urgency: snapshot, else quote pickup minus quote creation, else 7.
        if o.days_until_departure is not None:
            days = int(o.days_until_departure)
        else:
            q = o.quote
            if getattr(q, 'pickup_date', None) and getattr(q, 'created_at', None):
                days = max(0, (q.pickup_date - q.created_at.date()).days)
            else:
                days = 7

        # Seasonality from QUOTE creation time, not the outcome click time.
        quote_created = getattr(o.quote, 'created_at', None)
        month = o.quote_month or (quote_created.month if quote_created else 1)
        dow = o.quote_dow if o.quote_dow is not None else (quote_created.weekday() if quote_created else 0)

        # Popularity: prefer the capture-time snapshot (identical definition to
        # inference); legacy rows fall back to within-training normalization.
        if o.route_popularity is not None:
            popularity = float(o.route_popularity)
        else:
            popularity = lane_count.get(
                (canon_code(o.origin), canon_code(o.destination)), 1) / max_lane

        rows.append([price_ratio, tier, days, hist, month, dow, popularity])
        labels.append(1 if o.outcome == 'accepted' else 0)

    return np.array(rows, dtype=float), np.array(labels, dtype=int), len(rows)


def retrain_win_model(min_samples=None) -> dict:
    """Fit the win-probability logistic model from QuoteOutcome data. Never raises."""
    from core.services.quote_ml import WIN_ML_AVAILABLE, WinProbabilityModel

    if not WIN_ML_AVAILABLE:
        return {'trained': False, 'reason': 'sklearn/joblib not available'}

    min_samples = min_samples or _min_samples()
    try:
        X, y, n = build_win_training_matrix()
    except Exception as exc:
        logger.warning('win training matrix build failed: %s', exc)
        return {'trained': False, 'reason': f'feature build failed: {exc}'}

    if n < min_samples:
        return {'trained': False, 'reason': f'insufficient data ({n}/{min_samples})', 'samples': n}
    if len(set(y.tolist())) < 2:
        return {'trained': False, 'reason': 'only one outcome class present', 'samples': n}

    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    try:
        if n >= 25:
            X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
        else:
            X_tr, X_te, y_tr, y_te = X, X, y, y

        # Scale features before the L2-penalized fit — otherwise price_ratio
        # (~1.0) is drowned out by month (1-12) and the win curve goes flat.
        model = make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, random_state=42, C=1.0),
        )
        model.fit(X_tr, y_tr)

        proba = model.predict_proba(X_te)[:, 1]
        acc = float(accuracy_score(y_te, model.predict(X_te)))
        try:
            auc = float(roc_auc_score(y_te, proba)) if len(set(y_te.tolist())) > 1 else None
        except Exception:
            auc = None

        win = WinProbabilityModel()
        win.model = model
        win.metadata = {
            'trained_at': datetime.now().isoformat(),
            'training_count': int(len(X_tr)),
            'sample_count': int(n),
            'accuracy': round(acc, 4),
            'auc': round(auc, 4) if auc is not None else None,
            'feature_names': WIN_FEATURE_COLS,
            'version': '1.0.0',
        }
        win._save_model()
        logger.info('Win model retrained on %s outcomes (auc=%s)', n, auc)
        return {'trained': True, 'samples': n, 'accuracy': acc, 'auc': auc}
    except Exception as exc:
        logger.warning('win model fit failed: %s', exc)
        return {'trained': False, 'reason': f'fit failed: {exc}', 'samples': n}


def win_model_status(company=None) -> dict:
    """Honest snapshot of the win model for the quote UI. Never raises.

    Pass `company` to scope the outcome count to one tenant (what the UI
    shows); the model itself is global, so its metadata is unscoped.
    """
    from core.models import QuoteOutcome
    qs = QuoteOutcome.objects.filter(outcome__in=['accepted', 'rejected'])
    if company is not None:
        qs = qs.filter(quote__company=company)
        if company.ai_training_started_at is not None:
            qs = qs.filter(created_at__gte=company.ai_training_started_at)
    outcomes = qs.count()
    min_needed = _min_samples()

    meta = {}
    try:
        from core.services.quote_ml import WIN_ML_AVAILABLE, WinProbabilityModel
        if WIN_ML_AVAILABLE:
            meta = getattr(WinProbabilityModel(), 'metadata', {}) or {}
    except Exception:
        meta = {}

    trained = bool(meta.get('trained_at'))
    return {
        'mode': 'learned' if trained else 'heuristic',
        'trained': trained,
        'outcomes_collected': outcomes,
        'outcomes_needed': min_needed,
        'progress_pct': min(100, round(outcomes / min_needed * 100)) if min_needed else 0,
        'auc': meta.get('auc'),
        'accuracy': meta.get('accuracy'),
        'last_trained': meta.get('trained_at'),
        'sample_count': meta.get('sample_count'),
    }


# NOTE: retraining is scheduled — Celery Beat runs core.tasks.retrain_win_model
# nightly (idempotent, no-ops below the sample threshold). The old fire-and-
# forget daemon-thread retrain inside the web request was removed: it died with
# the worker, raced joblib.dump across gunicorn workers, and skipped retrains
# whenever count % 10 != 0.
