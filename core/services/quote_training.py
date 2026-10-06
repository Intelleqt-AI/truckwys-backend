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
    setting_name = {'user': 'WIN_MODEL_USER_MIN_SAMPLES',
                    'company': 'WIN_MODEL_COMPANY_MIN_SAMPLES'}.get(scope, 'WIN_MODEL_GLOBAL_MIN_SAMPLES')
    return int(getattr(settings, setting_name, 40))


def _cv_threshold() -> int:
    return int(getattr(settings, 'WIN_MODEL_CV_THRESHOLD', 150))


# ---------------------------------------------------------------------------
# Training matrix
# ---------------------------------------------------------------------------

def closed_outcomes():
    """Every decided (accepted/rejected) QuoteOutcome the win model may learn
    from — the ONE definition of a "closed quote" for training, the nightly
    sweeps and every "N closed quotes" count shown to users (train and serve
    stay consistent).

    Quotes that were never sent to the customer (Quote.was_sent False: straight
    from DRAFT to won/lost) are excluded — they are not evidence of how a
    customer reacts to a price (r5 M1; the same rule quote_features and the
    lane benchmark already apply). was_sent NULL (older rows, unknown) still
    counts.
    """
    from core.models import QuoteOutcome
    return (QuoteOutcome.objects.filter(outcome__in=['accepted', 'rejected'])
            .exclude(quote__was_sent=False))


def build_win_training_matrix_for_scope(scope: str, user_id=None, company_id=None):
    """Return (X, y, n, feature_names) engineered from QuoteOutcome. Never raises.

    scope='global': every company's pooled outcomes (minus each company's own
    ai_training_started_at cutoff, unchanged from before) — same as the
    original single-tier design.
    scope='user': only outcomes this user (Quote.created_by, snapshotted onto
    QuoteOutcome.created_by at record time) has personally decided.
    scope='company': only this company's own decided quotes (pricing
    analysis' company tier, checked before the user tier).

    The global pool only takes outcomes from companies that opted in
    (Company.pool_pricing_data) — the same flag that lets a company be served
    the global model — so no tenant's outcomes shape another's likelihoods
    without consent.

    Prefers each row's feature_snapshot (frozen, versioned, at outcome-record
    time) when present and current-version; falls back to live reconstruction
    via quote_features.compute_features_for_quote() for legacy rows — itself
    leakage-safe (as_of pinned to the quote's own created_at), just not
    frozen against future changes to the feature-computation code the way a
    stored snapshot is.
    """
    qs = (
        closed_outcomes()
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
    elif scope == 'company':
        if not company_id:
            return np.empty((0, 0)), np.empty((0,)), 0, []
        qs = qs.filter(quote__company_id=company_id)
    else:
        qs = qs.filter(quote__company__pool_pricing_data=True)

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
        # class_weight='balanced': real quote data is heavily one-sided (the
        # first global model trained on 73 won against 14 lost). Unweighted,
        # the intercept alone reached +2.6 — a default answer of "93% likely
        # to win" before any feature was consulted — and the minority class
        # carried too little weight for price to matter. Balancing is what
        # lets the lost quotes actually shape the boundary.
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, random_state=42, C=1.0, class_weight='balanced'),
        )
    if name == 'gradient_boosting':
        from sklearn.ensemble import GradientBoostingClassifier
        return make_pipeline(StandardScaler(), GradientBoostingClassifier(random_state=42))
    if name == 'lightgbm':
        import lightgbm as lgb
        return lgb.LGBMClassifier(random_state=42, verbosity=-1)
    raise ValueError(f'unknown candidate algorithm: {name}')


