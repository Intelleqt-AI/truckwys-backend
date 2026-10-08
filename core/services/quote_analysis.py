"""Comprehensive AI quote analysis.

Synthesises the cost / fuel / profit / market pieces the New Quote flow already
has into one response, plus an LLM narrative + suggested-price rationale. This is
pure synthesis over existing services — it never raises: every section is wrapped
and degrades to a safe default so the API stays stable.

LLM narrative uses the project's configured provider via core.services.agent
(OpenAI / gpt-4o in this deployment); with no key it falls back to a rule-based
summary.
"""
import logging
import re

from django.utils import timezone

logger = logging.getLogger(__name__)


def _fr(v, dp=0):
    """SA rand format ('R 24 000', 'R 32,80')."""
    from core.services.quote_costing import fmt_rand
    return fmt_rand(v, dp)


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Cost correctness / revenue guard — shared by RevenueGuardView and analyze_quote
# ---------------------------------------------------------------------------
# Trip-linked expense categories that make up the FIXED cost per km in the
# pricing analysis floor. Fuel, tolls and subcontractor costs are excluded:
# fuel and tolls are their own floor lines (the builder's figures), and a
# subcontracted load isn't run on the fleet's own trucks.
FIXED_COST_CATEGORIES = ('MAINTENANCE', 'INSURANCE', 'OVERHEAD', 'OTHER', 'DRIVER_COST')
FLEET_CPK_MIN_TRIPS = 10


def fleet_cost_per_km(company, categories=None, *, net_of_vat=False, exclude_rejected=False,
                      use_cache=True):
    """This company's cost per km from completed trips' expenses over the last
    12 months. Generalises the Revenue Guard's fleet average.

    `categories` (None = every category) picks which expenses count; the km
    denominator is always every COSTED trip (a completed trip with distance
    and at least one expense in the window), so a category logged on a few
    trips is spread over the whole fleet's distance, not just those trips.
    Returns {'value': float|None, 'trips': int, 'km': float} — value is None
    with fewer than FLEET_CPK_MIN_TRIPS costed trips. Cached 1 h. Never raises.
    """
    empty = {'value': None, 'trips': 0, 'km': 0.0}
    if company is None or not getattr(company, 'id', None):
        return empty
    from django.core.cache import cache
    key_parts = ['all' if categories is None else '-'.join(sorted(categories)),
                 'net' if net_of_vat else 'gross', 'norej' if exclude_rejected else 'all']
    cache_key = f'fleet_cpk_{company.id}_{"_".join(key_parts)}'
    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    out = dict(empty)
    try:
        from datetime import timedelta
        from django.db.models import F, Sum
        from core.models import Expense, Trip

        year_ago = timezone.now().date() - timedelta(days=365)
        exp = Expense.objects.filter(
            company=company, trip__isnull=False, trip__status='COMPLETED',
            trip__distance_km__gt=0, expense_date__gte=year_ago,
        )
        if exclude_rejected:
            exp = exp.exclude(status='REJECTED')
        trip_ids = list(exp.values_list('trip_id', flat=True).distinct())
        out['trips'] = len(trip_ids)
        total_km = float(Trip.objects.filter(id__in=trip_ids).aggregate(s=Sum('distance_km'))['s'] or 0)
        out['km'] = round(total_km, 1)
        if len(trip_ids) >= FLEET_CPK_MIN_TRIPS and total_km > 0:
            counted = exp if categories is None else exp.filter(category__in=list(categories))
            if net_of_vat:
                total_cost = float(counted.aggregate(s=Sum(F('amount') - F('vat_amount')))['s'] or 0)
            else:
                total_cost = float(counted.aggregate(s=Sum('amount'))['s'] or 0)
            if total_cost > 0:
                out['value'] = round(total_cost / total_km, 2)
    except Exception as exc:
        logger.warning('fleet cost-per-km aggregate failed: %s', exc)
    cache.set(cache_key, out, 3600)
    return out


def _fleet_avg_cpk(company):
    """This company's real cost-per-km from completed trips' expenses over the
    last 12 months (cached 1h). Falls back to the industry default when there
    are fewer than 10 costed trips: then None (no invented "fleet average").
    Never raises. (Every category, gross; see fleet_cost_per_km.)"""
    if company is None or not getattr(company, 'id', None):
        return None
    from django.core.cache import cache
    cache_key = f'fleet_avg_cpk_{company.id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = fleet_cost_per_km(company, None, use_cache=False)
    cpk = result['value'] or None
    if cpk is not None:
        cache.set(cache_key, cpk, 3600)
    return cpk


