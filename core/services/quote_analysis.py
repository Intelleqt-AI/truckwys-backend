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

from django.utils import timezone

logger = logging.getLogger(__name__)


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
def _fleet_avg_cpk(company):
    """This company's real cost-per-km from completed trips' expenses over the
    last 12 months (cached 1h). Falls back to the industry default when there
    are fewer than 10 costed trips. Never raises."""
    FALLBACK = 19.80
    if company is None or not getattr(company, 'id', None):
        return FALLBACK
    from django.core.cache import cache
    cache_key = f'fleet_avg_cpk_{company.id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    cpk = FALLBACK
    try:
        from datetime import timedelta
        from django.db.models import Sum
        from core.models import Expense, Trip

        year_ago = timezone.now().date() - timedelta(days=365)
        exp = Expense.objects.filter(
            company=company, trip__isnull=False, trip__status='COMPLETED',
            trip__distance_km__gt=0, expense_date__gte=year_ago,
        )
        trip_ids = list(exp.values_list('trip_id', flat=True).distinct())
        if len(trip_ids) >= 10:
            total_cost = float(exp.aggregate(s=Sum('amount'))['s'] or 0)
            total_km = float(
                Trip.objects.filter(id__in=trip_ids).aggregate(s=Sum('distance_km'))['s'] or 0
            )
            if total_cost > 0 and total_km > 0:
                cpk = round(total_cost / total_km, 2)
    except Exception as exc:
        logger.warning('fleet CPK aggregate failed: %s', exc)
    cache.set(cache_key, cpk, 3600)
    return cpk


def assess_revenue_guard(*, total_cost, quote_price, distance_km=0.0,
                         fuel_cost=0.0, company=None, quote=None, customer=None):
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
    if cost_per_km is not None and cost_per_km > fleet_avg_cpk * 1.1:
        explanations.append(f"Cost-per-km on this route is R{cost_per_km:.2f} — above the fleet average of R{fleet_avg_cpk:.2f}")
        suggestions.append("Review your cost model — this route may need a base rate increase")

    # Fuel-delta analysis for an already-saved quote.
    if quote is not None:
        try:
            from core.services.fuel_price import fetch_fuel_prices

            if getattr(quote, 'fuel_price_at_creation', None):
                fuel_at_creation = _f(quote.fuel_price_at_creation)
                if fuel_at_creation > 0:
                    fuel_current = _f(fetch_fuel_prices().diesel_inland)
                    delta_pct = ((fuel_current - fuel_at_creation) / fuel_at_creation) * 100
                    if delta_pct > 3:
                        delta_zar = fuel_current - fuel_at_creation
                        explanations.append(f"Fuel has risen R{delta_zar:.2f}/L since this quote was created")
                        surcharge = int(fuel_cost * (delta_pct / 100))
                        suggestions.append(f"Add a fuel surcharge of R{surcharge} to protect the margin")
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
            suggestions.append(f"Increase price by ~R{int(increase_needed)} to reach a {target_margin:.0f}% margin")

    margin_floor = int(total_cost)
    return {
        'success': True,
        'status': risk_level,
        'risk_level': risk_level,
        'color': color,
        'margin_pct': round(margin_pct, 2),
        'cost_per_km': cost_per_km,
        'explanations': explanations,
        'suggestions': suggestions,
        'margin_floor': margin_floor,
        'margin_floor_display': f"R{margin_floor:,}",
        'target_margin_pct': target_margin,
        'warnings': explanations if risk_level != 'SAFE' else [],
    }


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def _fuel_analysis(fuel_cost, fuel_usage_litres, fuel_price_used, quote_total):
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
        from core.services.fuel_price import fetch_fuel_prices
        fp = fetch_fuel_prices()
        current = _f(fp.diesel_inland)
        days_old = (timezone.now().date() - fp.date).days
        out['current_price'] = round(current, 2)
        out['last_updated'] = fp.date.isoformat()
        out['source'] = fp.source
        if days_old > 7:
            out['is_stale'] = True
            out['stale_warning'] = f"Diesel price last updated {days_old} days ago — consider refreshing."
        # Flag a meaningful gap between the price used and the live price.
        if fuel_price_used and current and abs(current - fuel_price_used) / current > 0.02:
            direction = 'higher' if current > fuel_price_used else 'lower'
            out['price_note'] = (
                f"The price used (R{fuel_price_used:.2f}/L) is {direction} than the live price "
                f"(R{current:.2f}/L) — fuel cost may be off."
            )
    except Exception as exc:
        logger.warning('fuel analysis failed: %s', exc)
    return out