def _price_sensitivity(model, X, feature_names, *, multipliers=(0.9, 1.0, 1.1, 1.2), probe_rows=25):
    """How much predicted win probability falls as price rises, averaged over
    real training rows. Positive = behaves like a market (dearer loses more
    often); <= 0 = the model has learned price backwards or not at all.

    Each probe holds one actual row fixed and moves only price_ratio, which is
    exactly what the quote builder does when an operator drags the price, so
    this measures the one behaviour the product actually depends on.

    The sweep is multiplicative around each row's OWN price_ratio rather than
    over fixed absolute values. An earlier absolute 0.85->1.30 sweep measured
    almost nothing on a company that habitually quotes at 40-75% of the
    benchmark rate: every probe point landed outside the observed range, where
    the model has already saturated, so a model that was in fact correctly
    ordered on 100% of rows scored 0.013. Relative probing asks the question
    the operator asks — "what if I moved this quote's price" — at whatever
    price level that operator actually works.

    Returns (sensitivity, fraction_of_rows_ordered_correctly).
    """
    if 'price_ratio' not in feature_names:
        return 0.0, 0.0
    idx = feature_names.index('price_ratio')
    avail_idx = feature_names.index('price_ratio_available') if 'price_ratio_available' in feature_names else None

    # Evenly spaced across the whole training set rather than the first rows
    # only (which are whatever order the query returned them in), so one
    # period's or one lane's rows can't stand in for the model's behaviour.
    if len(X) > probe_rows:
        rows = X[np.linspace(0, len(X) - 1, probe_rows).round().astype(int)]
    else:
        rows = X
    drops, ordered = [], 0
    for row in rows:
        own = float(row[idx])
        if own <= 0:
            continue
        probe = np.repeat(row.reshape(1, -1), len(multipliers), axis=0)
        probe[:, idx] = [own * m for m in multipliers]
        if avail_idx is not None:
            # A sweep is only meaningful where the ratio is a real measurement.
            probe[:, avail_idx] = 1.0
        try:
            p = model.predict_proba(probe)[:, 1]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning('price sensitivity probe failed: %s', exc)
            return 0.0, 0.0
        drops.append(float(p[0] - p[-1]))
        if all(p[i] >= p[i + 1] for i in range(len(p) - 1)):
            ordered += 1
    if not drops:
        return 0.0, 0.0
    return float(np.mean(drops)), ordered / len(drops)


def _price_ratio_range(X, feature_names):
    """[lo, hi] price_ratio over the training rows that had a real market
    reference, or None. Never raises."""
    try:
        if 'price_ratio' not in feature_names or not len(X):
            return None
        col = X[:, feature_names.index('price_ratio')]
        if 'price_ratio_available' in feature_names:
            col = col[X[:, feature_names.index('price_ratio_available')] > 0.5]
        col = col[col > 0]
        if len(col) < 5:
            return None
        return [round(float(np.percentile(col, 2.5)), 4), round(float(np.percentile(col, 97.5)), 4)]
    except Exception:
        return None


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

def _scope_filter(qs, scope, user_id, company_id=None):
    """Narrow an MLModelVersion queryset to exactly one tier's rows."""
    if scope == 'user':
        return qs.filter(user_id=user_id)
    if scope == 'company':
        return qs.filter(company_id=company_id)
    return qs.filter(user__isnull=True, company__isnull=True)


def _current_active_version(scope, user_id, company_id=None):
    from core.models import MLModelVersion
    qs = MLModelVersion.objects.filter(scope=scope, status='active')
    qs = _scope_filter(qs, scope, user_id, company_id)
    return qs.order_by('-created_at').first()


def _record_model_version(scope, user_id, *, status, algorithm='', feature_names=None,
                           sample_count=0, accepted_count=0, rejected_count=0,
                           evaluation_metrics=None, hyperparameters=None, rejection_reason='',
                           company_id=None):
    from django.utils import timezone
    from core.models import MLModelVersion
    owner = user_id if scope == 'user' else company_id if scope == 'company' else None
    return MLModelVersion.objects.create(
        scope=scope, user_id=(user_id if scope == 'user' else None),
        company_id=(company_id if scope == 'company' else None), status=status,
        algorithm=algorithm, feature_version=quote_features.FEATURE_VERSION,
        # Compact stamp, not isoformat(): the field is varchar(40) and
        # "global:-:2026-09-15T13:06:47.716726+00:00" is 41 characters, so
        # every activation died on Postgres with StringDataRightTruncation
        # AFTER the artifact was already on disk — a trained, serving model
        # with no bookkeeping row. SQLite doesn't enforce varchar length, so
        # local runs and the test suite never saw it. This also matches the
        # field's own documented shape ("user:123:v7, global:v42").
        feature_names=feature_names or [],
        model_version=f'{scope}:{owner or "-"}:{timezone.now():%Y%m%dT%H%M%S}',
        training_sample_count=sample_count, accepted_count=accepted_count, rejected_count=rejected_count,
        evaluation_metrics=evaluation_metrics or {}, hyperparameters=hyperparameters or {},
        rejection_reason=rejection_reason, trained_at=timezone.now(),
    )


