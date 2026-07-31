"""Shared building blocks for notify_company() title/body copy.

Keeps every quote/load notification's customer/route/amount fragments
identical regardless of which of several call sites triggered the same
event — the old ad-hoc string-concatenation duplicated slightly differently
in each signal branch is why "Quote accepted" sent via the public customer
link included the total while the same event fired from the authenticated
staff UI didn't.

Also owns channel routing: which Android notification channel a push lands
in, by event prefix. Presentation/grouping only — orthogonal to
notification_prefs.EVENT_CATEGORY, which gates whether a push is sent at all.
"""


def customer_name(entity) -> str:
    """The customer's display name off a Quote/Load, or '' if none set."""
    cust = getattr(entity, 'customer', None)
    return (getattr(cust, 'name', '') or '') if cust else ''


def money(amount) -> str:
    """R-formatted amount, or '' if falsy — callers just skip the fragment."""
    if not amount:
        return ''
    return f'R{float(amount):,.0f}'


def quote_route(quote) -> str:
    """'{origin} → {destination}' using the short lane codes, or '' if either
    is blank. The full pickup/delivery address strings are too long for a
    push body — this only uses the short codes."""
    origin = getattr(quote, 'origin', '') or ''
    destination = getattr(quote, 'destination', '') or ''
    return f'{origin} → {destination}' if origin and destination else ''


def load_route(load) -> str:
    pickup = getattr(load, 'pickup_city', '') or ''
    delivery = getattr(load, 'delivery_city', '') or ''
    return f'{pickup} → {delivery}' if pickup and delivery else ''


def join_parts(*parts) -> str:
    """' · '-join whatever fragments are non-empty."""
    return ' · '.join(p for p in parts if p)


def truncate(text: str, limit: int = 60) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + '…'


def quote_accepted_copy(quote) -> tuple[str, str]:
    """Shared by the Quote post_save signal (the public customer-link path)
    and QuoteViewSet.update_status (the authenticated staff path) — the one
    event with two call sites, and exactly where today's inconsistency (the
    amount present in one, missing in the other) came from."""
    cust = customer_name(quote)
    ident = quote.quote_number or f'Quote {quote.id}'
    lead = f'{cust} accepted {ident}' if cust else f'{ident} accepted'
    body = join_parts(lead, quote_route(quote), money(quote.total_amount))
    return '🎉 Quote accepted!', body


def quote_declined_copy(quote) -> tuple[str, str]:
    """The customer's typed decline reason, when there is one — genuinely
    useful context that was captured (Quote.rejection_reason) but never
    surfaced anywhere. Truncated: a long free-text reason must not blow up
    the push body."""
    cust = customer_name(quote)
    ident = quote.quote_number or f'Quote {quote.id}'
    lead = f'{cust} declined {ident}' if cust else f'{ident} declined'
    reason = truncate(getattr(quote, 'rejection_reason', '') or '')
    body = join_parts(lead, f'"{reason}"' if reason else '')
    return 'Quote declined', body


# Prefix -> Android notification channel. Checked in order; first match wins.
# Presentation/grouping only (which channel a push lands in on the device) —
# does not gate whether the push is sent (see notification_prefs.EVENT_CATEGORY
# for that).
_CHANNEL_PREFIXES = (
    (('quote.', 'booking.'), 'bookings'),
    (('invoice.', 'payment.', 'advance.'), 'finance'),
    (('maintenance.', 'driver.'), 'fleet'),
)


def channel_for(event: str) -> str:
    """Which of the three Android notification channels this event's push
    should land in. Falls back to 'bookings' for anything unmapped
    (customer.created, risk-score updates, etc.) rather than reintroducing a
    channel-less default."""
    event = event or ''
    for prefixes, channel in _CHANNEL_PREFIXES:
        if event.startswith(prefixes):
            return channel
    return 'bookings'
