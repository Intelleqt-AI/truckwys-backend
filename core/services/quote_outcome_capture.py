"""Record quote outcomes (accepted/rejected) for the quote-ML flywheel.

One entry point — record_quote_outcome — called from EVERY path where a quote
is decided: the public customer accept/decline link, the operator's status
update, and the manual "Mark Outcome" button. It updates the Quote's outcome
fields and upserts the single QuoteOutcome row per quote (unique constraint),
snapshotting the point-in-time ML features so training never has to
reconstruct them against drifted benchmarks later.

Never raises: outcome capture must never break an acceptance flow.
"""
import logging
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)


def _client_tier_for(quote):
    """Tier from the customer's company-scoped accepted-quote history."""
    from core.models import Quote
    if not getattr(quote, 'customer_id', None):
        return 'new'
    accepted = Quote.objects.filter(
        company=quote.company, customer_id=quote.customer_id, outcome='accepted',
    ).exclude(id=quote.id).count()
    if accepted >= 10:
        return 'vip'
    if accepted >= 3:
        return 'regular'
    return 'new'


def _historical_acceptance_rate(quote):
    """Customer's acceptance rate BEFORE this outcome (excludes this quote)."""
    from core.models import Quote
    if not getattr(quote, 'customer_id', None):
        return None
    decided = Quote.objects.filter(
        company=quote.company, customer_id=quote.customer_id,
        outcome__in=['accepted', 'rejected'],
    ).exclude(id=quote.id)
    total = decided.count()
    if total == 0:
        return None
    accepted = decided.filter(outcome='accepted').count()
    return (Decimal(accepted) / Decimal(total)).quantize(Decimal('0.0001'))


def record_quote_outcome(quote, outcome, *, rejection_reason='', final_price=None,
                         update_quote=True, allow_flip=False):
    """Upsert the QuoteOutcome row for a quote and sync the quote's own
    outcome fields. Returns the QuoteOutcome or None on failure. Never raises.

    Idempotent: re-recording the SAME outcome (e.g. ACCEPTED then IT, or a
    repeated Mark Outcome click) returns the existing row untouched so the
    point-in-time snapshot and accepted_at never drift. Recording the OPPOSITE
    outcome is refused unless allow_flip=True (the deliberate operator
    correction path) — a stale public link or status change can never silently
    flip a training label.

    Args:
        quote: a saved Quote instance.
        outcome: 'accepted' | 'rejected'.
        rejection_reason: free text, stored only for rejections.
        final_price: agreed price override; defaults to quote.total_amount.
        update_quote: also set quote.outcome/accepted_at/rejected_at.
        allow_flip: permit overwriting an existing opposite-outcome label.
    """
    try:
        from core.models import QuoteOutcome

        if outcome not in ('accepted', 'rejected'):
            return None

        existing = QuoteOutcome.objects.filter(quote=quote).first()
        if existing is not None:
            if existing.outcome == outcome:
                return existing
            if not allow_flip:
                logger.info(
                    'record_quote_outcome: refusing to flip quote %s label %s -> %s '
                    '(pass allow_flip=True to correct deliberately)',
                    quote.id, existing.outcome, outcome,
                )
                return existing

        rejection_reason = rejection_reason if outcome == 'rejected' else ''

        # Snapshots below must reflect what was knowable BEFORE this outcome,
        # so compute them before the quote's own outcome field changes.
        hist_rate = _historical_acceptance_rate(quote)
        client_tier = _client_tier_for(quote)

        if update_quote:
            quote.outcome = outcome
            quote.rejection_reason = rejection_reason
            if outcome == 'accepted':
                quote.accepted_at = timezone.now()
                quote.rejected_at = None
            else:
                quote.rejected_at = timezone.now()
                quote.accepted_at = None
            quote.save(update_fields=[
                'outcome', 'rejection_reason', 'accepted_at', 'rejected_at', 'updated_at',
            ])

        final_price_val = Decimal(str(final_price)) if final_price else (quote.total_amount or Decimal('0'))
        direct_cost = (
            (quote.fuel_surcharge or 0) + (quote.toll_charges or 0)
            + (quote.driver_allowance or 0) + (quote.additional_charges or 0)
        )
        margin_pct = (
            (final_price_val - direct_cost) / final_price_val * 100
            if final_price_val > 0 else Decimal('0')
        )

        market_rate = None
        market_source = ''
        try:
            from core.services.lane_benchmark import resolve_market_rate
            # exclude_quote_id: this quote's status is already ACCEPTED when we
            # run, so without the exclusion its own price would sit inside its
            # own benchmark — biasing accepted rows' price_ratio toward 1.0.
            rate, src = resolve_market_rate(
                quote.origin, quote.destination, quote.vehicle_type or None,
                company=quote.company, exclude_quote_id=quote.id,
            )
            if rate and rate > 0:
                market_rate, market_source = Decimal(str(round(rate, 2))), src
        except Exception as exc:
            logger.warning('outcome capture: market rate resolve failed: %s', exc)

        route_popularity = None
        try:
            from core.services.margin_optimizer import _route_popularity
            route_popularity = Decimal(str(round(
                _route_popularity(quote.origin, quote.destination), 4)))
        except Exception as exc:
            logger.warning('outcome capture: route popularity failed: %s', exc)

        price_ratio = None
        if market_rate and market_rate > 0 and final_price_val > 0:
            price_ratio = (final_price_val / market_rate).quantize(Decimal('0.0001'))

        days_until_departure = None
        if quote.pickup_date and quote.created_at:
            days_until_departure = max(0, (quote.pickup_date - quote.created_at.date()).days)

        created = quote.created_at or timezone.now()

        record, _ = QuoteOutcome.objects.update_or_create(
            quote=quote,
            defaults={
                'company': quote.company,
                'outcome': outcome,
                'rejection_reason': rejection_reason,
                'final_price': final_price_val,
                'margin_pct': margin_pct,
                'distance_km': quote.distance,
                'vehicle_type': quote.vehicle_type,
                'origin': quote.origin,
                'destination': quote.destination,
                'weight_kg': quote.weight,
                'client_tier': client_tier,
                'fuel_price': quote.fuel_price_at_creation,
                'market_rate_at_outcome': market_rate,
                'market_rate_source': market_source,
                'price_ratio': price_ratio,
                'route_popularity': route_popularity,
                'days_until_departure': days_until_departure,
                'quote_month': created.month,
                'quote_dow': created.weekday(),
                'historical_acceptance_rate': hist_rate,
            },
        )
        return record
    except Exception as exc:
        logger.exception('record_quote_outcome failed for quote %s: %s',
                         getattr(quote, 'id', '?'), exc)
        return None
