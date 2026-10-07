"""Storing what the pricing analysis showed at save time (QuotePricingDecision)
and the honest Quote.win_probability that follows from it."""
import logging
from decimal import Decimal, InvalidOperation

from django.utils import timezone

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
        from core.services.pricing_analysis import _half_up
        out = _half_up(float(v))
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


def pricing_payload_from_fields(fields: dict):
    """The pricing-analysis inputs the likelihood needs, from a quote's field
    values (a saved Quote's attributes, or a serializer's validated_data
    merged over the instance — so it works BEFORE the quote is written)."""
    legs = 2 if fields.get('trip_type') == 'ROUND_TRIP' else 1
    one_way = float(fields.get('distance') or 0)
    customer = fields.get('customer')
    pickup = fields.get('pickup_date')
    return {
        'quote_id': fields.get('id'), 'customer_id': getattr(customer, 'id', customer),
        'origin': fields.get('origin'), 'destination': fields.get('destination'),
        'pickup_location': fields.get('pickup_location'), 'delivery_location': fields.get('delivery_location'),
        'vehicle_type': fields.get('vehicle_type'), 'weight': float(fields.get('weight') or 0),
        'distance_km': one_way * legs, 'one_way_distance_km': one_way, 'legs': legs,
        'pickup_date': pickup.isoformat() if hasattr(pickup, 'isoformat') else pickup,
    }


QUOTE_SCORING_FIELDS = ('id', 'customer', 'origin', 'destination', 'pickup_location', 'delivery_location',
                        'vehicle_type', 'weight', 'distance', 'trip_type', 'pickup_date', 'total_amount')


def quote_fields(instance=None, validated_data=None):
    out = {f: getattr(instance, f, None) for f in QUOTE_SCORING_FIELDS} if instance is not None else {}
    out.update({k: v for k, v in (validated_data or {}).items() if k in QUOTE_SCORING_FIELDS})
    return out


def score_final_price(fields: dict, decision: dict, *, company, user):
    """The SERVER's likelihood at the decision's final price, from the same
    rules as the pricing analysis: {'level', 'pct', 'band', 'model_version',
    'final_price', 'floor'}.

      model level  a model resolves, the lane has a market reference and the
                   final price sits inside the model's range -> pct, band None;
      rules level  otherwise -> band from the same thresholds the panel uses
                   (market range, scaled for a return trip, + this customer's
                   record), pct None. The client's band/pct are never used.

    Runs BEFORE the write transaction (no SQLite write lock held while
    scoring). A DatabaseError PROPAGATES (the save then fails with 503); any
    other failure just means no model % / no band."""
    from django.db import DatabaseError
    from core.services import pricing_analysis as pa

    final_price = _dec(decision.get('final_price')) or _dec(fields.get('total_amount'))
    floor = _dec(decision.get('floor'))
    out = {'level': 'rules', 'pct': None, 'band': None, 'model_version': None,
           'final_price': final_price, 'floor': floor}
    price = float(final_price or 0)
    if price <= 0 or company is None:
        return out
    payload = pricing_payload_from_fields(fields)
    origin, destination = pa._resolve_lane(payload)
    vt_name = fields.get('vehicle_type') or None
    floor_total = float(floor) if floor and float(floor) > 0 else price * 0.75
    try:
        market = pa.trip_market(origin, destination, vt_name, company, payload['quote_id'], payload['legs'])
        cust = None
        customer = fields.get('customer')
        if customer is not None and not hasattr(customer, 'id'):
            from core.models import Customer
            customer = Customer.objects.filter(id=customer, company=company).first()
        if customer is not None and getattr(customer, 'company_id', None) == company.id:
            cust = pa.customer_evidence(customer, company, origin, destination, payload['quote_id'])
        thresholds, _basis = pa.rules_thresholds(market, cust)

        from core.services.win_prediction import resolve_prediction_context
        ctx = resolve_prediction_context(user, company)
        if ctx.available:
            try:
                block, _reason, predictor = pa.model_likelihood(
                    ctx=ctx, company=company, user=user, payload=payload, origin=origin, destination=destination,
                    vt_name=vt_name, floor_total=floor_total, probe_prices=[price],
                    customer_id=getattr(customer, 'id', None))
            except DatabaseError:
                raise
            except Exception as exc:   # a model that can't score -> rules level
                logger.warning('score_final_price: model scoring failed: %s', exc)
                block, predictor = None, None
            if block is not None and predictor is not None and predictor[1](price):
                out.update({'level': 'model', 'pct': pa._half_up(predictor[0](price) * 100),
                            'model_version': block.get('version')})
                return out
        out['band'] = pa._band(price, thresholds)
        return out
    except DatabaseError:
        raise
    except Exception as exc:
        logger.warning('score_final_price failed: %s', exc)
        return out