def _full_floor_fields(total_cost, quote_price, distance_km, company, vehicle_type=None, is_full_floor=False):
    """Additive Revenue Guard fields on the ONE margin definition the pricing
    analysis uses (core.services.pricing_analysis.margin_against_floor):
    margin = price − full cost floor, where the floor adds fixed cost/km × km
    to the direct costs the caller sent. The guard's original margin_pct /
    margin_floor keep their meaning (direct-cost margin) for existing clients.
    Never raises."""
    try:
        from core.services.pricing_analysis import fixed_cost_per_km, margin_against_floor
        # total_cost that is already THE floor (quote_costing) includes the
        # operating cost: never add it twice.
        fixed = fixed_cost_per_km(company, None, vehicle_type) if distance_km > 0 and not is_full_floor else None
        # To the cent (as quote_costing): never whole-rand rounded.
        from core.services.quote_costing import cents
        fixed_zar = cents(fixed['value'] * distance_km) if fixed else 0.0
        floor = cents(float(total_cost) + fixed_zar)
        m = margin_against_floor(quote_price, floor)
        return {
            'full_cost_floor': floor,
            'fixed_cost_per_km': fixed['value'] if fixed else None,
            'fixed_cost_source': fixed['source'] if fixed else None,
            'margin_vs_floor': m['margin'],
            'margin_floor_pct': m['margin_pct'],
        }
    except Exception as exc:
        logger.warning('revenue guard: full floor failed: %s', exc)
        return {}


def assess_revenue_guard(*, total_cost, quote_price, distance_km=0.0,
                         fuel_cost=0.0, company=None, quote=None, customer=None, vehicle_type=None,
                         is_full_floor=False):
    """Assess margin health for a quote.

    total_cost = direct operating cost; quote_price = price being charged.
    `company` supplies margin thresholds (falls back to sane defaults); `quote`
    (a saved Quote) enables the fuel-delta analysis; `customer` (or
    quote.customer) enables the payment-history check at quote-creation time.
    Returns a dict (always includes success + the display fields the frontend
    already consumes). Never raises.
    """
    total_cost = _f(total_cost)
    quote_price = _f(quote_price)
    distance_km = _f(distance_km)
    fuel_cost = _f(fuel_cost)

    if total_cost <= 0 or quote_price <= 0:
        return {'success': False, 'error': 'total_cost and quote_price must be > 0'}

    margin_pct = (quote_price - total_cost) / quote_price * 100

    at_risk_threshold = _f(getattr(company, 'margin_at_risk_pct', None), 5.0) or 5.0
    caution_threshold = _f(getattr(company, 'margin_caution_pct', None), 12.0) or 12.0
    target_margin = _f(getattr(company, 'margin_target_pct', None), 10.0) or 10.0

    explanations, suggestions = [], []

    if margin_pct < at_risk_threshold:
        risk_level, color = 'AT_RISK', 'danger'
        explanations.append(f"Margin is below {at_risk_threshold:.0f}% safety threshold ({margin_pct:.1f}%)")
    elif margin_pct < caution_threshold:
        risk_level, color = 'CAUTION', 'warning'
        explanations.append(f"Margin is below {caution_threshold:.0f}% — limited buffer for unexpected costs ({margin_pct:.1f}%)")
    else:
        risk_level, color = 'SAFE', 'success'
        explanations.append(f"Margin is healthy at {margin_pct:.1f}%")

    cost_per_km = round(total_cost / distance_km, 2) if distance_km > 0 else None
    fleet_avg_cpk = _fleet_avg_cpk(company)
    if cost_per_km is not None and fleet_avg_cpk and cost_per_km > fleet_avg_cpk * 1.1:
        explanations.append(f"Cost-per-km on this route is {_fr(cost_per_km, 2)} — above the fleet average of {_fr(fleet_avg_cpk, 2)}")
        suggestions.append("Review your cost model — this route may need a base rate increase")

    # Fuel-delta analysis for an already-saved quote: like-for-like zone
    # against its pricing snapshot (QUOTE-RULES §9).
    if quote is not None:
        try:
            from core.services.quote_snapshot import fuel_change_since_pricing
            change = fuel_change_since_pricing(quote)
            if change and change['delta_pct'] > 3:
                explanations.append(f"Fuel has risen {_fr(change['delta'], 2)}/L since this quote was priced")
                surcharge = int(change['impact_zar'] or fuel_cost * (change['delta_pct'] / 100))
                suggestions.append(f"Add a fuel surcharge of {_fr(surcharge)} to protect the margin")
        except Exception as exc:  # never break the assessment
            logger.warning('revenue-guard fuel analysis failed: %s', exc)

    # Payment-history check — works at quote-creation time when a customer is
    # passed directly, or from a saved quote's customer.
    customer = customer or getattr(quote, 'customer', None)
    if customer is not None and company is not None:
        try:
            from core.services.customer_risk import compute_customer_risk
            risk = compute_customer_risk(customer, company)
            stats = risk.get('stats', {})
            late = stats.get('late_count') or 0
            considered = stats.get('invoice_count') or 0
            if risk.get('band') in ('HIGH', 'CRITICAL') or late > 2:
                explanations.append(
                    f"This client paid late (>30 days) on {late} of {considered} recent invoices "
                    f"(payment risk: {risk.get('band', '?')})"
                )
                suggestions.append("Consider requiring a 50% upfront deposit given payment history")
        except Exception as exc:  # never break the assessment
            logger.warning('revenue-guard payment-history check failed: %s', exc)

    if margin_pct < at_risk_threshold:
        t = target_margin / 100
        # Margin is defined on revenue, so the price hitting target t is cost/(1-t).
        increase_needed = total_cost / (1 - t) - quote_price
        if increase_needed > 0:
            from core.services.quote_costing import fmt_rand
            suggestions.append(f"Increase price by ~{fmt_rand(increase_needed)} to reach a {target_margin:.0f}% margin")

    from core.services.quote_costing import cents, fmt_rand
    margin_floor = cents(float(total_cost))
    floor_fields = _full_floor_fields(total_cost, quote_price, distance_km, company,
                                      vehicle_type or getattr(quote, 'vehicle_type', None), is_full_floor)
    return {
        **floor_fields,
        'success': True,
        'status': risk_level,
        'risk_level': risk_level,
        'color': color,
        'margin_pct': margin_pct,           # unrounded: displays round it
        'cost_per_km': cost_per_km,
        'explanations': explanations,
        'suggestions': suggestions,
        'margin_floor': margin_floor,
        'margin_floor_display': fmt_rand(margin_floor, 2),
        'target_margin_pct': target_margin,
        'warnings': explanations if risk_level != 'SAFE' else [],
    }


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def _fuel_analysis(fuel_cost, fuel_usage_litres, fuel_price_used, quote_total, company=None):
    """Current diesel price + freshness, and this quote's fuel usage/cost."""
    out = {
        'fuel_cost_zar': round(fuel_cost, 2),
        'fuel_usage_litres': round(fuel_usage_litres, 2) if fuel_usage_litres else None,
        'fuel_price_used': round(fuel_price_used, 2) if fuel_price_used else None,
        'fuel_pct_of_total': round(fuel_cost / quote_total * 100, 1) if quote_total > 0 else None,
        'current_price': None,
        'is_stale': False,
        'last_updated': None,
        'stale_warning': None,
        'price_note': None,
    }
    try:
        # The company's own price in force (official zone price or its own),
        # freshness by the first-Wednesday period (QUOTE-RULES §1-§2).
        from core.services.fuel_price import resolve_company_diesel
        res = resolve_company_diesel(company) if company is not None else None
        current = (res or {}).get('price')
        official = (res or {}).get('official') or {}
        out['current_price'] = round(current, 2) if current else None
        from core.services.quote_costing import parse_dt
        eff = parse_dt(official.get('effective_from'))
        out['last_updated'] = timezone.localtime(eff).date().isoformat() if eff else None   # SAST date
        out['source'] = (res or {}).get('source')
        if official.get('stale'):
            out['is_stale'] = True
            out['stale_warning'] = 'The official diesel price for this month is not loaded yet.'
        if current is None:
            out['is_stale'] = True
            out['stale_warning'] = 'No diesel price is available right now.'
        # Flag a meaningful gap between the price used and the price in use.
        if fuel_price_used and current and abs(current - fuel_price_used) / current > 0.02:
            direction = 'higher' if current > fuel_price_used else 'lower'
            out['price_note'] = (
                f"The price used ({_fr(fuel_price_used, 2)}/L) is {direction} than your current price "
                f"({_fr(current, 2)}/L) — fuel cost may be off."
            )
    except Exception as exc:
        logger.warning('fuel analysis failed: %s', exc)
    return out


