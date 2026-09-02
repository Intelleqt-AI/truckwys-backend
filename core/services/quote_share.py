"""Shared logic for putting a Quote in front of its customer: the share
token/URL and the email that carries it. Used both by the explicit
send_to_customer action and by the SENT-transition signal in signals.py, so
a quote dragged into "Sent" on the Kanban board or PATCHed directly gets the
exact same treatment as clicking "Send to customer" — one place, no drift.
"""
import secrets

from django.conf import settings


def ensure_quote_token(quote) -> str:
    """Idempotently give the quote a share token, saving only if it needed one."""
    if not quote.token:
        quote.token = secrets.token_urlsafe(32)
        quote.save(update_fields=['token'])
    return quote.token


def quote_share_url(quote) -> str:
    ensure_quote_token(quote)
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3701')
    return f"{frontend_url}/quotes/view/{quote.id}/{quote.token}"


def send_quote_to_customer_email(quote) -> tuple[bool, str | None]:
    """Email the share link to the quote's customer, if one is on file.

    Returns (email_sent, recipient_address_or_None).
    """
    from core.services.email_service import send_quote_share_email
    recipient = quote.customer.email if quote.customer else None
    email_sent = send_quote_share_email(quote, quote_share_url(quote)) if recipient else False
    return email_sent, recipient
