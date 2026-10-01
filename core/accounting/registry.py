"""Which accounting providers exist and how to build their adapters."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderInfo:
    code: str
    slug: str
    name: str
    availability: str          # available | coming_soon


PROVIDERS = [
    ProviderInfo('XERO', 'xero', 'Xero', 'available'),
    ProviderInfo('QBO', 'quickbooks', 'QuickBooks Online', 'available'),
    ProviderInfo('SAGE', 'sage', 'Sage Business Cloud Accounting', 'coming_soon'),
]
BY_SLUG = {p.slug: p for p in PROVIDERS}
BY_CODE = {p.code: p for p in PROVIDERS}


def adapter_class(code: str):
    if code == 'XERO':
        from core.accounting.xero import XeroAdapter
        return XeroAdapter
    if code == 'QBO':
        from core.accounting.quickbooks import QuickBooksAdapter
        return QuickBooksAdapter
    raise LookupError(f'No adapter for provider {code!r}')


def is_configured(code: str) -> bool:
    if code == 'XERO':
        from core.accounting import xero
        return xero.is_configured()
    if code == 'QBO':
        from core.accounting import quickbooks
        return quickbooks.is_configured()
    return False


def get_adapter(connection):
    return adapter_class(connection.provider)(connection)


def redirect_uri(code: str) -> str:
    from django.conf import settings
    return {'XERO': getattr(settings, 'XERO_REDIRECT_URI', ''),
            'QBO': getattr(settings, 'QBO_REDIRECT_URI', '')}.get(code, '')


def provider_name(code: str) -> str:
    info = BY_CODE.get(code)
    return info.name if info else code
