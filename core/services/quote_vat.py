"""VAT on a quote (or the order/Load it becomes), as the customer sees it.

Works on anything with total_amount, company, is_international and
created_at: Quote and Load both qualify, so the quote and order pages show
the same figures as the customer documents.

Quote.total_amount is the price excl. VAT (what the quote builder prices).
Everything customer-facing (the PDF, the share and acceptance emails, the
online quote page and the WhatsApp text built from it) shows that price,
the VAT on it, and the total incl. VAT, from this one function, so they all
agree to the cent with each other and with the invoice the quote becomes:
same rate table and rounding as invoices (core.tax_codes), standard rate on
the quote's date.

Domestic transport is standard-rated (15%). International transport of
goods (Quote.is_international: the route leaves South Africa) is zero-rated
under s11(2)(a): the VAT line still shows, as 0%. A company that isn't
VAT-registered can't charge VAT: no VAT line, and the total is the price.
"""
from datetime import date

from core import tax_codes


def quote_vat(quote) -> dict:
    company = getattr(quote, 'company', None)
    vat_registered = getattr(company, 'vat_registered', True) if company is not None else True
    subtotal = tax_codes.round2(quote.total_amount or 0)
    if not vat_registered:
        return {'vat_registered': False, 'tax_code': tax_codes.NO_VAT, 'zero_rated': False, 'rate_percent': None, 'subtotal': subtotal,
                'vat': tax_codes.ZERO, 'total': subtotal}
    created = getattr(quote, 'created_at', None)
    on_date = created.date() if created else date.today()
    code = tax_codes.ZERO_RATED if getattr(quote, 'is_international', False) else tax_codes.STANDARD
    rate = tax_codes.rate_for(code, on_date)
    vat = tax_codes.round2(subtotal * rate)
    return {'vat_registered': True, 'tax_code': code, 'zero_rated': code == tax_codes.ZERO_RATED,
            'rate_percent': tax_codes.rate_percent(code, on_date),
            'subtotal': subtotal, 'vat': vat, 'total': subtotal + vat}


def vat_label(summary: dict) -> str:
    """'VAT (15%)', or 'VAT 0% (zero-rated international transport)'."""
    pct = summary['rate_percent']
    if pct is None:
        return 'VAT'
    if summary.get('zero_rated'):
        return 'VAT 0% (zero-rated international transport)'
    return f"VAT ({pct.normalize():f}%)"


def public_fields(summary: dict) -> dict:
    """The breakdown for API responses (strings, like total_amount)."""
    return {
        'vat_registered': summary['vat_registered'],
        'zero_rated': summary.get('zero_rated', False),
        'vat_label': vat_label(summary),
        'vat_rate_percent': str(summary['rate_percent']) if summary['rate_percent'] is not None else None,
        'subtotal_excl_vat': str(summary['subtotal']),
        'vat_amount': str(summary['vat']),
        'total_incl_vat': str(summary['total']),
    }


def sum_incl_vat(queryset):
    """Total incl. VAT over many quotes or loads, each rounded exactly as
    quote_vat() rounds it, so a list total equals the sum of its rows. Reads
    only the four columns it needs."""
    from types import SimpleNamespace
    total = tax_codes.ZERO
    # prefetch_related(None): the list querysets prefetch loads, which
    # .iterator() refuses, and these four columns don't need it.
    rows = queryset.prefetch_related(None).values_list('total_amount', 'is_international', 'created_at', 'company__vat_registered')
    for amount, international, created, vat_registered in rows.iterator():
        row = SimpleNamespace(total_amount=amount, is_international=international, created_at=created,
                              company=SimpleNamespace(vat_registered=vat_registered if vat_registered is not None else True))
        total += quote_vat(row)['total']
    return total