def _activate_model_version(scope, user_id, company_id=None, **kwargs):
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
        existing = _scope_filter(existing, scope, user_id, company_id)
        existing.update(status='superseded', superseded_at=timezone.now())
        new_version = _record_model_version(scope, user_id, status='active', company_id=company_id, **kwargs)
        new_version.activated_at = timezone.now()
        new_version.save(update_fields=['activated_at'])
    return new_version


# ---------------------------------------------------------------------------
# Retrain entrypoints
# ---------------------------------------------------------------------------

def retrain_win_model_for_scope(scope: str, user_id=None, min_samples=None, company_id=None) -> dict:
    """Fit, validate, and (if it clears the gate) activate a win-probability
    model for one scope ('user' | 'company' | 'global'). Never raises."""
    from core.services.quote_ml import WIN_ML_AVAILABLE, WinProbabilityModel

    if not WIN_ML_AVAILABLE:
        return {'trained': False, 'reason': 'sklearn/joblib not available'}
    if scope == 'user' and not user_id:
        return {'trained': False, 'reason': 'user scope requires a user_id'}
    if scope == 'company' and not company_id:
        return {'trained': False, 'reason': 'company scope requires a company_id'}
    # Every bookkeeping call below takes the tier's owner the same way.
    owner = {'company_id': company_id} if scope == 'company' else {}

    min_samples = min_samples if min_samples is not None else _min_samples_for(scope)
    try:
        X, y, n, feature_names = build_win_training_matrix_for_scope(scope, user_id=user_id, company_id=company_id)
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
            scope, user_id, **owner, status='failed', algorithm=winner_name, feature_names=feature_names,
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
    previous = _current_active_version(scope, user_id, company_id)
    if n >= _cv_threshold() and previous is not None:
        prev_auc = (previous.evaluation_metrics or {}).get('roc_auc')
        new_auc = winner_metrics.get('roc_auc')
        substantially_bigger = n >= (previous.training_sample_count or 0) * 1.5
        if prev_auc is not None and new_auc is not None and new_auc < prev_auc - 0.03 and not substantially_bigger:
            _record_model_version(
                scope, user_id, **owner, status='rejected', algorithm=winner_name, feature_names=feature_names,
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

    # Price-sensitivity gate. Deliberately BEFORE the disk write below: the
    # artifact file is what predictions resolve against, so a model that
    # reaches disk is live regardless of any DB bookkeeping. Failing here
    # leaves the previous artifact (or no artifact, and therefore the
    # heuristic) exactly as it was.
    #
    # This is the gate the AUC gate cannot be. A model can rank outcomes
    # better than chance while being flat or inverted in price, and that is
    # not a subtle defect: the margin optimizer searches for the price where
    # expected profit peaks, so a win curve that does not fall with price has
    # no interior peak and the search walks to the top of the allowed band.
    # In production that produced a recommendation 62% above the operator's
    # own quote, labelled 96% likely to win.
    min_sensitivity = float(getattr(settings, 'WIN_MODEL_MIN_PRICE_SENSITIVITY', 0.05))
    sensitivity, ordered_frac = _price_sensitivity(final_model, X, feature_names)
    if sensitivity < min_sensitivity or ordered_frac < 0.5:
        reason = (
            f'price sensitivity gate: win probability falls only {sensitivity:+.4f} '
            f'when price is raised 0.9x->1.2x (need >= {min_sensitivity}), '
            f'monotonic on {ordered_frac:.0%} of probe rows (need >= 50%)'
        )
        _record_model_version(
            scope, user_id, **owner, status='rejected', algorithm=winner_name, feature_names=feature_names,
            sample_count=n, accepted_count=accepted_count, rejected_count=rejected_count,
            evaluation_metrics={**winner_metrics, 'price_sensitivity': round(sensitivity, 4),
                                'price_monotonic_fraction': round(ordered_frac, 4)},
            hyperparameters={'candidates_considered': bench},
            rejection_reason=reason,
        )
        logger.warning('win model retrain REJECTED on price sensitivity (scope=%s, user=%s): %s',
                       scope, user_id, reason)
        return {'trained': False, 'reason': reason, 'samples': n,
                'price_sensitivity': round(sensitivity, 4)}

    # Atomic activation: write the artifact to disk FIRST (existing atomic
    # tempfile+os.replace mechanism, unchanged) — only after that succeeds
    # does the DB bookkeeping flip. A crash mid-sequence leaves the DB row
    # briefly stale (self-heals next run) rather than ever claiming an active
    # model that isn't actually what's on disk.
    win = WinProbabilityModel(scope=scope, user_id=user_id, company_id=company_id)
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
        # Price ratios this model actually saw (2.5th-97.5th percentile of
        # rows with a real market reference) — the pricing analysis only
        # shows a model % for prices inside it.
        'price_ratio_range': _price_ratio_range(X, feature_names),
    }
    win._save_model()

    _activate_model_version(
        scope=scope, user_id=user_id, **owner, algorithm=winner_name, feature_names=feature_names,
        sample_count=n, accepted_count=accepted_count, rejected_count=rejected_count,
        # Stored alongside AUC so the admin panel can show whether the live
        # model actually prices, not just whether it ranks.
        evaluation_metrics={**winner_metrics, 'price_sensitivity': round(sensitivity, 4),
                            'price_monotonic_fraction': round(ordered_frac, 4)},
        hyperparameters={'candidates_considered': bench},
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


def retrain_company_win_models(min_growth: int = 5) -> dict:
    """(Re)train the per-company tier for every company that qualifies:
    >= WIN_MODEL_COMPANY_MIN_SAMPLES decided outcomes (both classes are
    checked by retrain_win_model_for_scope itself). Skips a company whose
    decided-outcome count grew by fewer than `min_growth` since its last
    training attempt, so the nightly run is not a blind refit of everyone.
    Never raises. Returns {'considered', 'trained', 'skipped', 'results'}."""
    from django.db.models import Count
    from core.models import MLModelVersion

    min_samples = _min_samples_for('company')
    summary = {'considered': 0, 'trained': 0, 'skipped': 0, 'results': {}}
    try:
        counts = (
            closed_outcomes().filter(quote__company__isnull=False)
            .filter(
                Q(quote__company__ai_training_started_at__isnull=True)
                | Q(created_at__gte=F('quote__company__ai_training_started_at'))
            )
            .values('quote__company_id').annotate(n=Count('id'))
        )
        rows = [(r['quote__company_id'], r['n']) for r in counts]
    except Exception as exc:
        logger.warning('retrain_company_win_models: count failed: %s', exc)
        return summary
    for company_id, n in rows:
        if n < min_samples:
            continue
        summary['considered'] += 1
        latest = (MLModelVersion.objects.filter(scope='company', company_id=company_id)
                  .order_by('-created_at').first())
        if latest is not None and n < (latest.training_sample_count or 0) + min_growth:
            summary['skipped'] += 1
            continue
        result = retrain_win_model_for_scope('company', company_id=company_id)
        summary['results'][company_id] = result
        if result.get('trained'):
            summary['trained'] += 1
    return summary


def win_model_status(company=None) -> dict:
    """Honest snapshot of the GLOBAL win model for the quote UI/CLI. Never
    raises. Pass `company` to scope the displayed outcome count to one
    tenant; the global model's own metadata is always platform-wide.

    Superseded for API responses by core.services.win_prediction.
    model_progress(), which reports BOTH tiers — this narrower, single-tier
    function is kept for the `retrain_win_model` management command's
    startup message and any other pre-existing global-only caller.
    """
    qs = closed_outcomes()
    if company is not None:
        qs = qs.filter(company=company)
        if company.ai_training_started_at is not None:
            qs = qs.filter(created_at__gte=company.ai_training_started_at)
    else:
        # Platform-wide: only what the global model can actually train on —
        # outcomes from companies that opted into pooling.
        qs = qs.filter(quote__company__pool_pricing_data=True)
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
