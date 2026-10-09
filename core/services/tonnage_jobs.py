"""Tonnage quotes as jobs and invoices (QUOTE-RULES "Tonnage quotes").

* A per-tonne quote for one consignment converts to ONE Load carrying the
  rate per tonne, the minimum tonnes and the planned tonnes.
* A volume contract (per-tonne quote with total_tonnes) is the Quote itself:
  each call-off is a Load referencing it (Load.quote), drawing down the
  remaining tonnes (actual weighbridge tonnes when known, else planned).
* Invoice line = rate × max(tonnes, minimum) where tonnes = the load's actual
  (weighbridge) tonnes, else its planned tonnes flagged "Awaiting weighbridge
  tonnes" (the invoice stays a draft and is never auto-emailed).
"""
from decimal import ROUND_HALF_UP, Decimal

AWAITING_WEIGHBRIDGE = 'Awaiting weighbridge tonnes'


def _d(v):
    return None if v is None else Decimal(str(v))


def _money(v):
    return Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def quote_min_tonnes(quote):
    """The quote's minimum per load: typed, else the basis truck's planned
    load from the pricing snapshot."""
    if quote.min_tonnes_per_load is not None:
        return Decimal(quote.min_tonnes_per_load)
    t = ((quote.costing_snapshot or {}).get('tonnage') or {}).get('min_tonnes_per_load')
    return _d(t)


def quote_load_size(quote):
    """Planned tonnes per call-off: tonnes_per_load, else the basis truck's
    planned load."""
    if quote.tonnes_per_load is not None:
        return Decimal(quote.tonnes_per_load)
    snap = (quote.costing_snapshot or {}).get('tonnage') or {}
    basis = next((t for t in snap.get('trucks') or [] if t.get('is_basis')), None)
    return _d((basis or {}).get('tonnes_per_load'))


def contract_status(quote):
    """{total_tonnes, booked_tonnes, remaining_tonnes, loads_booked,
    loads_planned, tonnes_per_load} for a volume contract."""
    total = Decimal(quote.total_tonnes or 0)
    booked = delivered = Decimal('0')
    rows = []
    for load in quote.loads.exclude(status='CANCELLED').order_by('id'):
        t = Decimal(load.actual_tonnes if load.actual_tonnes is not None else (load.planned_tonnes or 0))
        booked += t
        if load.actual_tonnes is not None:
            delivered += Decimal(load.actual_tonnes)
        rows.append({'id': load.id, 'load_number': load.load_number, 'status': load.status,
                     'pickup_date': load.pickup_date.isoformat() if load.pickup_date else None,
                     'planned_tonnes': float(load.planned_tonnes) if load.planned_tonnes is not None else None,
                     'actual_tonnes': float(load.actual_tonnes) if load.actual_tonnes is not None else None,
                     'weighbridge_slip': load.weighbridge_slip or None, 'total_amount': float(load.total_amount)})
    size = quote_load_size(quote)
    cap = max_load_tonnes(quote)
    return {'total_tonnes': float(total), 'booked_tonnes': float(booked),
            # The most one call-off can carry: the largest eligible truck's payload.
            'max_tonnes_per_load': float(cap) if cap is not None else None,
            # Weighbridge tonnes on record (loads with actual tonnes).
            'delivered_tonnes': float(delivered),
            'remaining_tonnes': float(max(total - booked, Decimal('0'))), 'loads_booked': len(rows),
            'loads_planned': quote.loads_planned, 'tonnes_per_load': float(size) if size is not None else None,
            'contract_start': quote.contract_start.isoformat() if quote.contract_start else None,
            'contract_end': quote.contract_end.isoformat() if quote.contract_end else None,
            'loads': rows}


