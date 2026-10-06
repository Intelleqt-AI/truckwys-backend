"""Storing what the pricing analysis showed at save time (QuotePricingDecision)
and the honest Quote.win_probability that follows from it."""
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

LOSS_REASONS = ('price', 'timing', 'capacity', 'relationship', 'other')


def _dec(v):
    if v in (None, ''):
        return None
    try:
        return Decimal(str(v)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None


def _pct(v):
    try:
        out = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, out))


FLOOR_LINE_KEYS = ('fuel', 'tolls', 'driver_allowance', 'border', 'fixed_cost', 'return_leg')


def _floor_lines(raw):
    """Normalised [{key, label, amount, source_kind}] from the client's floor
    breakdown (the pricing analysis' cost_floor.lines), or []."""
    out = []
    for ln in raw if isinstance(raw, list) else []:
        if not isinstance(ln, dict) or ln.get('key') not in FLOOR_LINE_KEYS:
            continue
        amount = _dec(ln.get('amount'))
        if amount is None:
            continue
        src = ln.get('source') if isinstance(ln.get('source'), dict) else {}
        out.append({'key': ln['key'], 'label': str(ln.get('label') or ln['key'])[:40],
                    'amount': int(amount.to_integral_value()),
                    'source_kind': str(ln.get('source_kind') or src.get('kind') or '')[:20]})
    return out[:len(FLOOR_LINE_KEYS)]


def _quote_pricing_payload(quote):
    """The pricing-analysis inputs the WIN MODEL needs, rebuilt from the saved
    quote (lane, vehicle, weight, distance, legs, pickup date, customer)."""
    legs = 2 if quote.trip_type == 'ROUND_TRIP' else 1
    one_way = float(quote.distance or 0)
    return {
        'quote_id': quote.id, 'customer_id': quote.customer_id,
        'origin': quote.origin, 'destination': quote.destination,
        'pickup_location': quote.pickup_location, 'delivery_location': quote.delivery_location,
        'vehicle_type': quote.vehicle_type, 'weight': float(quote.weight or 0),
        'distance_km': one_way * legs, 'one_way_distance_km': one_way, 'legs': legs,
        'pickup_date': quote.pickup_date.isoformat() if quote.pickup_date else None,
    }


def server_model_pct(quote, final_price, floor, *, user):
    """(pct, model_version) — the win model's likelihood at the FINAL price,
    computed here with exactly the pricing analysis' model-level gates (model
    resolves, market reference, training range, price-sensitive curve, price
    inside the range), or (None, None). Never trusts a client figure. Never
    raises (a scoring failure just means no model %)."""
    try:
        from core.services import pricing_analysis as pa
        from core.services.win_prediction import resolve_prediction_context
        price = float(final_price or 0)
        if price <= 0:
            return None, None
        company = quote.company
        ctx = resolve_prediction_context(user, company)
        if not ctx.available:
            return None, None
        payload = _quote_pricing_payload(quote)
        origin, destination = pa._resolve_lane(payload)
        floor_total = float(floor) if floor and float(floor) > 0 else price * 0.75
        block, _reason, predictor = pa.model_likelihood(
            ctx=ctx, company=company, user=user, payload=payload, origin=origin, destination=destination,
            vt_name=quote.vehicle_type or None, floor_total=floor_total, probe_prices=[price],
            customer_id=quote.customer_id)
        if block is None or predictor is None:
            return None, None
        predict, in_range = predictor
        if not in_range(price):
            return None, None
        return int(round(predict(price) * 100)), block.get('version')
    except Exception as exc:
        logger.warning('server_model_pct failed for quote %s: %s', getattr(quote, 'id', None), exc)
        return None, None


def save_pricing_decision(quote, decision: dict, *, user=None):
    """Upsert the quote's QuotePricingDecision and set Quote.win_probability.

    Call inside the same transaction as the quote save: a database error here
    PROPAGATES, so the save fails rather than reporting a decision that was
    never stored.

    The likelihood stored is the SERVER's model % at the final price (same
    gates as the pricing analysis); the client's figure is kept only as
    `client_pct` in the payload. win_probability = that server % at model
    level, else null."""
    from core.models import QuotePricingDecision

    market = decision.get('market') if isinstance(decision.get('market'), dict) else {}
    final_price = _dec(decision.get('final_price')) or quote.total_amount
    floor = _dec(decision.get('floor'))
    pct, server_version = server_model_pct(quote, final_price, floor, user=user)
    level = 'model' if pct is not None else 'rules'
    payload = dict(decision)
    payload.update({
        'client_level': decision.get('likelihood_level'),
        'client_pct': _pct(decision.get('likelihood_at_final_pct')),
        'likelihood_level': level,
        'likelihood_at_final_pct': pct,
        'floor_lines': _floor_lines(decision.get('floor_lines')),
        # Whole-rand adjustment the operator applied on top of the picked
        # price (client figure, kept for traceability), or None.
        'price_adjustment': (int(_dec(decision.get('price_adjustment')).to_integral_value())
                             if _dec(decision.get('price_adjustment')) is not None else None),
    })
    if server_version:
        payload['model_version'] = server_version
    fields = {
        'company_id': quote.company_id,
        'created_by': user if getattr(user, 'is_authenticated', False) else None,
        'version': str(decision.get('version') or '')[:20],
        'picked_choice': decision.get('picked_choice') or '',
        'final_price': final_price,
        'floor': floor,
        'likelihood_level': level,
        'likelihood_at_final_pct': pct,
        'band_at_final': '' if level == 'model' else str(decision.get('band_at_final') or '')[:20],
        'model_version': str(server_version or '')[:60],
        'market_tier': str(market.get('tier') or '')[:20],
        'payload': payload,
    }
    row, _ = QuotePricingDecision.objects.update_or_create(quote=quote, defaults=fields)
    quote.pricing_decision = row   # refresh the cached relation (select_related) for the response

    win_probability = Decimal(pct) if pct is not None else None
    if quote.win_probability != win_probability:
        quote.win_probability = win_probability
        quote.save(update_fields=['win_probability', 'updated_at'])


def decision_representation(quote):
    """The stored decision for the quote detail response, or None — always a
    fresh read, never the in-memory relation (which a failed write could have
    left populated)."""
    from core.models import QuotePricingDecision
    d = QuotePricingDecision.objects.filter(quote_id=quote.id).first()
    if d is None:
        return None
    payload = dict(d.payload or {})
    payload.update({
        'picked_choice': d.picked_choice or payload.get('picked_choice'),
        'likelihood_level': d.likelihood_level or payload.get('likelihood_level'),
        'likelihood_at_final_pct': d.likelihood_at_final_pct,
        'floor_lines': payload.get('floor_lines') or [],
        'saved_at': d.updated_at.isoformat() if d.updated_at else None,
    })
    return payload


def loss_reason_representation(quote):
    """{'reason', 'note'} from the quote's outcome row when it was lost with
    a structured reason, else None."""
    try:
        from core.models import QuoteOutcome
        row = QuoteOutcome.objects.filter(quote=quote).only('loss_reason', 'loss_reason_note').first()
    except Exception:
        return None
    if row is None or not row.loss_reason:
        return None
    return {'reason': row.loss_reason, 'note': row.loss_reason_note or ''}


def clean_loss_reason(value):
    """A valid loss reason code, or '' (unknown values are dropped, not an error)."""
    v = str(value or '').strip().lower()
    return v if v in LOSS_REASONS else ''
