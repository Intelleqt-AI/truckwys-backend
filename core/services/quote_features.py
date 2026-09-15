"""Feature engineering v2 for the quote win-probability model — the single
source of truth for every historical aggregate used at training time,
outcome-snapshot time, and live-serving time alike.

Before this module, three call sites computed overlapping-but-not-identical
versions of the same signals and drifted independently: quote_outcome_capture
(leave-one-out only), quote_training's build_win_training_matrix (its own
reconstruction), and views_ai_quote's _derive_client_features (yet another
copy). This module replaces all three.

Core correctness rule: every historical aggregate filters `created_at__lte=as_of`
using the QUOTE's own created_at (never "now", never the outcome's decided-at
timestamp). The previous implementation only excluded a row from its own
aggregate (leave-one-out) — that's necessary but not sufficient: it doesn't
stop a LATER quote's outcome from leaking into an EARLIER quote's
reconstructed training features. Passing `as_of=quote.created_at` (plus
`exclude_quote_id=quote.id` for the leave-one-out case) closes that gap.
`<=` rather than strict `<`: on a coarse system clock, a row created just
before `as_of` is captured can share its exact timestamp, and strict `<`
would wrongly exclude a genuinely-prior row (confirmed on Windows: two Quote
rows created microseconds apart collapsed to the same stored timestamp under
test load). This is safe because the one row a given computation must never
see — the quote/outcome it's computing a feature FOR — is always excluded
separately, by id (`exclude_quote_id`), not by this timestamp cutoff.

Never raises: every public function degrades to a safe default rather than
breaking quote creation, training, or outcome capture.
"""
import logging
import math

from django.utils import timezone

logger = logging.getLogger(__name__)

# Bump whenever the feature set changes, so snapshots written under an older
# schema are recomputed instead of being fed to a model expecting the new one.
# v3 added price_ratio_available.
# v4 moved the four cyclical calendar features out of CORE (see below), which
# also retires every v3 artifact on schema mismatch — the intended way to
# withdraw a model, since the file on disk is what predictions resolve against.
FEATURE_VERSION = 'v4'

# Vehicle-type bucketing is deliberately a small, fixed vocabulary rather than
# one-hot-ing the raw free-text VehicleType.name (company-defined, unbounded
# cardinality) — matching, e.g., client_tier's existing dict-based encoding.
VEHICLE_TYPE_BUCKETS = [
    ('interlink', ('interlink',)),
    ('superlink', ('superlink',)),
    ('tautliner', ('tautliner', 'curtain')),
    ('refrigerated', ('reefer', 'refrig', 'fridge')),
    ('tanker', ('tanker',)),
    ('flatbed', ('flatbed', 'flat deck', 'flat-deck')),
    ('tipper', ('tipper', 'dropside', 'drop-side', 'drop side')),
]
VEHICLE_TYPE_BUCKET_NAMES = [name for name, _kw in VEHICLE_TYPE_BUCKETS] + ['other']
_VEHICLE_TYPE_BUCKET_FEATURES = [f'vehicle_type_is_{name}' for name in VEHICLE_TYPE_BUCKET_NAMES]

# CORE: used whenever the training scope has fewer rows than
# settings.WIN_MODEL_CV_THRESHOLD — essentially every per-user model at the
# 40-sample floor. Kept low-dimensional on purpose: a tiny dataset overfits a
# wide feature set long before it overfits a narrow one.
CORE_FEATURES = [
    'price_ratio',
    # Whether price_ratio is a real measurement or the 1.0 filler. Paired with
    # price_ratio on purpose and in CORE, not FULL: the filler dominated ~64%
    # of rows in production, so a model that cannot tell the two apart is
    # learning from a constant. Cheap — one binary column.
    'price_ratio_available',
    'quoted_margin_pct',
    'client_tier',
    'historical_acceptance_rate',
    'user_quote_volume_prior',
    'user_historical_win_rate',
    'days_until_departure',
    'route_popularity',
    'distance_km',
]

