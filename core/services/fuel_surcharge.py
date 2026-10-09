"""Fuel price clause on quotes and the fuel price adjustment on invoices.

The clause (company setting, default 5%): the quote states the official zone
price it was priced on; if the official price in force on the trip date has
moved by MORE than the threshold, the fuel part of the price changes by the
same amount, up or down:

    adjustment = cents(fuel_litres × (official on trip date − official at pricing))

applied only when |official on trip − official at pricing| / official at
pricing × 100 > threshold. Basis = the OFFICIAL zone price at pricing
(Quote.fuel_official_at_pricing, QUOTE-RULES §9) for the fuel the quote was
priced on (diesel / petrol 95 / petrol 93), even when the quote used the
fleet's own price: the clause is a promise to the customer about a public
figure. Litres = Quote.fuel_litres (all legs, incl. an empty return).

The clause is stamped on the quote when it is sent (QuoteFuelClause); only a
stamped clause adjusts an invoice, so a customer is never charged under terms
they weren't shown. Invoicing calls invoice_adjustment_for_load() (the one
hook, see core.services.invoicing.create_invoice_for_load).
"""
import logging
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

logger = logging.getLogger(__name__)
SAST = ZoneInfo('Africa/Johannesburg')


def _product_label(product):
    return 'diesel' if product == 'diesel' else f"petrol {product.split('_')[1]}"


def _zone_label(zone):
    return 'coastal' if str(zone or '').upper() == 'COASTAL' else 'inland'


def _fmt_pct(v):
    from core.services.quote_costing import fmt_num
    v = float(v)
    return f'{fmt_num(v, 0 if v == int(v) else 1)}%'


def _product_for(quote):
    from core.services.quote_snapshot import quote_fuel_product
    product = quote_fuel_product(quote)
    if product.startswith('petrol') and (quote.fuel_zone or '').upper() == 'COASTAL':
        product = 'petrol_95'                 # 93 is not sold at the coast
    return product


def clause_terms(quote, settings=None):
    """The clause this quote carries (stamped) or would carry if sent now
    (company setting), or None. {threshold_pct, basis_price, basis_effective_from,
    zone, product, litres, stamped}."""
    from core.models import QuoteFuelClause
    try:
        c = quote.fuel_clause
    except QuoteFuelClause.DoesNotExist:
        c = None
    if c is not None:
        return {'threshold_pct': float(c.threshold_pct), 'basis_price': float(c.basis_price),
                'basis_effective_from': c.basis_effective_from, 'zone': c.zone, 'product': c.product,
                'litres': float(c.litres), 'stamped': True, 'text': c.text}
    if quote.status not in ('DRAFT', 'SENT') or quote.company_id is None:
        return None
    if quote.status == 'SENT':
        return None                            # sent without a clause: never retro-fitted
    from core.services.quote_automation import get_settings
    s = settings or get_settings(quote.company)
    if s is None or not s.fuel_surcharge_enabled:
        return None
    return _terms_from_snapshot(quote, s.fuel_surcharge_threshold_pct)


def _terms_from_snapshot(quote, threshold_pct):
    basis = quote.fuel_official_at_pricing
    litres = quote.fuel_litres
    if basis is None or litres is None or float(basis) <= 0 or float(litres) <= 0:
        return None
    snap = (quote.costing_snapshot or {}).get('diesel') or {}
    if str(snap.get('fuel_type') or 'Diesel').lower() == 'electric':
        return None
    terms = {'threshold_pct': float(threshold_pct), 'basis_price': float(basis),
             'basis_effective_from': quote.fuel_effective_from,
             'zone': (quote.fuel_zone or 'INLAND').upper(), 'product': _product_for(quote),
             'litres': float(litres), 'stamped': False}
    terms['text'] = clause_text(terms, quote)
    return terms


def clause_text(terms, quote=None):
    """The PDF / share-page sentence. When the quote was priced on the
    official price the reference line already names it, so the sentence just
    states the rule; otherwise it names the official basis too."""
    from core.services.quote_costing import fmt_rand, sa_date
    fuel = _product_label(terms['product'])
    pct = _fmt_pct(terms['threshold_pct'])
    source = getattr(quote, 'fuel_price_source', '') if quote is not None else ''
    if source == 'official':
        return (f'If the official price moves more than {pct} before the trip, '
                f'the fuel part of this quote changes by the same amount.')
    when = sa_date(terms.get('basis_effective_from'))
    basis = (f'the official {_zone_label(terms["zone"])} {fuel} price '
             f'({fmt_rand(terms["basis_price"], 2)}/L' + (f' on {when}' if when else '') + ')')
    return (f'If {basis} moves more than {pct} before the trip, '
            f'the fuel part of this quote changes by the same amount.')