def _market_analysis(origin, destination, vehicle_type, company, market_rate, quote_total, user=None):
    """Resolve a REAL lane market rate and compare the quote to it.

    When there is no real benchmark for the lane (no cross-platform/own won-quote
    history and no coarse SA estimate), we return market_rate=None with
    source='none' — we do NOT fabricate a figure from the quote itself. Showing a
    made-up "market rate" (previously quote_total x 1.25) misled users into
    thinking it was real market intelligence.

    `user` (the quoting user, when known) excludes their OWN quotes from the
    benchmark at every cascade tier, so a single prolific user's own pricing
    can never become their own market comparison — see lane_benchmark's
    exclude_created_by_user_id."""
    out = {'market_rate': round(market_rate, 2) if market_rate else None,
           'source': 'client' if market_rate else 'none',
           'your_vs_market_pct': None}
    try:
        if origin and destination:
            from core.services.lane_benchmark import resolve_market_rate
            rate, src = resolve_market_rate(
                origin, destination, vehicle_type or None, company=company,
                exclude_created_by_user_id=getattr(user, 'id', None),
                one_way_only=True, sent_only=True,   # QUOTE-RULES §8
            )
            if rate and rate > 0:
                out['market_rate'] = round(float(rate), 2)
                out['source'] = src
        if not out['market_rate'] or out['market_rate'] <= 0:
            # No real benchmark for this lane — say so honestly, don't invent one.
            out['market_rate'] = None
            out['source'] = 'none'
        if out['market_rate'] and quote_total > 0:
            out['your_vs_market_pct'] = round((quote_total - out['market_rate']) / out['market_rate'] * 100, 1)
    except Exception as exc:
        logger.warning('market analysis failed: %s', exc)
    return out


