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
    ProviderInfo('QBO', 'quickbooks', 'QuickBooks Online', 'coming_soon'),
    ProviderInfo('SAGE', 'sage', 'Sage Business Cloud Accounting', 'coming_soon'),
]
BY_SLUG = {p.slug: p for p in PROVIDERS}
BY_CODE = {p.code: p for p in PROVIDERS}


def adapter_class(code: str):
    if code == 'XERO':
        from core.accounting.xero import XeroAdapter
        return XeroAdapter
    raise LookupError(f'No adapter for provider {code!r}')


def is_configured(code: str) -> bool:
    if code == 'XERO':
        from core.accounting import xero
        return xero.is_configured()
    return False


def get_adapter(connection):
    return adapter_class(connection.provider)(connection)


def provider_name(code: str) -> str:
    info = BY_CODE.get(code)
    return info.name if info else code
