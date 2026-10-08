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
    return {'total_tonnes': float(total), 'booked_tonnes': float(booked),
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
        text += f' (minimum {t(b["min_tonnes"])}; {t(b["tonnes"])} delivered)'
    if b['awaiting_weighbridge']:
        text += ' (planned tonnes, awaiting the weighbridge)'
    return {'description': text, 'quantity': Decimal(str(b['billable_tonnes'])),
            'unit_price': Decimal(str(b['rate_per_tonne']))}


def refresh_load_amount(load, save=True):
    """Keep a per-tonne load's total (and its draft invoice) on rate × billed
    tonnes after its tonnes change. Issued invoices are never touched."""
    from core.models import Invoice, Load
    b = load_billing(load)
    if b is None:
        return None
    if Invoice.objects.filter(load=load).exclude(status='DRAFT').exists():
        return b    # issued: corrected with a credit note, never re-priced here
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


class CallOffError(ValueError):
    pass


def call_off_tonnes(quote, requested=None):
    """Planned tonnes for the next load of a per-tonne quote. One consignment:
    its tonnes_per_load (once). Volume: the requested tonnes (default the
    planned load size), never more than what remains."""
    if quote.total_tonnes is None:
        if quote.loads.exists():
            raise CallOffError('Quote already converted')
        tonnes = quote_load_size(quote) or Decimal('0')
        if requested not in (None, ''):
            tonnes = Decimal(str(requested))
        if tonnes <= 0:
            raise CallOffError('Enter the tonnes for this load.')
        return tonnes, None
    status = contract_status(quote)
    remaining = Decimal(str(status['remaining_tonnes']))
    if remaining <= 0:
        raise CallOffError('Every tonne on this contract is booked.')
    if requested not in (None, ''):
        try:
            tonnes = Decimal(str(requested))
        except Exception:
            raise CallOffError('tonnes must be a number.')
        if tonnes <= 0:
            raise CallOffError('Enter the tonnes for this load.')
        if tonnes > remaining:
            raise CallOffError(f'Only {float(remaining):g} t is left on this contract.')
    else:
        size = quote_load_size(quote) or remaining
        tonnes = min(size, remaining)
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
