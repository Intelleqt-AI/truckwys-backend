"""VAT tax codes and the one rounding rule every TruckWys money figure uses.

Pure Python (no model imports) so models, services, serializers, migrations
and the golden-dataset tests can all share it.

Tax codes (South Africa, VAT Act 89 of 1991):
  STANDARD    15%  standard-rated supply
  ZERO_RATED   0%  zero-rated supply (s11: e.g. international transport of
                    goods, exports, diesel/petrol). Still a VAT supply and is
                    reported on the VAT201 at 0%.
  EXEMPT       0%  exempt supply (s12: e.g. domestic passenger transport).
                    Not a VAT supply; no input VAT may be claimed against it.
  NO_VAT       0%  the seller is not a VAT vendor (or, on an expense, the
                    supplier did not issue a valid tax invoice). Outside VAT.

Rounding rule (documented in docs/foundation/SPEC.md):
  per line:  gross    = quantity x unit_price           (unrounded)
             discount = discount_amount, or gross x discount_percent / 100
             net      = round2(gross - discount)        (discount BEFORE VAT)
             vat      = round2(net x rate)
             total    = net + vat
  document:  subtotal = sum(line net); vat = sum(line vat); total = sum(line total)
  round2 is ROUND_HALF_UP to the cent. All arithmetic is Decimal, never float.

Rounding per line (not on the document total) matches the default of Xero
("Round tax per line") and keeps every credit note line an exact mirror of
the invoice line it reverses, so a full credit always nets to R0.00.

Expenses are captured GROSS (the amount on the receipt, incl. VAT). Input VAT
on a standard-rated receipt is the tax fraction: round2(gross x 15/115).
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal('0.01')
ZERO = Decimal('0.00')

STANDARD = 'STANDARD'
ZERO_RATED = 'ZERO_RATED'
EXEMPT = 'EXEMPT'
NO_VAT = 'NO_VAT'

TAX_CODE_CHOICES = [
    (STANDARD, 'Standard rate (15%)'),
    (ZERO_RATED, 'Zero-rated (0%)'),
    (EXEMPT, 'Exempt'),
    (NO_VAT, 'No VAT (not a VAT vendor)'),
]
TAX_CODES = [c for c, _ in TAX_CODE_CHOICES]

# Standard rate history. The 2025 budget proposal to move to 15.5% was
# withdrawn, so 15% has applied since 1 April 2018. A future change is one
# new row here; documents are taxed at the rate in force on their issue date.
STANDARD_RATE_HISTORY = [
    (date(2018, 4, 1), Decimal('0.15')),
]
_PRE_2018_RATE = Decimal('0.14')


def round2(value) -> Decimal:
    """ROUND_HALF_UP to the cent."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def to_decimal(value, default=ZERO) -> Decimal:
    if value is None or value == '':
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Floats are converted via str so 0.1 stays 0.1, not 0.1000000000000000055.
        value = repr(value)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Not a valid number: {value!r}')


def rate_for(tax_code: str, on_date: date | None = None) -> Decimal:
    """Fractional VAT rate (0.15) for a code on a date."""
    if tax_code not in TAX_CODES:
        raise ValueError(f'Unknown tax code: {tax_code!r}')
    if tax_code != STANDARD:
        return Decimal('0')
    on_date = on_date or date.today()
    rate = _PRE_2018_RATE
    for start, r in STANDARD_RATE_HISTORY:
        if on_date >= start:
            rate = r
    return rate


def rate_percent(tax_code: str, on_date: date | None = None) -> Decimal:
    return (rate_for(tax_code, on_date) * 100).quantize(CENT)


def compute_line(quantity, unit_price, tax_code, *, discount_amount=None,
                 discount_percent=None, on_date=None) -> dict:
    """Apply the rounding rule to one line. Returns Decimals:
    gross, discount, net, vat, total, rate (fraction)."""
    qty = to_decimal(quantity, Decimal('1'))
    price = to_decimal(unit_price)
    gross = qty * price
    if discount_amount not in (None, ''):
        discount = to_decimal(discount_amount)
    elif discount_percent not in (None, ''):
        pct = to_decimal(discount_percent)
        if pct < 0 or pct > 100:
            raise ValueError('Discount percent must be between 0 and 100')
        discount = gross * pct / Decimal('100')
    else:
        discount = ZERO
    if discount < 0:
        raise ValueError('Discount cannot be negative')
    net = round2(gross - discount)
    discount = round2(gross) - net if discount else ZERO
    rate = rate_for(tax_code, on_date)
    vat = round2(net * rate)
    return {
        'gross': round2(gross), 'discount': discount, 'net': net,
        'vat': vat, 'total': net + vat, 'rate': rate,
    }


def vat_fraction_of_gross(gross, tax_code, on_date=None) -> Decimal:
    """Input VAT inside a VAT-inclusive amount: gross x r / (1 + r)."""
    rate = rate_for(tax_code, on_date)
    if not rate:
        return ZERO
    return round2(to_decimal(gross) * rate / (1 + rate))