def save_pricing_decision(quote, decision: dict, *, user=None, scored=None):
    """Upsert the quote's QuotePricingDecision and set Quote.win_probability.

    `scored` is score_final_price()'s result, computed BEFORE the transaction
    (pass None only from callers outside the serializer: it is scored here
    then). Call inside the quote's own transaction: a database error here
    PROPAGATES, so the save fails rather than reporting a decision that was
    never stored. Stored level / pct / band are the SERVER's; the client's
    figures are kept only as client_level / client_pct / client_band."""
    from core.models import QuotePricingDecision

    if scored is None:
        scored = score_final_price(quote_fields(quote), decision, company=quote.company, user=user)
    market = decision.get('market') if isinstance(decision.get('market'), dict) else {}
    level, pct, band = scored['level'], scored['pct'], scored['band']
    payload = dict(decision)
    payload.update({
        'client_level': decision.get('likelihood_level'),
        'client_pct': _pct(decision.get('likelihood_at_final_pct')),
        'client_band': decision.get('band_at_final'),
        'likelihood_level': level,
        'likelihood_at_final_pct': pct,
        'band_at_final': band,
        'floor_lines': _floor_lines(decision.get('floor_lines')),
        # Whole-rand adjustment the operator applied on top of the picked
        # price (client figure, kept for traceability), or None.
        'price_adjustment': (int(_dec(decision.get('price_adjustment')).to_integral_value())
                             if _dec(decision.get('price_adjustment')) is not None else None),
    })
    if scored.get('model_version'):
        payload['model_version'] = scored['model_version']
    fields = {
        'company_id': quote.company_id,
        'created_by': user if getattr(user, 'is_authenticated', False) else None,
        'version': str(decision.get('version') or '')[:20],
        'picked_choice': decision.get('picked_choice') or '',
        'final_price': scored['final_price'] or quote.total_amount,
        'floor': scored['floor'],
        'likelihood_level': level,
        'likelihood_at_final_pct': pct,
        'band_at_final': str(band or '')[:20],
        'model_version': str(scored.get('model_version') or '')[:60],
        'market_tier': str(market.get('tier') or '')[:20],
        'payload': payload,
        # Current unless the decided price already differs from the quote's
        # total (a client that sent figures for an earlier price).
        'superseded_at': None if _matches_total(scored['final_price'] or quote.total_amount, quote.total_amount)
        else timezone.now(),
    }
    row, _ = QuotePricingDecision.objects.update_or_create(quote=quote, defaults=fields)
    quote.pricing_decision = row   # refresh the cached relation (select_related) for the response

    # No chance is kept for a decision that doesn't describe the saved price.
    win_probability = Decimal(pct) if pct is not None and row.superseded_at is None else None
    if quote.win_probability != win_probability:
        quote.win_probability = win_probability
        quote.save(update_fields=['win_probability', 'updated_at'])


# A decision describes the quote while its final price equals the quote's
# total to within this (the quote detail page's own rule, pricingDecision.ts).
PRICE_MATCH_TOLERANCE = Decimal('0.5')


def _matches_total(final_price, total) -> bool:
    if final_price is None or total is None:
        return False
    try:
        return abs(Decimal(str(final_price)) - Decimal(str(total))) <= PRICE_MATCH_TOLERANCE
    except (ArithmeticError, ValueError, InvalidOperation):
        return False


def supersede_if_price_changed(quote) -> bool:
    """Called after every quote save. When the quote's total no longer
    matches its current decision's final price (the price was changed
    without a new analysis), mark the decision superseded and clear the
    quote's win_probability, which was the chance at the old price. True
    when it did. A decision saved in the same request clears the mark again
    (save_pricing_decision runs after the quote's own save)."""
    from core.models import QuotePricingDecision, Quote
    row = (QuotePricingDecision.objects.filter(quote_id=quote.pk, superseded_at__isnull=True)
           .values_list('pk', 'final_price').first())
    if row is None or _matches_total(row[1], quote.total_amount):
        return False
    QuotePricingDecision.objects.filter(pk=row[0]).update(superseded_at=timezone.now())
    if quote.win_probability is not None:
        Quote.objects.filter(pk=quote.pk).update(win_probability=None)
        quote.win_probability = None
    return True


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
        # The price was changed after this was saved: screens must not
        # restore or show its price, margin or chance as the quote's.
        'stale': d.superseded_at is not None,
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


def agreed_price_representation(quote):
    """The price agreed on a WON quote when it differs from the quote's total
    (QuoteOutcome.final_price, 2 dp, excl. VAT like total_amount), else None.
    Display only — billing still uses the quote total."""
    try:
        from core.models import QuoteOutcome
        row = (QuoteOutcome.objects.filter(quote=quote).only('outcome', 'final_price').first())
    except Exception:
        return None
    if row is None or row.outcome != 'accepted' or row.final_price is None or quote.total_amount is None:
        return None
    agreed = Decimal(row.final_price).quantize(Decimal('0.01'))
    if abs(agreed - Decimal(quote.total_amount)) < Decimal('0.005') or agreed <= 0:
        return None
    return float(agreed)


def agreed_margin_representation(agreed_price, decision):
    """{'agreed_margin': whole rand, 'agreed_margin_pct': whole % (half up)}
    = agreed price − the decision's cost floor (/ agreed price). Both null
    when the agreed price or the floor is missing. Display only."""
    from core.services.pricing_analysis import _rand, pct_half_up
    # A stale decision's floor was worked out for an earlier version of the quote.
    if isinstance(decision, dict) and decision.get('stale'):
        return {'agreed_margin': None, 'agreed_margin_pct': None}
    floor = (decision or {}).get('floor') if isinstance(decision, dict) else None
    try:
        agreed, floor = float(agreed_price), float(floor)
    except (TypeError, ValueError):
        return {'agreed_margin': None, 'agreed_margin_pct': None}
    if agreed <= 0:
        return {'agreed_margin': None, 'agreed_margin_pct': None}
    return {'agreed_margin': _rand(agreed - floor), 'agreed_margin_pct': pct_half_up(agreed - floor, agreed)}


def clean_loss_reason(value):
    """A valid loss reason code, or '' (unknown values are dropped, not an error)."""
    v = str(value or '').strip().lower()
    return v if v in LOSS_REASONS else ''