def load_billing(load):
    """How a per-tonne load is invoiced: {tonnes, tonnes_source actual|planned,
    min_tonnes, billable_tonnes, rate_per_tonne, amount, awaiting_weighbridge,
    flag}. None for a per-load load."""
    if getattr(load, 'pricing_basis', 'per_load') != 'per_tonne' or load.rate_per_tonne is None:
        return None
    actual = load.actual_tonnes
    tonnes = Decimal(actual) if actual is not None else Decimal(load.planned_tonnes or 0)
    minimum = Decimal(load.min_tonnes or 0)
    billable = max(tonnes, minimum)
    rate = Decimal(load.rate_per_tonne)
    return {'tonnes': float(tonnes), 'tonnes_source': 'actual' if actual is not None else 'planned',
            'min_tonnes': float(minimum) if load.min_tonnes is not None else None,
            'billable_tonnes': float(billable), 'rate_per_tonne': float(rate),
            'amount': float(_money(rate * billable)), 'awaiting_weighbridge': actual is None,
            'flag': AWAITING_WEIGHBRIDGE if actual is None else None}


def tonnes_txt(v):
    """'30 t', '27,5 t', '31,24 t' (SA format, up to 3 decimals, no trailing zeros)."""
    from core.services.quote_costing import fmt_num
    txt = fmt_num(float(v), 3)
    if ',' in txt:
        txt = txt.rstrip('0').rstrip(',')
    return f'{txt} t'


def invoice_line_for_load(load, description):
    """The invoice line for a per-tonne load (quantity = billed tonnes)."""
    from core.services.quote_costing import fmt_rand
    b = load_billing(load)
    t = tonnes_txt
    rate = b['rate_per_tonne']
    text = f'{description}: {t(b["billable_tonnes"])} at {fmt_rand(rate, 0 if float(rate).is_integer() else 2)}/t'
    if b['min_tonnes'] and b['billable_tonnes'] > b['tonnes']:
        text += (f' (minimum {t(b["min_tonnes"])}; {t(b["tonnes"])} '
                 f'{"delivered" if b["tonnes_source"] == "actual" else "planned"})')
    if b['awaiting_weighbridge']:
        text += ' (planned tonnes, awaiting the weighbridge)'
    slip = (getattr(load, 'weighbridge_slip', '') or '').strip()
    if slip and not b['awaiting_weighbridge']:
        text += f'. Weighbridge slip {slip}'
    return {'description': text, 'quantity': Decimal(str(b['billable_tonnes'])),
            'unit_price': Decimal(str(b['rate_per_tonne']))}


def refresh_load_amount(load, save=True):
    """Keep a per-tonne load's total (and its draft invoice) on rate × billed
    tonnes after its tonnes change. Issued invoices are never touched."""
    from core.models import Invoice, Load
    b = load_billing(load)
    if b is None or load.status == 'CANCELLED':
        return None
    issued = (Invoice.objects.filter(load=load).exclude(status__in=('DRAFT', 'VOID', 'CANCELLED'))
              .order_by('-id').first())
    if issued is not None:
        # Issued: never re-priced here (a credit note or a new invoice
        # corrects it); flagged on the load so it is seen.
        flag_weighed_after_invoicing(load, issued, b)
        return b
    amount = _money(b['amount'])
    if save:
        Load.objects.filter(pk=load.pk).update(total_amount=amount, rate=amount)
    load.total_amount = load.rate = amount
    inv = Invoice.objects.filter(load=load, status='DRAFT').first()
    if inv is not None:
        from core.services.invoice_lines import apply_lines
        from core.services.invoicing import invoice_lines_for_load
        # The same lines a fresh invoice gets (incl. a fuel price adjustment,
        # which follows the billed tonnes).
        apply_lines(inv, invoice_lines_for_load(load, inv.company))
        note = f'{AWAITING_WEIGHBRIDGE}: invoiced on planned tonnes.'
        notes = (inv.notes or '').replace(note, '').strip()
        if b['awaiting_weighbridge']:
            notes = f'{notes}\n{note}'.strip()
        if notes != (inv.notes or ''):
            Invoice.objects.filter(pk=inv.pk).update(notes=notes)
    return b