def stamp_clause(quote, now=None):
    """Called when a quote is sent: store the clause it went out with (if the
    company has the clause on and the quote has an official basis). Idempotent
    for one send; a re-send re-stamps with the current settings."""
    from core.models import QuoteFuelClause
    from core.services.quote_automation import get_settings
    now = now or timezone.now()
    s = get_settings(quote.company) if quote.company_id else None
    if s is None or not s.fuel_surcharge_enabled:
        QuoteFuelClause.objects.filter(quote=quote).delete()
        return None
    terms = _terms_from_snapshot(quote, s.fuel_surcharge_threshold_pct)
    if terms is None:
        QuoteFuelClause.objects.filter(quote=quote).delete()
        return None
    obj, _ = QuoteFuelClause.objects.update_or_create(quote=quote, defaults={
        'threshold_pct': Decimal(str(terms['threshold_pct'])),
        'basis_price': Decimal(str(terms['basis_price'])).quantize(Decimal('0.0001')),
        'basis_effective_from': terms['basis_effective_from'],
        'zone': terms['zone'], 'product': terms['product'],
        'litres': Decimal(str(terms['litres'])).quantize(Decimal('0.001')),
        'text': terms['text'], 'stamped_at': now,
    })
    quote.fuel_clause = obj
    return obj


def _trip_moment(d):
    """Midday SAST on the trip date: the price change happens at 00:01."""
    if isinstance(d, datetime):
        d = d.astimezone(SAST).date()
    return datetime.combine(d, time(12, 0), tzinfo=SAST)


def trip_date_for(quote, load=None):
    """(date, source): the load's pickup date, else the quote's collection
    date, else today (SAST)."""
    if load is not None and getattr(load, 'pickup_date', None):
        pd = load.pickup_date
        return (pd.astimezone(SAST).date() if isinstance(pd, datetime) else pd), 'load_pickup'
    if getattr(quote, 'pickup_date', None):
        return quote.pickup_date, 'quote_pickup'
    return timezone.now().astimezone(SAST).date(), 'today'


def litres_for(quote, terms, load=None):
    """The litres the adjustment applies to. Per load: the clause's litres
    (all legs of the quoted trip). Per tonne: the quote's litres per billed
    tonne (snapshot) times the tonnes this load is billed on, so a volume
    contract call-off of 30 t adjusts only its own share of the fuel."""
    litres = terms['litres']
    if getattr(quote, 'pricing_basis', 'per_load') != 'per_tonne':
        return litres
    from core.services.tonnage_jobs import load_billing, quote_load_size, quote_min_tonnes
    quoted = ((quote.costing_snapshot or {}).get('tonnage') or {}).get('billable_tonnes')
    if not quoted or float(quoted) <= 0:
        return litres
    if load is not None:
        billing = load_billing(load)
        if billing is None:
            return litres
        tonnes = float(billing['billable_tonnes'])
    elif quote.total_tonnes is not None:
        # A volume contract with no load yet: one planned load's share.
        size, minimum = quote_load_size(quote), quote_min_tonnes(quote)
        if size is None:
            return litres
        tonnes = float(max(size, minimum or 0))
    else:
        return litres
    return round(litres / float(quoted) * tonnes, 3)