# FULL: used once a scope has enough data (global model, eventually a heavy
# per-user model) to support more dimensions without overfitting.
FULL_FEATURES = CORE_FEATURES + [
    # Seasonality only once there is enough data to separate it from noise.
    # At the 40-sample floor these four were the opposite: in the first
    # per-user model ever trained (53 rows) month_sin (+0.88) and dow_sin
    # (-0.81) came out among the largest coefficients in the whole model,
    # outweighing price. Four cyclical columns give a tiny dataset four
    # convincing ways to memorise which weeks happened to close.
    'month_sin', 'month_cos',
    'dow_sin', 'dow_cos',
    'cost_to_market_ratio',
    'weight_kg',
    'sla_hours',
    'is_round_trip',
    'customer_relationship_days_at_quote',
    'customer_quote_volume_prior',
    'user_avg_price_ratio_prior',
    'lane_historical_acceptance_rate',
] + _VEHICLE_TYPE_BUCKET_FEATURES


def feature_tier_for(sample_count: int) -> list:
    """CORE_FEATURES below settings.WIN_MODEL_CV_THRESHOLD, else FULL_FEATURES."""
    from django.conf import settings
    threshold = int(getattr(settings, 'WIN_MODEL_CV_THRESHOLD', 150))
    return CORE_FEATURES if sample_count < threshold else FULL_FEATURES


def vectorize(features: dict, feature_names) -> list:
    """Order a {name: value} feature dict into the array a specific model's
    OWN stored feature_names expects — never a hardcoded global order. This is
    what lets a CORE-trained 40-sample user model and a FULL-trained
    5,000-sample global model coexist safely, and is why a schema-mismatched
    model is treated as unusable (see quote_ml.WinProbabilityModel) rather
    than silently fed a misaligned vector."""
    return [float(features.get(name) or 0.0) for name in feature_names]


def _cyclical(value, period):
    angle = 2 * math.pi * (float(value or 0) % period) / period
    return math.sin(angle), math.cos(angle)


def _tier_from_accepted_count(accepted: int) -> int:
    if accepted >= 10:
        return 2  # vip
    if accepted >= 3:
        return 1  # regular
    return 0  # new


def _vehicle_type_bucket(vehicle_type) -> str:
    v = (vehicle_type or '').strip().lower()
    for name, keywords in VEHICLE_TYPE_BUCKETS:
        if any(k in v for k in keywords):
            return name
    return 'other'


def customer_signals(company, customer_id, as_of, exclude_quote_id=None):
    """(client_tier: int 0/1/2, historical_acceptance_rate: float,
    customer_quote_volume_prior: int, customer_relationship_days_at_quote:
    float|None) — every number computed strictly from decided quotes with
    created_at < as_of. Cold start (no customer, or no prior history) ->
    (0, 0.5, 0, None), matching the existing serving-time default."""
    if not customer_id:
        return 0, 0.5, 0, None
    try:
        from core.models import Quote, Customer

        # __lte not __lt: a coarse system clock can give a just-created row the
        # exact same timestamp as `as_of`; safe to include since the row this
        # is computed FOR is always excluded separately, by id, below.
        qs = Quote.objects.filter(company=company, customer_id=customer_id, created_at__lte=as_of)
        if exclude_quote_id:
            qs = qs.exclude(id=exclude_quote_id)
        decided = qs.filter(outcome__in=['accepted', 'rejected'])
        total = decided.count()
        accepted = decided.filter(outcome='accepted').count()
        rate = (accepted / total) if total else 0.5
        tier = _tier_from_accepted_count(accepted)

        rel_days = None
        customer = Customer.objects.filter(id=customer_id).only('created_at').first()
        if customer and customer.created_at:
            ref_date = as_of.date() if hasattr(as_of, 'date') else as_of
            rel_days = max(0, (ref_date - customer.created_at.date()).days)
        return tier, rate, total, rel_days
    except Exception as exc:
        logger.warning('customer_signals failed: %s', exc)
        return 0, 0.5, 0, None


