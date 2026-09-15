"""Close the quote ML flywheel: retrain the win-probability model(s) from
real QuoteOutcome data.

Two-tier architecture: a per-user model trained only on one quoting user's
own outcomes, and a global model pooling every company's outcomes as the
fallback tier when a user doesn't have enough of their own data yet. See
core.services.win_prediction for how a prediction resolves between the two,
and core.services.quote_features for the feature vector both are trained on.

Until a tier has learned from enough real accepted/rejected outcomes it has
no trained model at all (never a silently-substituted heuristic labeled as
one — see win_prediction.heuristic_win_proba for that fallback, which lives
one layer up from here). This module is what turns captured outcomes into a
validated, activated model artifact and keeps each tier fresh as more quotes
are decided.
"""
import logging
from datetime import datetime

import numpy as np
from django.conf import settings
from django.db.models import F, Q

from core.services import quote_features

logger = logging.getLogger(__name__)

# Every candidate algorithm this module knows how to build. Which of these
# are actually tried for a given retrain depends on sample size — see
# _candidates_for_size(). LightGBM is optional (requirements-ml.txt); a
# missing import just drops it from the comparison, never breaks the run.
CANDIDATE_ALGORITHMS = ['logistic_regression', 'gradient_boosting', 'lightgbm']


def _min_samples_for(scope: str) -> int:
    setting_name = 'WIN_MODEL_USER_MIN_SAMPLES' if scope == 'user' else 'WIN_MODEL_GLOBAL_MIN_SAMPLES'
    return int(getattr(settings, setting_name, 40))


def _cv_threshold() -> int:
    return int(getattr(settings, 'WIN_MODEL_CV_THRESHOLD', 150))


# ---------------------------------------------------------------------------
# Training matrix
# ---------------------------------------------------------------------------

def build_win_training_matrix_for_scope(scope: str, user_id=None):
    """Return (X, y, n, feature_names) engineered from QuoteOutcome. Never raises.

    scope='global': every company's pooled outcomes (minus each company's own
    ai_training_started_at cutoff, unchanged from before) — same as the
    original single-tier design.
    scope='user': only outcomes this user (Quote.created_by, snapshotted onto
    QuoteOutcome.created_by at record time) has personally decided.

    Prefers each row's feature_snapshot (frozen, versioned, at outcome-record
    time) when present and current-version; falls back to live reconstruction
    via quote_features.compute_features_for_quote() for legacy rows — itself
    leakage-safe (as_of pinned to the quote's own created_at), just not
    frozen against future changes to the feature-computation code the way a
    stored snapshot is.
    """
    from core.models import QuoteOutcome

    qs = (
        QuoteOutcome.objects.filter(outcome__in=['accepted', 'rejected'])
        # Exclude pre-launch/test outcomes for companies that reset their
        # training clock (Company.ai_training_started_at).
        .filter(
            Q(quote__company__ai_training_started_at__isnull=True)
            | Q(created_at__gte=F('quote__company__ai_training_started_at'))
        )
        .select_related('quote')
    )
    if scope == 'user':
        if not user_id:
            return np.empty((0, 0)), np.empty((0,)), 0, []
        qs = qs.filter(created_by_id=user_id)

    outcomes = list(qs)
    n = len(outcomes)
    if not n:
        return np.empty((0, 0)), np.empty((0,)), 0, []

    feature_names = quote_features.feature_tier_for(n)

    rows, labels = [], []
    for o in outcomes:
        try:
            snap = o.feature_snapshot or {}
            if snap.get('feature_version') == quote_features.FEATURE_VERSION and snap.get('features'):
                feats = snap['features']
            elif o.quote_id and o.quote is not None:
                feats = quote_features.compute_features_for_quote(o.quote, as_of=o.quote.created_at)
            else:
                continue
        except Exception as exc:
            logger.warning('build_win_training_matrix_for_scope: row %s skipped: %s', o.id, exc)
            continue
        rows.append(quote_features.vectorize(feats, feature_names))
        labels.append(1 if o.outcome == 'accepted' else 0)

    return np.array(rows, dtype=float), np.array(labels, dtype=int), len(rows), feature_names


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

def _build_candidate(name: str):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if name == 'logistic_regression':
        from sklearn.linear_model import LogisticRegression
        return make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=42, C=1.0))
    if name == 'gradient_boosting':
        from sklearn.ensemble import GradientBoostingClassifier
        return make_pipeline(StandardScaler(), GradientBoostingClassifier(random_state=42))
    if name == 'lightgbm':
        import lightgbm as lgb
        return lgb.LGBMClassifier(random_state=42, verbosity=-1)
    raise ValueError(f'unknown candidate algorithm: {name}')