def adjustment(quote, load=None, now=None):
    """The fuel price adjustment for a quote (or the load it became).

    {applies, reason, clause, stamped, product, zone, litres, price_at_pricing,
     price_on_trip, trip_date, trip_date_source, provisional, change_pct,
     threshold_pct, amount_zar, direction, description}
    reason: 'no_clause' | 'no_official_price' | 'within_threshold' | 'applies'.
    provisional: the trip date is in the future, so the price on the trip
    date isn't known yet (today's official price is shown)."""
    from core.services.fuel_price import price_in_force
    from core.services.quote_costing import cents, fmt_rand
    now = now or timezone.now()
    terms = clause_terms(quote)
    out = {'applies': False, 'reason': 'no_clause', 'clause': None, 'stamped': False,
           'amount_zar': None, 'direction': None, 'description': None}
    if terms is None:
        return out
    if not terms['stamped'] and (load is not None or (quote.pk and quote.loads.exists())):
        # Booked straight from a draft (one-tap booking): the customer never saw
        # a clause, so the invoice never carries one; don't show one either.
        return out
    trip, trip_source = trip_date_for(quote, load)
    today = now.astimezone(SAST).date()
    at = min(_trip_moment(trip), now) if trip > today else _trip_moment(trip)
    rec = price_in_force(terms['zone'], at, product=terms['product'])
    out.update({
        'clause': terms.get('text'), 'stamped': terms['stamped'],
        'product': terms['product'], 'zone': terms['zone'], 'litres': litres_for(quote, terms, load),
        'price_at_pricing': terms['basis_price'], 'threshold_pct': terms['threshold_pct'],
        'trip_date': trip.isoformat(), 'trip_date_source': trip_source, 'provisional': trip > today,
        'price_on_trip': rec['price'] if rec else None, 'change_pct': None,
    })
    if rec is None:
        out['reason'] = 'no_official_price'
        return out
    basis, on_trip = terms['basis_price'], rec['price']
    change_pct = (on_trip - basis) / basis * 100
    out['change_pct'] = round(change_pct, 2)
    if abs(change_pct) <= terms['threshold_pct'] + 1e-9:
        out['reason'] = 'within_threshold'
        return out
    amount = cents(out['litres'] * (on_trip - basis))
    if amount == 0:
        out['reason'] = 'within_threshold'
        return out
    out.update({
        'applies': True, 'reason': 'applies', 'amount_zar': amount,
        'direction': 'up' if amount > 0 else 'down',
        'description': (f'Fuel price adjustment ({_product_label(terms["product"])} '
                        f'{fmt_rand(basis, 2)} → {fmt_rand(on_trip, 2)}/L)'),
    })
    return out


def invoice_adjustment_for_load(load, now=None):
    """THE invoicing hook. For a load made from a quote with a stamped clause,
    the adjustment to put on its invoice, or None:
      {'amount': Decimal (signed), 'description': str, 'detail': adjustment()}
    Up = an extra FUEL_SURCHARGE line; down = a discount on the freight line
    (invoice lines can't be negative). Never raises."""
    quote = getattr(load, 'quote', None)
    if quote is None:
        return None
    try:
        from core.models import QuoteFuelClause
        if not QuoteFuelClause.objects.filter(quote=quote).exists():
            return None
        detail = adjustment(quote, load=load, now=now)
    except Exception:
        logger.exception('fuel adjustment for load %s failed', getattr(load, 'pk', None))
        return None
    if not detail['applies'] or detail['provisional']:
        return None
    return {'amount': Decimal(str(detail['amount_zar'])).quantize(Decimal('0.01')),
            'description': detail['description'], 'detail': detail}


def apply_to_invoice_lines(load, lines, now=None):
    """Add the adjustment to the raw invoice lines create_invoice_for_load
    builds (lines[0] = the freight line). Returns the adjustment applied or
    None. Never raises: an invoice is never blocked by its adjustment."""
    try:
        return _apply(load, lines, now)
    except Exception:
        logger.exception('fuel adjustment on invoice lines for load %s failed', getattr(load, 'pk', None))
        return None


def _apply(load, lines, now):
    adj = invoice_adjustment_for_load(load, now=now)
    if adj is None or not lines:
        return None
    from core import revenue_types
    freight = lines[0]
    if adj['amount'] > 0:
        lines.append({
            'description': adj['description'], 'quantity': 1, 'unit_price': adj['amount'],
            'tax_code': freight.get('tax_code'), 'revenue_type': revenue_types.FUEL_SURCHARGE,
            'load': freight.get('load'),
        })
    else:
        # discount_amount is on the whole line (quantity x unit price: a
        # per-tonne line has quantity = billed tonnes).
        gross = Decimal(str(freight['unit_price'])) * Decimal(str(freight.get('quantity') or 1))
        credit = min(-adj['amount'], gross.quantize(Decimal('0.01')))
        freight['discount_amount'] = credit
        freight['description'] = f"{freight['description']} less {adj['description'][0].lower()}{adj['description'][1:]}"
    return adj