def _market_analysis(origin, destination, vehicle_type, company, market_rate, quote_total):
    """Resolve a REAL lane market rate and compare the quote to it.

    When there is no real benchmark for the lane (no cross-platform/own won-quote
    history and no coarse SA estimate), we return market_rate=None with
    source='none' — we do NOT fabricate a figure from the quote itself. Showing a
    made-up "market rate" (previously quote_total x 1.25) misled users into
    thinking it was real market intelligence."""
    out = {'market_rate': round(market_rate, 2) if market_rate else None,
           'source': 'client' if market_rate else 'none',
           'your_vs_market_pct': None}
    try:
        if origin and destination:
            from core.services.lane_benchmark import resolve_market_rate
            rate, src = resolve_market_rate(origin, destination, vehicle_type or None, company=company)
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
                  historical_acceptance_rate=0.5, origin=None, destination=None):
    """Expected-profit optimization over the carrier's DIRECT COST (not the
    quoted price), anchored on the market rate."""
    try:
        from core.services.margin_optimizer import optimize_price
        return optimize_price(
            total_cost=cost_basis,
            market_rate=market_rate or (cost_basis * 1.25 if cost_basis else 0),
            client_tier=client_tier,
            days_until_departure=days,
            historical_acceptance_rate=historical_acceptance_rate,
            origin=origin,
            destination=destination,
        )
    except Exception as exc:
        logger.warning('price optimization failed: %s', exc)
        return {
            'optimal_price': round(cost_basis * 1.15, 2) if cost_basis else 0.0,
            'optimal_margin_pct': 15.0,
            'win_probability_at_optimal': None,
            'expected_profit': 0.0,
            'curve': [],
        }


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------
def _rule_based_narrative(cost, fuel, opt, market, suggested_price, quote_total):
    parts = []
    if cost.get('success'):
        parts.append(f"Margin is {cost['margin_pct']:.1f}% ({cost['risk_level'].replace('_', ' ').lower()}).")
    if suggested_price:
        delta = suggested_price - quote_total
        move = 'above' if delta >= 0 else 'below'
        parts.append(
            f"Suggested price R{suggested_price:,.0f}"
            + (f" ({opt['optimal_margin_pct']:.0f}% margin" if opt.get('optimal_margin_pct') is not None else "")
            + (f", {round((opt['win_probability_at_optimal'] or 0) * 100)}% win chance)" if opt.get('win_probability_at_optimal') is not None else ")" if opt.get('optimal_margin_pct') is not None else "")
            + f" — R{abs(delta):,.0f} {move} your current total."
        )
    if market.get('market_rate') and market.get('your_vs_market_pct') is not None:
        vs = market['your_vs_market_pct']
        rel = 'above' if vs >= 0 else 'below'
        parts.append(f"Market rate ~R{market['market_rate']:,.0f} (you're {abs(vs):.0f}% {rel} market).")
    elif not market.get('market_rate'):
        parts.append("No market data exists for this lane yet, so there's no market comparison.")
    if fuel.get('is_stale'):
        parts.append(fuel.get('stale_warning') or "Fuel price may be out of date.")
    elif fuel.get('price_note'):
        parts.append(fuel['price_note'])
    return " ".join(parts) or "Analysis complete."


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
        text = agent._llm_generate(system, convo)
        return text.strip() or None
    except Exception as exc:
        logger.warning('LLM narrative failed, using rule-based: %s', exc)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def analyze_quote(payload, company=None):
    """Run the full analysis. Never raises.

    Expected payload keys (all optional-safe):
      quote_total, direct_cost, distance_km, origin, destination, vehicle_type,
      weight, fuel_cost, toll_cost, driver_cost, fuel_usage_litres,
      fuel_price_used, market_rate, client_tier, days_until_departure,
      historical_acceptance_rate, customer_id
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

    # Expected profit must be computed against what the job COSTS, not the
    # price being asked — otherwise the optimum is forced above the current total.
    cost_basis = direct_cost or quote_total

    customer = None
    if payload.get('customer_id') and company is not None:
        try:
            from core.models import Customer
            customer = Customer.objects.filter(
                id=payload['customer_id'], company=company,
            ).first()
        except Exception as exc:
            logger.warning('analyze: customer lookup failed: %s', exc)

    market = _market_analysis(origin, destination, vehicle_type, company, market_rate, quote_total)
    # market_rate is now either a REAL benchmark or None (no fabricated anchor).
    # When it's None, _optimization anchors its search band on cost internally
    # (cost_basis * 1.25) — that's a private search bound, never shown as a
    # "market rate".
    real_market_rate = market.get('market_rate')
    opt = _optimization(
        cost_basis, real_market_rate, client_tier, days,
        historical_acceptance_rate=hist_rate, origin=origin, destination=destination,
    )

    # With NO market data the optimizer's "optimum" is an artefact of its own
    # synthetic cost*1.25 anchor — effectively a fixed ~30% markup pulled from
    # thin air, contradicting the Revenue Guard's margin-target advice shown on
    # the same screen. Until the lane has a real benchmark, recommend the
    # company's own target margin instead (same formula the guard uses), so
    # both panels agree. The curve is kept so the sweet-spot chart still renders.
    no_market_data = not real_market_rate
    if no_market_data and cost_basis > 0:
        t = _f(getattr(company, 'margin_target_pct', None), 10.0) or 10.0
        t = min(max(t, 1.0), 40.0)  # sane bounds; t is margin-on-price in %
        target_price = round(cost_basis / (1 - t / 100), 2)
        curve = opt.get('curve') or []
        nearest = min(curve, key=lambda p: abs(_f(p.get('price')) - target_price)) if curve else None
        p_win = nearest.get('win_probability') if nearest else None
        opt['optimal_price'] = target_price
        opt['optimal_margin_pct'] = round((target_price - cost_basis) / cost_basis * 100, 1)
        opt['win_probability_at_optimal'] = p_win
        opt['expected_profit'] = round(
            (target_price - cost_basis) * (p_win if p_win is not None else 1.0), 2)

    cost = assess_revenue_guard(
        total_cost=cost_basis, quote_price=quote_total,
        distance_km=distance_km, fuel_cost=fuel_cost, company=company,
        customer=customer,
    )
    fuel = _fuel_analysis(fuel_cost, fuel_usage_litres, fuel_price_used, quote_total)

    suggested_price = _f(opt.get('optimal_price')) or round(quote_total * 1.15, 2)

    structured = {
        'route': f"{origin} → {destination}" if origin and destination else None,
        'quote_total': round(quote_total, 2),
        'cost_analysis': cost,
        'fuel_analysis': fuel,
        'price_optimization': opt,
        'market_analysis': market,
        'suggested_price': round(suggested_price, 2),
    }

    # skip_narrative: callers that never display the narrative (e.g. the Quote
    # Builder's live panel, which re-analyzes on every cost change) skip the
    # synchronous OpenAI call — it dominates response time by seconds.
    narrative = None if payload.get('skip_narrative') else _llm_narrative(structured)
    narrative_source = 'llm' if narrative else 'rules'
    if not narrative:
        narrative = _rule_based_narrative(cost, fuel, opt, market, suggested_price, quote_total)

    rationale = None
    if no_market_data and cost_basis > 0:
        t = _f(getattr(company, 'margin_target_pct', None), 10.0) or 10.0
        t = min(max(t, 1.0), 40.0)
        rationale = (
            f"No market data for this lane yet — priced to your target margin of {t:.0f}%. "
            "As you win quotes on this lane, pricing will optimise for expected profit."
        )
    elif opt.get('optimal_margin_pct') is not None:
        rationale = (
            f"Maximises expected profit at a {opt['optimal_margin_pct']:.0f}% margin"
            + (f" with a {round((opt['win_probability_at_optimal'] or 0) * 100)}% win probability."
               if opt.get('win_probability_at_optimal') is not None else ".")
        )

    return {
        'success': True,
        **structured,
        'suggested_price_rationale': rationale,
        'narrative': narrative,
        'narrative_source': narrative_source,
    }