def _candidates_for_size(n: int):
    """Data-size-aware candidate list. Below the CV threshold (essentially
    every per-user model near the 40-sample floor): logistic regression only
    — the fix for a tiny-n problem is regularization, not a bigger model
    family; a tree ensemble at n=40 overfits FASTER than a regularized linear
    model, not slower. Above it, widen the comparison as data allows."""
    threshold = _cv_threshold()
    if n < threshold:
        return ['logistic_regression']
    if n < 500:
        return ['logistic_regression', 'gradient_boosting']
    names = ['logistic_regression', 'gradient_boosting']
    try:
        import lightgbm  # noqa: F401
        names.append('lightgbm')
    except ImportError:
        pass
    return names


def benchmark_candidates(X, y, names) -> list:
    """Stratified k-fold CV per candidate, pooling out-of-fold predictions so
    every row contributes to evaluation (valuable specifically because
    per-user data is scarce). Returns a list of {'name','roc_auc','brier',
    'accuracy'} sorted best-first (ROC-AUC desc, Brier score asc tie-break —
    calibration, not raw accuracy, since accepted/rejected isn't guaranteed
    balanced). Never raises; a candidate that errors is simply dropped."""
    from sklearn.metrics import accuracy_score, roc_auc_score
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    class_counts = np.bincount(y)
    min_class = int(class_counts.min()) if len(class_counts) else 0
    k = max(2, min(5, min_class))

    results = []
    for name in names:
        try:
            model = _build_candidate(name)
            skf = StratifiedKFold(n_splits=k, shuffle=True, random_state=42)
            proba = cross_val_predict(model, X, y, cv=skf, method='predict_proba')[:, 1]
            preds = (proba >= 0.5).astype(int)
            auc = float(roc_auc_score(y, proba)) if len(set(y.tolist())) > 1 else 0.5
            brier = float(np.mean((proba - y) ** 2))
            acc = float(accuracy_score(y, preds))
            results.append({'name': name, 'roc_auc': round(auc, 4), 'brier': round(brier, 4), 'accuracy': round(acc, 4)})
        except Exception as exc:
            logger.warning('benchmark_candidates: %s failed: %s', name, exc)
    results.sort(key=lambda r: (-r['roc_auc'], r['brier']))
    return results


# ---------------------------------------------------------------------------
# MLModelVersion bookkeeping
# ---------------------------------------------------------------------------

def _current_active_version(scope, user_id):
    from core.models import MLModelVersion
    qs = MLModelVersion.objects.filter(scope=scope, status='active')
    qs = qs.filter(user_id=user_id) if scope == 'user' else qs.filter(user__isnull=True)
    return qs.order_by('-created_at').first()


def _record_model_version(scope, user_id, *, status, algorithm='', feature_names=None,
                           sample_count=0, accepted_count=0, rejected_count=0,
                           evaluation_metrics=None, hyperparameters=None, rejection_reason=''):
    from django.utils import timezone
    from core.models import MLModelVersion
    return MLModelVersion.objects.create(
        scope=scope, user_id=(user_id if scope == 'user' else None), status=status,
        algorithm=algorithm, feature_version=quote_features.FEATURE_VERSION,
        feature_names=feature_names or [], model_version=f'{scope}:{user_id or "-"}:{timezone.now().isoformat()}',
        training_sample_count=sample_count, accepted_count=accepted_count, rejected_count=rejected_count,
        evaluation_metrics=evaluation_metrics or {}, hyperparameters=hyperparameters or {},
        rejection_reason=rejection_reason, trained_at=timezone.now(),
    )


def _activate_model_version(scope, user_id, **kwargs):
    """Supersede any prior active row and record the new one as active — only
    ever called AFTER the artifact is already safely written to disk (file
    write is the source of truth; this is bookkeeping around it). Guarded by
    select_for_update() for the scope='user' case (protected by a real DB
    unique constraint too); the scope='global' case has no row to lock on a
    fresh first-ever training run, an accepted narrow race given Celery Beat
    only ever runs one global retrain at a time in practice."""
    from django.db import transaction
    from django.utils import timezone
    from core.models import MLModelVersion

    with transaction.atomic():
        existing = MLModelVersion.objects.select_for_update().filter(scope=scope, status='active')
        existing = existing.filter(user_id=user_id) if scope == 'user' else existing.filter(user__isnull=True)
        existing.update(status='superseded', superseded_at=timezone.now())
        new_version = _record_model_version(scope, user_id, status='active', **kwargs)
        new_version.activated_at = timezone.now()
        new_version.save(update_fields=['activated_at'])
    return new_version