def user_signals(user_id, as_of, exclude_quote_id=None):
    """(user_quote_volume_prior: int, user_historical_win_rate: float,
    user_avg_price_ratio_prior: float|None) for the QUOTING user ("User A"),
    computed strictly before as_of. No user (public/legacy flow) -> (0, 0.5, None)."""
    if not user_id:
        return 0, 0.5, None
    try:
        from core.models import Quote, QuoteOutcome

        qs = Quote.objects.filter(created_by_id=user_id, created_at__lte=as_of)
        if exclude_quote_id:
            qs = qs.exclude(id=exclude_quote_id)
        decided = qs.filter(outcome__in=['accepted', 'rejected'])
        total = decided.count()
        accepted = decided.filter(outcome='accepted').count()
        rate = (accepted / total) if total else 0.5

        ratio_qs = QuoteOutcome.objects.filter(
            created_by_id=user_id, created_at__lte=as_of, price_ratio__isnull=False,
        )
        if exclude_quote_id:
            ratio_qs = ratio_qs.exclude(quote_id=exclude_quote_id)
        ratios = [float(v) for v in ratio_qs.values_list('price_ratio', flat=True)]
        avg_ratio = (sum(ratios) / len(ratios)) if ratios else None
        return total, rate, avg_ratio
    except Exception as exc:
        logger.warning('user_signals failed: %s', exc)
        return 0, 0.5, None


def lane_historical_acceptance_rate(company, origin, destination, as_of, exclude_quote_id=None):
    """This company's win rate on this canonical lane, strictly before as_of.
    Returns 0.5 (neutral) when there's no usable history — distinct from
    route_popularity, which measures VOLUME, not outcome quality."""
    if not origin or not destination:
        return 0.5
    try:
        from core.models import Quote
        from core.services.lane_benchmark import _lane_q

        qs = Quote.objects.filter(
            _lane_q('origin', origin), _lane_q('destination', destination),
            company=company, created_at__lte=as_of,
            outcome__in=['accepted', 'rejected'],
        )
        if exclude_quote_id:
            qs = qs.exclude(id=exclude_quote_id)
        total = qs.count()
        if not total:
            return 0.5
        accepted = qs.filter(outcome='accepted').count()
        return accepted / total
    except Exception as exc:
        logger.warning('lane_historical_acceptance_rate failed: %s', exc)
        return 0.5