def _optimization(cost_basis, market_rate, client_tier, days,
                  historical_acceptance_rate=0.5, origin=None, destination=None,
                  company=None, prediction_ctx=None, base_features=None):
    """Expected-profit optimization over the carrier's DIRECT COST (not the
    quoted price), anchored on the market rate. Constraint floors/ceilings
    come from the company's own settings (Company.ai_optimizer_*), falling
    back to the platform-wide defaults in settings.py — same
    per-company-else-platform-default pattern as margin_target_pct above."""
    try:
        from django.conf import settings as dj_settings
        from core.services.margin_optimizer import optimize_price

        min_margin_pct = _f(getattr(company, 'ai_optimizer_min_margin_pct', None),
                            getattr(dj_settings, 'AI_OPTIMIZER_MIN_MARGIN_PCT', 5.0)) or 5.0
        min_win_prob_pct = _f(getattr(company, 'ai_optimizer_min_win_probability_pct', None),
                              getattr(dj_settings, 'AI_OPTIMIZER_MIN_WIN_PROBABILITY_PCT', 15.0))
        max_deviation_pct = _f(getattr(company, 'ai_optimizer_max_market_deviation_pct', None),
                               getattr(dj_settings, 'AI_OPTIMIZER_MAX_MARKET_DEVIATION_PCT', 35.0)) or 35.0

        kwargs = dict(
            total_cost=cost_basis,
            market_rate=market_rate or (cost_basis * 1.25 if cost_basis else 0),
            client_tier=client_tier,
            days_until_departure=days,
            historical_acceptance_rate=historical_acceptance_rate,
            origin=origin,
            destination=destination,
            min_margin=min_margin_pct / 100.0,
            min_win_probability=min_win_prob_pct / 100.0,
            max_market_deviation=max_deviation_pct / 100.0,
        )
        if base_features is not None:
            kwargs['base_features'] = base_features
        if prediction_ctx is not None:
            kwargs['predict_proba_fn'] = prediction_ctx.predict_proba
        return optimize_price(**kwargs)
    except Exception as exc:
        logger.warning('price optimization failed: %s', exc)
        # No invented +15%: the company's target margin over the cost floor.
        t = min(max(_f(getattr(company, 'margin_target_pct', None), 10.0) or 10.0, 1.0), 40.0)
        price = round(cost_basis / (1 - t / 100), 2) if cost_basis else None
        return {
            'optimal_price': price,
            'optimal_margin_pct': t if price else None,
            'win_probability_at_optimal': None,
            'expected_profit': 0.0,
            'curve': [],
        }


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------
def _rule_based_narrative(cost, fuel, opt, market, suggested_price, quote_total):
    from core.services.quote_costing import fmt_rand
    parts = []
    if cost.get('success'):
        parts.append(f"Margin is {cost['margin_pct']:.1f}% ({cost['risk_level'].replace('_', ' ').lower()}).".replace('.', ',', 1))
    if suggested_price:
        delta = suggested_price - quote_total
        move = 'above' if delta >= 0 else 'below'
        parts.append(
            f"Suggested price {fmt_rand(suggested_price)}"
            + (f" ({opt['optimal_margin_pct']:.0f}% margin" if opt.get('optimal_margin_pct') is not None else "")
            + (f", {round((opt['win_probability_at_optimal'] or 0) * 100)}% win chance)" if opt.get('win_probability_at_optimal') is not None else ")" if opt.get('optimal_margin_pct') is not None else "")
            + f" — {fmt_rand(abs(delta))} {move} your current total."
        )
    if market.get('market_rate') and market.get('your_vs_market_pct') is not None:
        vs = market['your_vs_market_pct']
        rel = 'above' if vs >= 0 else 'below'
        parts.append(f"Market rate ~{fmt_rand(market['market_rate'])} (you're {abs(vs):.0f}% {rel} market).")
    elif not market.get('market_rate'):
        parts.append("No market data exists for this lane yet, so there's no market comparison.")
    if fuel.get('is_stale'):
        parts.append(fuel.get('stale_warning') or "Fuel price may be out of date.")
    elif fuel.get('price_note'):
        parts.append(fuel['price_note'])
    return " ".join(parts) or "Analysis complete."


NARRATIVE_BUDGET_SECONDS = 20


def _llm_narrative(structured):
    """OpenAI (via agent._llm_generate) narrative grounded in the structured numbers.
    Returns text or None (caller falls back to the rule-based summary)."""
    import json
    from core.services import agent
    if not agent._llm_enabled():
        return None
    system = (
        "You are a South African road-freight pricing analyst. Given a JSON analysis of a "
        "single freight quote (all amounts in ZAR / R), write a concise 2–4 sentence summary a "
        "dispatcher can act on: whether the cost looks right for the route, the fuel situation, the "
        "profit/margin picture, and a one-line justification for the suggested price. Ground EVERY "
        "figure ONLY in the JSON — never invent numbers, never add VAT or derived math. If "
        "market_analysis.market_rate is null, state plainly that there is no market data for this "
        "lane yet and do NOT estimate or mention a market rate. Be direct.\n\n"
        f"Analysis JSON:\n{json.dumps(structured, default=str)}"
    )
    convo = [{"role": "user", "content": "Summarise this quote analysis and justify the suggested price."}]
    try:
        # One budget for the narrative (QUOTE-RULES: <= 20 s), enforced by the
        # SDK itself with no retries: a slow call is CANCELLED at the budget
        # (the connection is closed), not left running in a thread we no
        # longer wait for. A short answer only needs a few hundred tokens.
        try:
            text = agent._llm_generate(system, convo, timeout=NARRATIVE_BUDGET_SECONDS, max_retries=0,
                                       max_tokens=350)
        except Exception as exc:
            if 'timeout' in type(exc).__name__.lower() or 'timed out' in str(exc).lower():
                logger.warning('LLM narrative over its %ss budget; using the rule-based summary',
                               NARRATIVE_BUDGET_SECONDS)
                return None
            raise
        return text.strip() or None
    except Exception as exc:
        logger.warning('LLM narrative failed, using rule-based: %s', exc)
        return None


