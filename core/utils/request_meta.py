"""Helpers for deriving display metadata (device, client IP) from a request.

Centralizes the User-Agent -> device string and X-Forwarded-For / REMOTE_ADDR
IP logic that was previously inlined in the sessions view.
"""


def client_ip(request) -> str:
    """Best-effort client IP as a display string (never raises)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '') or ''
    if xff:
        return xff.split(',')[0].strip()[:45]
    return (request.META.get('REMOTE_ADDR', '') or '')[:45]


def mask_email(email: str) -> str:
    """Partially obscure an email for display, e.g. 'al***@ex***.com'.

    Null-safe; only ever used for display (never trust it as an identifier).
    """
    email = (email or '').strip()
    if '@' not in email:
        return email
    local, _, domain = email.partition('@')
    dom_name, dot, tld = domain.partition('.')

    def _mask(part: str) -> str:
        if len(part) <= 1:
            return part + '***'
        return part[:2] + '***'

    masked_domain = _mask(dom_name) + (dot + tld if dot else '')
    return f"{_mask(local)}@{masked_domain}"


def parse_device(request) -> str:
    """Coarse device label from the User-Agent.

    Order matters: mobile is checked before mac/windows so tablet/phone UAs
    (which also contain 'mac'/'windows' fragments) classify as mobile.
    """
    ua = (request.META.get('HTTP_USER_AGENT', '') or '').lower()
    if 'iphone' in ua or 'android' in ua or 'mobile' in ua:
        return 'Mobile device'
    if 'mac' in ua:
        return 'Mac'
    if 'windows' in ua:
        return 'Windows PC'
    return 'Unknown device'
