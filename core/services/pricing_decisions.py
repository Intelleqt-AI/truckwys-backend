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


def save_pricing_decision(quote, decision: dict, *, user=None):
    """Upsert the quote's QuotePricingDecision and set Quote.win_probability.

    win_probability is set ONLY to the model likelihood at the final price,
    and only when the decision says the model priced it AND a real model is
    actually available to this company right now (a client can't promote a
    rules-level band, or a stale model figure, into a stored probability).
    Otherwise it is cleared to null. Never raises."""
    from core.models import QuotePricingDecision

    try:
        level = decision.get('likelihood_level') or ''
        pct = _pct(decision.get('likelihood_at_final_pct'))
        market = decision.get('market') if isinstance(decision.get('market'), dict) else {}
        fields = {
            'company_id': quote.company_id,
            'created_by': user if getattr(user, 'is_authenticated', False) else None,
            'version': str(decision.get('version') or '')[:20],
            'picked_choice': decision.get('picked_choice') or '',
            'final_price': _dec(decision.get('final_price')),
            'floor': _dec(decision.get('floor')),
            'likelihood_level': level,
            'likelihood_at_final_pct': pct if level == 'model' else None,
            'band_at_final': str(decision.get('band_at_final') or '')[:20],
            'model_version': str(decision.get('model_version') or '')[:60],
            'market_tier': str(market.get('tier') or '')[:20],
            'payload': decision,
        }
        QuotePricingDecision.objects.update_or_create(quote=quote, defaults=fields)

        win_probability = None
        if level == 'model' and pct is not None and _model_available(user, quote.company):
            win_probability = Decimal(pct)
        if quote.win_probability != win_probability:
            quote.win_probability = win_probability
            quote.save(update_fields=['win_probability', 'updated_at'])
    except Exception as exc:
        logger.warning('save_pricing_decision failed for quote %s: %s', getattr(quote, 'id', None), exc)


def _model_available(user, company) -> bool:
    try:
        from core.services.win_prediction import resolve_prediction_context
        return bool(resolve_prediction_context(user, company).available)
    except Exception:
        return False


def decision_representation(quote):
    """The stored decision for the quote detail response, or None."""
    try:
        d = quote.pricing_decision
    except Exception:
        return None
    payload = dict(d.payload or {})
    payload.update({
        'picked_choice': d.picked_choice or payload.get('picked_choice'),
        'likelihood_level': d.likelihood_level or payload.get('likelihood_level'),
        'likelihood_at_final_pct': d.likelihood_at_final_pct,
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