# The figures a narrative may state (QUOTE-RULES: no number that isn't ours).
HEADLINE_PATHS = (
    ('quote_total',), ('suggested_price',), ('cost_basis',), ('distance_km',),
    ('cost_floor', 'floor'), ('cost_floor', 'target_price'), ('cost_floor', 'minimum_charge'),
    ('cost_analysis', 'margin_pct'), ('cost_analysis', 'cost_per_km'), ('cost_analysis', 'margin_floor'),
    ('cost_analysis', 'full_cost_floor'), ('cost_analysis', 'margin_vs_floor'), ('cost_analysis', 'margin_floor_pct'),
    ('cost_analysis', 'target_margin_pct'),
    ('fuel_analysis', 'fuel_cost_zar'), ('fuel_analysis', 'fuel_usage_litres'), ('fuel_analysis', 'fuel_price_used'),
    ('fuel_analysis', 'current_price'), ('fuel_analysis', 'fuel_pct_of_total'),
    ('price_optimization', 'optimal_price'), ('price_optimization', 'optimal_margin_pct'),
    ('price_optimization', 'expected_profit'), ('price_optimization', 'win_probability_at_optimal'),
    ('market_analysis', 'market_rate'), ('market_analysis', 'your_vs_market_pct'),
    ('ai_prediction', 'win_probability'), ('ai_prediction', 'recommended_price'),
    ('ai_prediction', 'margin_pct'), ('ai_prediction', 'price_vs_market_pct'),
)

_NUMBER = r'(\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)'

# Units of the headline figures: a stated % is only checked against our
# percentages, a rand amount only against rand, km against km.
_PCT_KEYS = {'margin_pct', 'margin_floor_pct', 'target_margin_pct', 'fuel_pct_of_total', 'optimal_margin_pct',
             'your_vs_market_pct', 'price_vs_market_pct'}
_PROB_KEYS = {'win_probability_at_optimal', 'win_probability'}
_KM_KEYS = {'distance_km'}
_LITRE_KEYS = {'fuel_usage_litres'}


def _headline_numbers(structured):
    """[(value, unit)] for every headline figure; unit is 'rand', 'pct',
    'km' or 'litres' (probabilities as %)."""
    out = []
    for path in HEADLINE_PATHS:
        v = structured
        for key in path:
            v = v.get(key) if isinstance(v, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        key = path[-1]
        if key in _PROB_KEYS:
            out.append((float(v) * 100 if 0 < abs(v) <= 1 else float(v), 'pct'))
        elif key in _PCT_KEYS:
            out.append((float(v), 'pct'))
        elif key in _KM_KEYS:
            out.append((float(v), 'km'))
        elif key in _LITRE_KEYS:
            out.append((float(v), 'litres'))
        else:
            out.append((float(v), 'rand'))
    return out


def _parse_number(raw):
    """'1 050' / '36,000' / '32,80' / '12.5' -> float (SA style: comma
    decimals, space thousands; a comma before exactly three digits with
    nothing after is a thousands separator)."""
    txt = raw.replace(' ', ' ').replace(' ', '')
    if ',' in txt and '.' in txt:
        txt = txt.replace(',', '')                 # 36,000.50
    elif ',' in txt:
        parts = txt.split(',')
        txt = txt.replace(',', '') if all(len(p) == 3 for p in parts[1:]) else txt.replace(',', '.')
    return float(txt)


def _decimals(raw):
    """Decimal places as written ('32,80' -> 2, '36,000' -> 0)."""
    txt = raw.replace(' ', ' ').replace(' ', '')
    if ',' in txt and '.' in txt:
        return len(txt.rsplit('.', 1)[1])
    for sep in (',', '.'):
        if sep in txt:
            tail = txt.rsplit(sep, 1)[1]
            if sep == ',' and all(len(p) == 3 for p in txt.split(',')[1:]):
                return 0
            return len(tail)
    return 0


_UNITS_WORDS = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
    'sixteen seventeen eighteen nineteen'.split())}
_TENS_WORDS = {w: 10 * (i + 2) for i, w in enumerate(
    'twenty thirty forty fifty sixty seventy eighty ninety'.split())}
_SCALE_WORDS = {'hundred': 100, 'thousand': 1000, 'million': 1_000_000}
_WORD = '|'.join(sorted([*_UNITS_WORDS, *_TENS_WORDS, *_SCALE_WORDS], key=len, reverse=True))
_WORDS_NUMBER = r'((?:' + _WORD + r')(?:(?:[\s-]+|\s+and\s+)(?:' + _WORD + r'))*)'