def compute_features(
    *, company, customer_id=None, created_by_user_id=None,
    origin=None, destination=None, vehicle_type=None,
    total_amount=0, base_rate=0, fuel_surcharge=0, toll_charges=0,
    driver_allowance=0, additional_charges=0,
    weight_kg=None, sla_hours=None, is_round_trip=False, distance_km=None,
    pickup_date=None, quote_created_at=None, market_rate=None,
    as_of=None, exclude_quote_id=None,
) -> dict:
    """The single, leakage-safe feature computation used at training,
    outcome-snapshot, and live-serving time alike.

    Takes primitives rather than a persisted Quote, because at live-serving
    time (the /quotes/analyze/ endpoint) there IS no Quote row yet — only a
    request payload. Use compute_features_for_quote() below when you do have
    a persisted Quote/QuoteOutcome to reconstruct from.

    `as_of` bounds every historical lookup (defaults to quote_created_at, else
    now) — pass the quote's own created_at explicitly when reconstructing a
    historical training row so nothing that happened after it can leak in.
    Never raises — every sub-computation already degrades safely on its own.
    """
    now = timezone.now()
    ref_time = quote_created_at or now
    as_of = as_of or ref_time

    total_amount = float(total_amount or 0)
    direct_cost = (
        float(base_rate or 0) + float(fuel_surcharge or 0) + float(toll_charges or 0)
        + float(driver_allowance or 0) + float(additional_charges or 0)
    )
    # Deliberately NOT QuoteOutcome.margin_pct (that snapshot can reflect a
    # negotiated final_price override, decided AFTER this quote was priced —
    # circular as a feature). Also deliberately includes base_rate as cost,
    # matching QuoteBuilder.tsx's directCost definition ("every real cost
    # component the carrier must recover, base rate included") rather than
    # quote_outcome_capture's narrower direct_cost (which excludes base_rate)
    # — the frontend's is the more complete, and more recently fixed, concept.
    quoted_margin_pct = ((total_amount - direct_cost) / total_amount * 100.0) if total_amount > 0 else 0.0

    if market_rate is None:
        try:
            from core.services.lane_benchmark import resolve_market_rate
            market_rate, _source = resolve_market_rate(
                origin, destination, vehicle_type, company=company,
                exclude_quote_id=exclude_quote_id, as_of=as_of,
            )
        except Exception as exc:
            logger.warning('compute_features: market rate resolve failed: %s', exc)
            market_rate = None
    market_rate = float(market_rate) if market_rate else None
    # A missing market rate used to become price_ratio = 1.0, i.e. "priced
    # exactly at market". That is a claim, not a neutral value: it was true for
    # ~64% of production rows, which flattened the variance out of the single
    # most predictive CORE feature while looking perfectly valid on inspection.
    # Now the absence is its own feature — price_ratio_available — so the model
    # can learn "we had no market reference for this lane" instead of being
    # told a fiction. price_ratio keeps 1.0 as its filler so the two features
    # stay independent (vectorize() zero-fills anything absent), and
    # price_ratio_available is what tells the model whether to trust it.
    market_rate_available = bool(market_rate and market_rate > 0)
    price_ratio = (total_amount / market_rate) if market_rate_available else 1.0
    cost_to_market_ratio = (direct_cost / market_rate) if market_rate_available else price_ratio

    tier, hist_rate, cust_volume, rel_days = customer_signals(
        company, customer_id, as_of, exclude_quote_id=exclude_quote_id)
    user_volume, user_rate, user_avg_ratio = user_signals(
        created_by_user_id, as_of, exclude_quote_id=exclude_quote_id)
    lane_rate = lane_historical_acceptance_rate(
        company, origin, destination, as_of, exclude_quote_id=exclude_quote_id)

    try:
        from core.services.margin_optimizer import _route_popularity
        popularity = _route_popularity(origin, destination, as_of=as_of)
    except Exception as exc:
        logger.warning('compute_features: route popularity failed: %s', exc)
        popularity = 0.5

    days_until_departure = 7
    if pickup_date and ref_time:
        ref_date = ref_time.date() if hasattr(ref_time, 'date') else ref_time
        days_until_departure = max(0, (pickup_date - ref_date).days)

    month_sin, month_cos = _cyclical(ref_time.month, 12)
    dow_sin, dow_cos = _cyclical(ref_time.weekday(), 7)

    features = {
        'price_ratio': price_ratio,
        'price_ratio_available': 1.0 if market_rate_available else 0.0,
        'quoted_margin_pct': quoted_margin_pct,
        'client_tier': tier,
        'historical_acceptance_rate': hist_rate,
        'user_quote_volume_prior': user_volume,
        'user_historical_win_rate': user_rate,
        'days_until_departure': days_until_departure,
        'month_sin': month_sin, 'month_cos': month_cos,
        'dow_sin': dow_sin, 'dow_cos': dow_cos,
        'route_popularity': popularity,
        'distance_km': float(distance_km) if distance_km else 0.0,
        'cost_to_market_ratio': cost_to_market_ratio,
        'weight_kg': float(weight_kg) if weight_kg else 0.0,
        'sla_hours': float(sla_hours) if sla_hours else 0.0,
        'is_round_trip': 1.0 if is_round_trip else 0.0,
        'customer_relationship_days_at_quote': float(rel_days) if rel_days is not None else 0.0,
        'customer_quote_volume_prior': cust_volume,
        'user_avg_price_ratio_prior': user_avg_ratio if user_avg_ratio is not None else price_ratio,
        'lane_historical_acceptance_rate': lane_rate,
    }
    bucket = _vehicle_type_bucket(vehicle_type)
    for name in VEHICLE_TYPE_BUCKET_NAMES:
        features[f'vehicle_type_is_{name}'] = 1.0 if name == bucket else 0.0
    return features


def compute_features_for_quote(quote, as_of=None, exclude_quote_id=None) -> dict:
    """Convenience wrapper for training/snapshot use: pulls primitives off a
    persisted Quote instance and delegates to compute_features(). Defaults
    as_of/exclude_quote_id to the quote itself (leakage-safe reconstruction)."""
    return compute_features(
        company=quote.company, customer_id=quote.customer_id,
        created_by_user_id=quote.created_by_id,
        origin=quote.origin, destination=quote.destination, vehicle_type=quote.vehicle_type,
        total_amount=quote.total_amount, base_rate=quote.base_rate,
        fuel_surcharge=quote.fuel_surcharge, toll_charges=quote.toll_charges,
        driver_allowance=quote.driver_allowance, additional_charges=quote.additional_charges,
        weight_kg=quote.weight, sla_hours=quote.sla_hours,
        is_round_trip=(quote.trip_type == 'ROUND_TRIP'), distance_km=quote.distance,
        pickup_date=quote.pickup_date, quote_created_at=quote.created_at,
        as_of=as_of or quote.created_at,
        exclude_quote_id=quote.id if exclude_quote_id is None else exclude_quote_id,
    )