# ---------------------------------------------------------------------------
# Retrain entrypoints
# ---------------------------------------------------------------------------

def retrain_win_model_for_scope(scope: str, user_id=None, min_samples=None) -> dict:
    """Fit, validate, and (if it clears the gate) activate a win-probability
    model for one scope. Never raises."""
    from core.services.quote_ml import WIN_ML_AVAILABLE, WinProbabilityModel

    if not WIN_ML_AVAILABLE:
        return {'trained': False, 'reason': 'sklearn/joblib not available'}
    if scope == 'user' and not user_id:
        return {'trained': False, 'reason': 'user scope requires a user_id'}

    min_samples = min_samples if min_samples is not None else _min_samples_for(scope)
    try:
        X, y, n, feature_names = build_win_training_matrix_for_scope(scope, user_id=user_id)
    except Exception as exc:
        logger.warning('win training matrix build failed (scope=%s, user=%s): %s', scope, user_id, exc)
        return {'trained': False, 'reason': f'feature build failed: {exc}'}

    if n < min_samples:
        return {'trained': False, 'reason': f'insufficient data ({n}/{min_samples})', 'samples': n}
    if len(set(y.tolist())) < 2:
        return {'trained': False, 'reason': 'only one outcome class present', 'samples': n}

    accepted_count = int(y.sum())
    rejected_count = n - accepted_count

    candidate_names = _candidates_for_size(n)
    bench = []
    try:
        bench = benchmark_candidates(X, y, candidate_names)
    except Exception as exc:
        logger.warning('benchmark_candidates failed (scope=%s, user=%s): %s', scope, user_id, exc)

    winner_name = bench[0]['name'] if bench else 'logistic_regression'
    winner_metrics = bench[0] if bench else {}

    try:
        final_model = _build_candidate(winner_name)
        final_model.fit(X, y)
    except Exception as exc:
        logger.warning('final fit failed (scope=%s, user=%s, algo=%s): %s', scope, user_id, winner_name, exc)
        return {'trained': False, 'reason': f'fit failed: {exc}', 'samples': n}

    # Round-trip sanity check: dump, reload, and predict on one training row
    # in the same process. A candidate that fails this never touches the
    # active model on disk — the previous artifact stays exactly as it was.
    try:
        import joblib
        import os as _os
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix='.pkl.check')
        _os.close(fd)
        try:
            joblib.dump(final_model, tmp)
            reloaded = joblib.load(tmp)
            check_proba = reloaded.predict_proba(X[:1])[0, 1]
            if not np.isfinite(check_proba):
                raise ValueError('non-finite probability from round-trip check')
        finally:
            if _os.path.exists(tmp):
                _os.unlink(tmp)
    except Exception as exc:
        logger.error('round-trip sanity check failed (scope=%s, user=%s): %s', scope, user_id, exc)
        _record_model_version(
            scope, user_id, status='failed', algorithm=winner_name, feature_names=feature_names,
            sample_count=n, accepted_count=accepted_count, rejected_count=rejected_count,
            evaluation_metrics=winner_metrics, hyperparameters={'candidates_considered': bench},
            rejection_reason=f'round-trip sanity check failed: {exc}',
        )
        return {'trained': False, 'reason': f'sanity check failed: {exc}', 'samples': n}

    # Regression gate — deliberately data-size-aware. Below the CV threshold
    # an 80/20-style holdout (or even k-fold) has too few rows for a metric
    # dip to mean anything; log it for observability but never block on it.
    # At/above threshold, block unless the new training set is substantially
    # larger than what's currently active (a bigger, more representative
    # dataset legitimately moving the decision boundary shouldn't be blocked
    # by comparison against an undertrained predecessor).
    previous = _current_active_version(scope, user_id)
    if n >= _cv_threshold() and previous is not None:
        prev_auc = (previous.evaluation_metrics or {}).get('roc_auc')
        new_auc = winner_metrics.get('roc_auc')
        substantially_bigger = n >= (previous.training_sample_count or 0) * 1.5
        if prev_auc is not None and new_auc is not None and new_auc < prev_auc - 0.03 and not substantially_bigger:
            _record_model_version(
                scope, user_id, status='rejected', algorithm=winner_name, feature_names=feature_names,
                sample_count=n, accepted_count=accepted_count, rejected_count=rejected_count,
                evaluation_metrics=winner_metrics, hyperparameters={'candidates_considered': bench},
                rejection_reason=f'roc_auc regressed {prev_auc:.3f} -> {new_auc:.3f} vs active model',
            )
            logger.warning('win model retrain REJECTED on regression gate (scope=%s, user=%s): %.3f -> %.3f',
                           scope, user_id, prev_auc, new_auc)
            return {'trained': False, 'reason': 'regression gate: metrics worse than active model', 'samples': n}
    elif n < _cv_threshold():
        logger.info('win model retrain (scope=%s, user=%s) below CV threshold (%s<%s) — '
                   'metrics logged, not gated: %s', scope, user_id, n, _cv_threshold(), winner_metrics)

    # Atomic activation: write the artifact to disk FIRST (existing atomic
    # tempfile+os.replace mechanism, unchanged) — only after that succeeds
    # does the DB bookkeeping flip. A crash mid-sequence leaves the DB row
    # briefly stale (self-heals next run) rather than ever claiming an active
    # model that isn't actually what's on disk.
    win = WinProbabilityModel(scope=scope, user_id=user_id)
    win.model = final_model
    win.metadata = {
        'trained_at': datetime.now().isoformat(),
        'training_count': int(n),
        'sample_count': int(n),
        'training_sample_count': int(n),
        'accuracy': winner_metrics.get('accuracy'),
        'auc': winner_metrics.get('roc_auc'),
        'brier': winner_metrics.get('brier'),
        'feature_names': feature_names,
        'feature_version': quote_features.FEATURE_VERSION,
        'algorithm': winner_name,
        'version': '2.0.0',
    }
    win._save_model()

    _activate_model_version(
        scope=scope, user_id=user_id, algorithm=winner_name, feature_names=feature_names,
        sample_count=n, accepted_count=accepted_count, rejected_count=rejected_count,
        evaluation_metrics=winner_metrics, hyperparameters={'candidates_considered': bench},
    )

    logger.info('Win model retrained (scope=%s, user=%s) on %s outcomes (algo=%s, auc=%s)',
               scope, user_id, n, winner_name, winner_metrics.get('roc_auc'))
    return {
        'trained': True, 'samples': n, 'algorithm': winner_name,
        'accuracy': winner_metrics.get('accuracy'), 'auc': winner_metrics.get('roc_auc'),
    }