def flag_weighed_after_invoicing(load, invoice, billing):
    from django.utils import timezone
    from core.models import ActivityEvent, Load
    invoiced = (invoice.total_amount or Decimal('0')) - (invoice.vat_amount or Decimal('0'))
    weighed = _money(billing['amount'])
    # The issued invoice may carry a fuel price adjustment (quote follow-ups);
    # compare like with like: the weighed amount plus the adjustment those
    # tonnes would carry. Nothing is added to the issued invoice itself.
    from core.services.fuel_surcharge import invoice_adjustment_for_load
    adj = invoice_adjustment_for_load(load)
    if adj is not None:
        weighed = max(weighed + adj['amount'], Decimal('0'))
    if abs(invoiced - weighed) < Decimal('0.01'):
        # Back in line with the invoice: drop an earlier weighed-after-invoicing flag.
        current = getattr(load, 'invoice_mismatch', None)
        if isinstance(current, dict) and current.get('code') == 'weighed_after_invoicing':
            Load.objects.filter(pk=load.pk).update(invoice_mismatch={})
            load.invoice_mismatch = {}
        return None
    flag = {
        'code': 'weighed_after_invoicing', 'invoice_id': invoice.pk, 'invoice_number': invoice.invoice_number,
        'invoice_status': invoice.status, 'invoice_excl_vat': float(invoiced), 'load_total_excl_vat': float(weighed),
        'difference': float(weighed - invoiced), 'tonnes': billing['tonnes'],
        'billable_tonnes': billing['billable_tonnes'], 'source': getattr(load, 'actual_tonnes_source', '') or '',
        'detected_at': timezone.now().isoformat(),
        'title': 'Weighed after invoicing',
        'detail': 'The weighbridge tonnes changed the amount; issue a credit note or a new invoice.',
    }
    if hasattr(load, 'invoice_mismatch'):
        Load.objects.filter(pk=load.pk).update(invoice_mismatch=flag)
        load.invoice_mismatch = flag
    ActivityEvent.objects.create(event_type='load', title=f'Weighed after invoicing: {load.load_number}'[:200],
                                 entity_id=load.pk, entity_type='Load', company=load.company, metadata=flag)
    return flag


class CallOffError(ValueError):
    """A call-off the server refuses. `invalid`: the tonnes are not a usable
    number at all (the request itself is bad)."""

    def __init__(self, message, invalid=False):
        super().__init__(message)
        self.invalid = invalid


MIN_CALL_OFF = Decimal('0.1')


def parse_tonnes(raw):
    """Requested tonnes -> Decimal: finite, more than 0, at most 3 decimals
    ("28,5" and "28.5" alike). Anything else is a CallOffError (400)."""
    from decimal import InvalidOperation
    if isinstance(raw, bool):
        raise CallOffError('Enter the tonnes as a number, e.g. 28,5.', invalid=True)
    try:
        d = Decimal(str(raw).strip().replace(' ', '').replace(',', '.'))
    except (InvalidOperation, ValueError, TypeError):
        raise CallOffError('Enter the tonnes as a number, e.g. 28,5.', invalid=True)
    if not d.is_finite():
        raise CallOffError('Enter the tonnes as a number, e.g. 28,5.', invalid=True)
    if d <= 0:
        raise CallOffError('Enter the tonnes for this load.', invalid=True)
    if d.as_tuple().exponent < -3:
        raise CallOffError('Tonnes take at most 3 decimals.', invalid=True)
    return d


def max_load_tonnes(quote):
    """The largest eligible truck's payload (tonnes) from the pricing
    snapshot, or None when unknown."""
    trucks = ((quote.costing_snapshot or {}).get('tonnage') or {}).get('trucks') or []
    caps = [Decimal(str(t['payload_t'])) for t in trucks if t.get('payload_t')]
    return max(caps) if caps else None