def _words_value(phrase):
    """'forty-five thousand' -> 45000; None when it isn't a number."""
    total = current = 0
    seen = False
    for w in re.split(r'[\s-]+', phrase.lower()):
        if w == 'and' or not w:
            continue
        if w in _UNITS_WORDS:
            current += _UNITS_WORDS[w]
        elif w in _TENS_WORDS:
            current += _TENS_WORDS[w]
        elif w == 'hundred':
            current = (current or 1) * 100
        elif w in _SCALE_WORDS:
            total += (current or 1) * _SCALE_WORDS[w]
            current = 0
        else:
            return None
        seen = True
    return float(total + current) if seen else None


# Sign cues next to a figure: "lose R 1 200", "5% below the market".
_NEG_BEFORE = re.compile(r'(?:\b(?:lose|losing|lost|loss of|minus|negative(?: margin)?(?: of)?|shortfall of|'
                         r'down(?: by)?|fell(?: by)?|fallen(?: by)?|dropped(?: by)?|drop of|fall of|decrease of|'
                         r'deficit of|short by|under by|below by)\s*|[-−]\s?)$', re.I)
_POS_BEFORE = re.compile(r'\b(?:gain of|profit of|plus|up(?: by)?|rose(?: by)?|risen(?: by)?|rise of|increase of|'
                         r'ahead by|above by|over by)\s*$', re.I)
_NEG_AFTER = re.compile(r'^\s*(?:below|under|less|lower|cheaper|short|loss|down|negative|in the red)\b', re.I)
_POS_AFTER = re.compile(r'^\s*(?:above|over|more|higher|ahead|profit|up)\b', re.I)


def _stated_sign(text, start, end):
    before, after = text[max(0, start - 30):start], text[end:end + 30]
    if _NEG_BEFORE.search(before) or _NEG_AFTER.match(after):
        return -1
    if _POS_BEFORE.search(before) or _POS_AFTER.match(after):
        return 1
    return 0


_UNIT_SUFFIX = (r'(\s?%|\s*(?:per\s?cent|percent)\b|\s?[kK](?![a-zA-Z])|\s?km\b|\s*kilomet(?:re|er)s?\b|'
                r'\s?(?:L|l|litres?|liters?)\b(?!/)|\s*rand\b)?')
_DIGITS_RE = re.compile(r'(?<![A-Za-z\d])(R\s?|ZAR\s?)?' + _NUMBER + _UNIT_SUFFIX)
_WORDS_RE = re.compile(r'(?<![A-Za-z])(R\s?)?\b' + _WORDS_NUMBER + r'\b' + _UNIT_SUFFIX, re.I)


def _stated_figures(text):
    """Every figure the text states: (value, unit or None, decimals, sign,
    from_k). unit None = no unit written."""
    out, spans = [], []
    for m in _DIGITS_RE.finditer(text):
        try:
            v = _parse_number(m.group(2))
        except ValueError:
            continue
        out.append((m, v, _decimals(m.group(2))))
        spans.append(m.span())
    for m in _WORDS_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        v = _words_value(m.group(2))
        if v is None:
            continue
        out.append((m, v, 0))
    figures = []
    for m, v, dec in out:
        rand = bool(m.group(1))
        suffix = re.sub(r'\s', '', (m.group(3) or '').lower())
        from_k = suffix == 'k'
        if from_k:
            v *= 1000
        if suffix == '%' or suffix.startswith('per'):       # %, percent, per cent
            unit = 'pct'
        elif suffix == 'km' or suffix.startswith('kilomet'):
            unit = 'km'
        elif suffix in ('l', 'litre', 'litres', 'liter', 'liters'):
            unit = 'litres'
        elif rand or from_k or suffix == 'rand':
            unit = 'rand'
        else:
            unit = None
        figures.append((v, unit, dec, _stated_sign(text, m.start(), m.end()), from_k))
    return figures


def narrative_numbers_ok(text, structured):
    """True when every figure the narrative states is one of the headline
    figures. Unit-aware (a % only against our percentages, R only against
    rand amounts, km against km, litres against litres; a figure with no unit
    against any). Within rounding of the figure as written or 0,1%, whichever
    is larger ('R36k' = nearest thousand). Number words ("nine percent",
    "forty-five thousand rand") are checked like digits. A sign cue ("lose
    R 1 200", "5% below the market", "-5%") must agree with the sign of the
    figure it matches. Bare counts up to 10 (nights, sentences) are allowed."""
    known = _headline_numbers(structured)
    for v, unit, dec, sign, from_k in _stated_figures(text or ''):
        if unit is None and v <= 10:
            continue
        tol = max(500.0 if from_k else 0.5 * 10 ** -dec, abs(v) * 0.001)

        def matches(k):
            kv, ku = k
            if unit is not None and ku != unit:
                return False
            if abs(abs(kv) - v) > tol:
                return False
            if sign < 0 and kv > 0:
                return False
            if sign > 0 and kv < 0:
                return False
            return True
        if not any(matches(k) for k in known):
            return False
    return True