def retrain_win_model(min_samples=None) -> dict:
    """Thin wrapper delegating to the global scope — keeps the existing
    Celery task name, Beat schedule entry, TaskRunLog tracking, and
    management command all working unchanged. The real, scope-aware
    implementation is retrain_win_model_for_scope()."""
    return retrain_win_model_for_scope('global', user_id=None, min_samples=min_samples)


def win_model_status(company=None) -> dict:
    """Honest snapshot of the GLOBAL win model for the quote UI/CLI. Never
    raises. Pass `company` to scope the displayed outcome count to one
    tenant; the global model's own metadata is always platform-wide.

    Superseded for API responses by core.services.win_prediction.
    model_progress(), which reports BOTH tiers — this narrower, single-tier
    function is kept for the `retrain_win_model` management command's
    startup message and any other pre-existing global-only caller.
    """
    from core.models import QuoteOutcome
    qs = QuoteOutcome.objects.filter(outcome__in=['accepted', 'rejected'])
    if company is not None:
        qs = qs.filter(company=company)
        if company.ai_training_started_at is not None:
            qs = qs.filter(created_at__gte=company.ai_training_started_at)
    outcomes = qs.count()
    min_needed = _min_samples_for('global')

    meta = {}
    try:
        from core.services.quote_ml import WIN_ML_AVAILABLE, WinProbabilityModel
        if WIN_ML_AVAILABLE:
            meta = getattr(WinProbabilityModel(scope='global'), 'metadata', {}) or {}
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


# NOTE: the global tier retrains on a nightly Celery Beat schedule
# (core.tasks.retrain_win_model, idempotent, no-ops below the sample
# threshold). Per-user tiers retrain event-driven + debounced
# (core.services.ml_training_queue.schedule_user_retrain, called from
# quote_outcome_capture.record_quote_outcome), with a nightly safety-net sweep
# (core.tasks.sweep_user_win_model_training) catching anything the event path
# missed. The old fire-and-forget daemon-thread retrain inside the web
# request was removed long before this two-tier design: it died with the
# worker, raced joblib.dump across gunicorn workers, and skipped retrains
# whenever count % 10 != 0 — the event-driven per-user path avoids the same
# mistakes via a real Celery task + DB-native debounce, not an in-process thread.
