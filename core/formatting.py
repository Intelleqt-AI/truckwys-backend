"""en-ZA figures for text people read: notifications, alerts, emails, PDFs.

Same output as the frontend's src/lib/formatters.ts: space-grouped thousands
(non-breaking, so "R 20 505,65" never wraps), decimal comma, a true minus.
Not for machine-read text (LLM prompts, source matching), which keeps its own
format.
"""
from decimal import Decimal, InvalidOperation

NBSP = ' '
MINUS = '−'


def _to_float(value):
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def format_number(value, decimals=0, minus=MINUS) -> str:
    """'20 506' / '31,9'. Missing or unparseable values read as 0."""
    n = _to_float(value)
    body = f'{abs(n):,.{decimals}f}'.replace(',', '\0').replace('.', ',').replace('\0', NBSP)
    negative = n < 0 and body.strip('0,' + NBSP) != ''
    return f'{minus if negative else ""}{body}'


def format_zar(value, decimals=2, minus=MINUS) -> str:
    """'R 20 505,65' (decimals=2) or 'R 20 506' (decimals=0).

    minus='-' for PDFs: the standard PDF fonts have no U+2212 glyph."""
    body = format_number(value, decimals, minus)
    sign = ''
    if body.startswith(minus):
        sign, body = minus, body[len(minus):]
    return f'{sign}R{NBSP}{body}'