def _build_ai_prediction(opt, real_market_rate, prediction_ctx):
    """The 'ai_prediction' response block — the ONLY place a caller should
    look to know whether a number came from a real trained model. Never lets
    a heuristic-driven price_optimization masquerade as this: available is
    True only when prediction_ctx itself resolved a real user/global model
    (see win_prediction.resolve_prediction_context)."""
    if prediction_ctx is None or not prediction_ctx.available:
        return {'available': False, 'reason': 'insufficient_training_data'}

    price = opt.get('optimal_price')
    p_win = opt.get('win_probability_at_optimal')
    if price is None or p_win is None:
        # A model IS trained but the optimizer itself degraded internally
        # (e.g. its except-branch fallback ran) — never show a half-real block.
        return {'available': False, 'reason': 'optimizer_error'}

    if opt.get("used_heuristic_fallback"):
        # The trained model's own curve was too flat/degenerate for the
        # optimizer to trust (margin_optimizer's guard), so it substituted
        # the heuristic. The price/margin/win numbers above are real, just
        # not model-derived -- showing model_scope here would be exactly
        # the masquerade this function exists to prevent.
        return {"available": False, "reason": "model_curve_unusable"}

    return {
        'available': True,
        'model_scope': prediction_ctx.scope,
        'training_samples': prediction_ctx.sample_count,
        'win_probability': p_win,
        'recommended_price': price,
        'expected_profit': opt.get('expected_profit'),
        'margin_pct': opt.get('optimal_margin_pct'),
        'market_rate': real_market_rate,
        'price_vs_market_pct': (
            round((price - real_market_rate) / real_market_rate * 100, 2)
            if real_market_rate else None
        ),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def analyze_quote(payload, company=None, user=None):
    """Run the full analysis. Never raises.

    Expected payload keys (all optional-safe):
      quote_total, direct_cost, distance_km, origin, destination, vehicle_type,
      weight, fuel_cost, toll_cost, driver_cost, fuel_usage_litres,
      fuel_price_used, market_rate, client_tier, days_until_departure,
      historical_acceptance_rate, customer_id

    `user` (the authenticated quoting user, when known) drives the two-tier
    AI resolution (their own model, else the global model, else no AI
    prediction at all) and excludes their own quotes from the market
    benchmark used for that prediction — see win_prediction and
    quote_features. Omitting it degrades gracefully to global-model-or-
    unavailable, same as an unauthenticated/public caller.
    """
    payload = payload or {}
    quote_total = _f(payload.get('quote_total'))
    direct_cost = _f(payload.get('direct_cost'))
    distance_km = _f(payload.get('distance_km'))
    fuel_cost = _f(payload.get('fuel_cost'))
    fuel_usage_litres = _f(payload.get('fuel_usage_litres'))
    fuel_price_used = _f(payload.get('fuel_price_used'))
    origin = str(payload.get('origin') or '').strip()
    destination = str(payload.get('destination') or '').strip()
    vehicle_type = str(payload.get('vehicle_type') or '').strip()
    client_tier = payload.get('client_tier') or 'standard'
    days = _i(payload.get('days_until_departure'), 7)
    hist_rate = _f(payload.get('historical_acceptance_rate'), 0.5)
    hist_rate = max(0.0, min(1.0, hist_rate))
    market_rate = _f(payload.get('market_rate'))

    if quote_total <= 0:
        return {'success': False, 'error': 'quote_total must be > 0'}

    customer = None
    if payload.get('customer_id') and company is not None:
        try:
            from core.models import Customer
            customer = Customer.objects.filter(id=payload['customer_id'], company=company).first()
        except Exception as exc:
            logger.warning('analyze: customer lookup failed: %s', exc)

    # THE engine (QUOTE-RULES): the pricing analysis — compute() floor on the
    # full payload (trip type, legs, duration, flags), the fuel-normalised
    # market (no hard-coded estimates) and the same three choices.
    from core.services.pricing_analysis import analyze_pricing
    pa_payload = dict(payload)
    pa_payload['your_price'] = quote_total
    analysis = {}
    if company is not None:
        try:
            analysis = analyze_pricing(pa_payload, company=company, user=user)
        except Exception as exc:
            logger.warning('analyze: pricing analysis failed: %s', exc)
            analysis = {}
    floor_block = analysis.get('cost_floor') or {}
    costing = analysis.get('costing') or floor_block.get('costing')
    blocking = analysis.get('blocking') or []
    floor = floor_block.get('total') if floor_block.get('complete') else None
    if floor is not None:
        cost_basis, cost_basis_source = floor, 'cost_floor'
    elif direct_cost > 0 and not blocking:
        cost_basis, cost_basis_source = direct_cost, 'client_direct_cost'
    else:
        cost_basis, cost_basis_source = 0.0, 'none'

    m = analysis.get('market') or {}
    usable = bool(m.get('available')) and not m.get('is_estimate')
    market_rate_val = float(m['median']) if usable and m.get('median') else None
    market = {'market_rate': round(market_rate_val, 2) if market_rate_val else None,
              'source': m.get('tier') if usable else 'none',
              'your_vs_market_pct': (round((quote_total - market_rate_val) / market_rate_val * 100, 1)
                                     if market_rate_val else None)}

    choices = analysis.get('choices') or []
    rec = next((c for c in choices if c.get('recommended')), None)
    if rec is None and (analysis.get('recommendation') or {}).get('code') == 'no_evidence' and choices:
        # No market, no model: nothing is "recommended", and the suggested
        # price is the cost floor plus the margin (Safe = the default price).
        rec = choices[0]
    lk = analysis.get('likelihood') or {}
    model = lk.get('model') if lk.get('level') == 'model' else None
    suggested_price = float(rec['price']) if rec and cost_basis > 0 and not blocking else None
    if suggested_price is None and cost_basis_source == 'client_direct_cost':
        # No floor of ours (no company context / route): the company target
        # margin over the client's own direct cost, labelled as such.
        t = min(max(_f(getattr(company, 'margin_target_pct', None), 10.0) or 10.0, 1.0), 40.0)
        suggested_price = round(cost_basis / (1 - t / 100), 2)
        rec = {'price': suggested_price, 'margin_pct': round(t, 1), 'margin': suggested_price - cost_basis,
               'likelihood': {'level': 'rules'}}
    win_at = ((rec['likelihood'].get('pct') or 0) / 100.0
              if rec and (rec.get('likelihood') or {}).get('level') == 'model' else None)
    opt = {
        'optimal_price': suggested_price,
        'optimal_margin_pct': rec['margin_pct'] if rec and suggested_price else None,
        'win_probability_at_optimal': win_at,
        'expected_profit': round(win_at * rec['margin'], 2) if win_at is not None and rec else 0.0,
        'curve': [{'price': p['price'], 'win_probability': p['pct'] / 100.0, 'expected_profit': p['expected_profit']}
                  for p in (model or {}).get('curve') or []],
        'choices': choices,
    }
    if cost_basis > 0:
        cost = assess_revenue_guard(
            total_cost=cost_basis, quote_price=quote_total, distance_km=distance_km, fuel_cost=fuel_cost,
            # Never adds operating cost on top: the floor already has it, and a
            # client's own direct cost is taken as given (labelled).
            company=company, customer=customer, is_full_floor=True)
    else:
        cost = {'success': False, 'error': 'The cost floor is not known yet.', 'blocking': blocking}
    fuel = _fuel_analysis(fuel_cost, fuel_usage_litres, fuel_price_used, quote_total, company)

    ai_prediction = ({'available': True, 'model_scope': model.get('scope'), 'training_samples': model.get('n_closed'),
                      'win_probability': win_at, 'recommended_price': suggested_price,
                      'expected_profit': opt['expected_profit'], 'margin_pct': opt['optimal_margin_pct'],
                      'market_rate': market_rate_val,
                      'price_vs_market_pct': (round((suggested_price - market_rate_val) / market_rate_val * 100, 2)
                                              if market_rate_val and suggested_price else None)}
                     if model and win_at is not None else {'available': False, 'reason': 'insufficient_training_data'})

    structured = {
        'route': f"{origin} → {destination}" if origin and destination else None,
        'distance_km': round(distance_km, 1) if distance_km else None,
        'quote_total': round(quote_total, 2),
        'cost_analysis': cost,
        'fuel_analysis': fuel,
        'price_optimization': opt,
        'market_analysis': market,
        'suggested_price': round(suggested_price, 2) if suggested_price else None,
        'cost_basis': round(cost_basis, 2) if cost_basis else None,
        'cost_basis_source': cost_basis_source,
        'cost_floor': ({k: costing.get(k) for k in ('floor', 'floor_known', 'target_price', 'minimum_charge',
                                                    'lines', 'warnings', 'blocking')} if costing else None),
        'blocking': blocking,
        'recommendation': analysis.get('recommendation'),
        'ai_prediction': ai_prediction,
    }

    narrative = None if (payload.get('skip_narrative') or blocking) else _llm_narrative(
        {k: v for k, v in structured.items() if k not in ('cost_floor',)})
    if narrative and not narrative_numbers_ok(narrative, structured):
        # The model stated a number that isn't ours: never shown.
        logger.info('analyze: LLM narrative rejected (unsupported numbers)')
        narrative = None
    narrative_source = 'llm' if narrative else 'rules'
    if not narrative:
        narrative = _rule_based_narrative(cost, fuel, opt, market, suggested_price, quote_total)

    no_market_data = not market_rate_val
    rationale = None
    if no_market_data and cost_basis > 0:
        t = _f(getattr(company, 'margin_target_pct', None), 10.0) or 10.0
        t = min(max(t, 1.0), 40.0)
        rationale = (
            f"No market data for this lane yet — priced to your target margin of {t:.0f}%. "
            "Market figures appear once real quotes on this lane have been sent and decided."
        )
    elif opt.get('optimal_margin_pct') is not None and opt.get('win_probability_at_optimal') is not None:
        # Only a real model gives an expected-profit optimum.
        rationale = (f"Maximises expected profit at a {opt['optimal_margin_pct']:.0f}% margin with a "
                     f"{round(opt['win_probability_at_optimal'] * 100)}% chance to win.")
    elif opt.get('optimal_margin_pct') is not None:
        rationale = f"Priced at a {opt['optimal_margin_pct']:.0f}% margin over your costs."
    if blocking:
        rationale = 'No suggested price until the blocking items are fixed.'

    return {
        'success': True,
        **structured,
        'suggested_price_rationale': rationale,
        'narrative': narrative,
        'narrative_source': narrative_source,
    }
