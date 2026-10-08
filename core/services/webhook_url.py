"""SSRF guard for every user-supplied outbound webhook URL.

One rule, applied when a URL is accepted (partner API, legacy Webhook and
IntegrationAPIKey serializers, Django admin) and again at send time (DNS can
change after the URL was accepted): https only, and the host must resolve
only to public addresses — no private, loopback, link-local (incl. the
169.254.169.254 cloud metadata endpoint), reserved, multicast or unspecified
IPs. Delivery itself never follows redirects (webhook_delivery._post).
"""

import ipaddress
import socket
from typing import List, Optional
from urllib.parse import urlparse

BLOCKED = 'blocked'          # never deliver: wrong scheme or a non-public address
UNRESOLVED = 'unresolved'    # DNS failed: may be transient

MESSAGE = 'Webhook URL must be an https URL to a public host.'


def resolve_ips(host: str) -> List[str]:
    """All addresses ``host`` resolves to (patched in tests: no network)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split('%', 1)[0])
    except ValueError:
        return False
    if getattr(ip, 'ipv4_mapped', None):
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified or not ip.is_global)


def check_webhook_url(url: str) -> Optional[str]:
    """None if ``url`` is safe to deliver to now, else BLOCKED / UNRESOLVED."""
    try:
        parsed = urlparse(url or '')
        host = parsed.hostname
    except ValueError:
        return BLOCKED
    if parsed.scheme != 'https' or not host or parsed.username or parsed.password:
        return BLOCKED
    try:
        addrs = resolve_ips(host)
    except (socket.gaierror, UnicodeError, OSError):
        return UNRESOLVED
    if not addrs or not all(_public(a) for a in addrs):
        return BLOCKED
    return None


def is_safe_webhook_url(url: str) -> bool:
    return check_webhook_url(url) is None


def validate_webhook_url(url: str) -> None:
    """Django/DRF validator: raises ValidationError for an unsafe URL."""
    from django.core.exceptions import ValidationError
    if url and not is_safe_webhook_url(url):
        raise ValidationError(MESSAGE)
