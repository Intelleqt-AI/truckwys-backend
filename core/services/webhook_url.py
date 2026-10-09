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
from typing import List, Optional, Tuple
from urllib.parse import urlparse

BLOCKED = 'blocked'          # never deliver: wrong scheme or a non-public address
UNRESOLVED = 'unresolved'    # DNS failed: may be transient

MESSAGE = 'Webhook URL must be an https URL to a public host.'

_TUNNEL_V6 = (ipaddress.ip_network('64:ff9b::/96'), ipaddress.ip_network('64:ff9b:1::/48'),
               ipaddress.ip_network('2002::/16'))


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
    # NAT64 (64:ff9b::/96) and 6to4 (2002::/16) can carry a private IPv4
    # inside: never a webhook target.
    if ip.version == 6 and any(ip in n for n in _TUNNEL_V6):
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified or not ip.is_global)


def resolve_webhook_target(url: str) -> Tuple[Optional[str], List[str]]:
    """(problem, addresses): problem is None when ``url`` is safe to deliver
    to now, else BLOCKED / UNRESOLVED; addresses are the checked public IPs.
    Delivery connects to one of THESE addresses (webhook_delivery._post), so
    a second DNS lookup can't swap in a private one (DNS rebinding)."""
    try:
        parsed = urlparse(url or '')
        host = parsed.hostname
        parsed.port   # raises ValueError for a bad port
    except ValueError:
        return BLOCKED, []
    if parsed.scheme != 'https' or not host or parsed.username or parsed.password:
        return BLOCKED, []
    try:
        addrs = resolve_ips(host)
    except (socket.gaierror, UnicodeError, OSError):
        return UNRESOLVED, []
    if not addrs or not all(_public(a) for a in addrs):
        return BLOCKED, []
    return None, addrs


def check_webhook_url(url: str) -> Optional[str]:
    """None if ``url`` is safe to deliver to now, else BLOCKED / UNRESOLVED."""
    return resolve_webhook_target(url)[0]


def is_safe_webhook_url(url: str) -> bool:
    return check_webhook_url(url) is None


def validate_webhook_url(url: str) -> None:
    """Django/DRF validator: raises ValidationError for an unsafe URL."""
    from django.core.exceptions import ValidationError
    if url and not is_safe_webhook_url(url):
        raise ValidationError(MESSAGE)