def call_off_tonnes(quote, requested=None):
    """Planned tonnes for the next load of a per-tonne quote.

    One consignment: its quoted tonnes, once; a figure may be sent but never
    above the quoted tonnes. Volume contract: the requested tonnes (default
    the planned load size), never more than what remains nor more than the
    largest eligible truck carries. Always at least 0,1 t. Raises CallOffError."""
    from core.services.quote_costing import fmt_num
    t = lambda d: (f'{fmt_num(float(d), 0)} t' if d == d.to_integral()
                   else f'{fmt_num(float(d), 3).rstrip("0").rstrip(",")} t')
    tonnes = parse_tonnes(requested) if requested not in (None, '') else None
    if quote.total_tonnes is None:
        if quote.loads.exists():
            raise CallOffError('Quote already converted')
        quoted = quote_load_size(quote)
        if tonnes is None:
            tonnes = quoted or Decimal('0')
        elif quoted is not None and tonnes > quoted:
            raise CallOffError(f'At most the quoted {t(quoted)} on this load.')
        if tonnes < MIN_CALL_OFF:
            raise CallOffError('A load is at least 0,1 t.')
        return tonnes, None
    status = contract_status(quote)
    remaining = Decimal(str(status['remaining_tonnes']))
    if remaining <= 0:
        raise CallOffError('Every tonne on this contract is booked.')
    cap = max_load_tonnes(quote)
    if tonnes is None:
        size = quote_load_size(quote) or remaining
        tonnes = min(size, remaining, *( [cap] if cap is not None else []))
    else:
        if tonnes > remaining:
            raise CallOffError(f'Only {t(remaining)} is left on this contract.')
        if cap is not None and tonnes > cap:
            raise CallOffError(f'At most {t(cap)} on one load (the largest truck).')
    if tonnes < MIN_CALL_OFF:
        raise CallOffError('A load is at least 0,1 t.')
    return tonnes, status


def tonnage_load_fields(quote, tonnes):
    """Load fields for a per-tonne call-off of `tonnes`."""
    minimum = quote_min_tonnes(quote)
    rate = Decimal(quote.rate_per_tonne)
    billable = max(tonnes, minimum or Decimal('0'))
    amount = _money(rate * billable)
    return {'pricing_basis': 'per_tonne', 'rate_per_tonne': rate, 'min_tonnes': minimum,
            'planned_tonnes': tonnes, 'weight': (tonnes * 1000).quantize(Decimal('0.01')),
            # Not itemised per load: one line, rate x tonnes.
            'rate': amount, 'fuel_surcharge': Decimal('0'), 'toll_charges': Decimal('0'),
            'driver_allowance': Decimal('0'), 'additional_charges': Decimal('0'), 'total_amount': amount}


def copy_tonnage_costing(quote, fields):
    """Trip-economics fields for a per-tonne call-off: one load on the truck
    the contract was priced on (the pricing snapshot's basis truck, a full
    load), so the job's margin shows a cost. costing_source 'quote'."""
    from core.services.trip_costing import SNAPSHOT_KEYS, copy_quote_costing
    out = copy_quote_costing(quote)
    one = ((quote.costing_snapshot or {}).get('tonnage') or {}).get('basis_load_costing') or {}
    if not one.get('lines'):
        out.update({'costing_snapshot': {}, 'cost_floor': None, 'quoted_cost_floor': None,
                    'quoted_margin_pct': None, 'quoted_price': fields.get('total_amount'), 'costing_source': ''})
        return out
    floor = one.get('floor')
    price = fields.get('total_amount')
    floor_d = Decimal(str(floor)).quantize(Decimal('0.01')) if floor is not None else None
    litres = (one.get('litres') or {}).get('total')
    margin = (((Decimal(price) - floor_d) / Decimal(price) * 100).quantize(Decimal('0.01'))
              if floor_d is not None and price else None)
    out.update({
        'costing_snapshot': {k: one.get(k) for k in SNAPSHOT_KEYS if k in one},
        'cost_floor': floor_d, 'quoted_cost_floor': floor_d, 'quoted_price': price, 'quoted_margin_pct': margin,
        'empty_return_assumed': (one.get('trip') or {}).get('empty_return_included'),
        'fuel_litres': Decimal(str(litres)).quantize(Decimal('0.001')) if litres is not None else None,
        'costing_source': 'quote',
    })
    return out
