"""Provider-agnostic debtor credit-bureau adapter.

Normalises the major South African commercial bureaus behind one interface so the
risk engine can ask for a debtor's credit score without caring which bureau is
wired in. Selected by environment:

    CREDIT_BUREAU_PROVIDER = EXPERIAN | TRANSUNION | XDS | COMPUSCAN
    CREDIT_BUREAU_API_KEY  = <secret>
    CREDIT_BUREAU_BASE_URL = <optional override>

When no provider/key is configured the adapter returns an HONEST "no data"
result (score=None) — it never fabricates a score. The HTTP request/parse shape
for each bureau is in place, so going live is just supplying a credential.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class BureauResult:
    available: bool
    score: Optional[int]          # normalised 0-100 (higher = better)
    source: str
    rating: str = ''
    risk_class: str = ''
    note: str = ''
    raw: dict = field(default_factory=dict)


class BureauProvider:
    name = 'BASE'

    def lookup(self, *, registration_number=None, name=None, vat_number=None) -> BureauResult:
        raise NotImplementedError


class NullProvider(BureauProvider):
    """Returned when nothing is configured — honest no-data, never a fake score."""
    name = 'NONE'

    def lookup(self, **kwargs) -> BureauResult:
        return BureauResult(
            available=False, score=None, source='NONE',
            note='No credit bureau configured (set CREDIT_BUREAU_PROVIDER + CREDIT_BUREAU_API_KEY).',
        )


class HttpBureauProvider(BureauProvider):
    """Generic HTTP adapter — subclasses set the endpoint + score normalisation."""
    name = 'HTTP'
    endpoint = ''
    timeout = 8

    def __init__(self, api_key: str, base_url: str = ''):
        self.api_key = api_key
        self.base_url = base_url or self.endpoint

    def _payload(self, registration_number, name, vat_number) -> dict:
        return {
            'registration_number': registration_number,
            'company_name': name,
            'vat_number': vat_number,
        }

    def _normalize(self, data: dict):
        """Map the bureau's native response to a 0-100 score. Override per bureau."""
        raw = data.get('score')
        return int(raw) if raw is not None else None

    def lookup(self, *, registration_number=None, name=None, vat_number=None) -> BureauResult:
        if not self.api_key:
            return NullProvider().lookup()
        if not (registration_number or name):
            return BureauResult(available=False, score=None, source=self.name,
                                note='Need a registration number or company name to query the bureau.')
        try:
            import requests
            resp = requests.post(
                self.base_url,
                headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
                json=self._payload(registration_number, name, vat_number),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            score = self._normalize(data)
            return BureauResult(
                available=score is not None, score=score, source=self.name,
                rating=str(data.get('rating', '')), risk_class=str(data.get('risk_class', '')),
                raw=data if isinstance(data, dict) else {},
            )
        except Exception as exc:  # network/credential/parse — degrade to no-data
            logger.warning('%s bureau lookup failed: %s', self.name, exc)
            return BureauResult(available=False, score=None, source=self.name, note=f'lookup error: {exc}')


class ExperianProvider(HttpBureauProvider):
    name = 'EXPERIAN'
    endpoint = 'https://api.experian.co.za/business/v1/commercial-score'

    def _normalize(self, data):
        s = data.get('commercial_delphi_score', data.get('score'))
        return int(s) if s is not None else None


class TransUnionProvider(HttpBureauProvider):
    name = 'TRANSUNION'
    endpoint = 'https://api.transunion.co.za/commercial/v2/score'

    def _normalize(self, data):
        s = data.get('commercial_score', data.get('score'))
        return int(s) if s is not None else None


class XDSProvider(HttpBureauProvider):
    name = 'XDS'
    endpoint = 'https://api.xds.co.za/v1/commercial/score'


class CompuScanProvider(HttpBureauProvider):
    name = 'COMPUSCAN'
    endpoint = 'https://api.compuscan.co.za/v1/business/score'


_PROVIDERS = {cls.name: cls for cls in (ExperianProvider, TransUnionProvider, XDSProvider, CompuScanProvider)}


def _cfg(key: str, default: str = '') -> str:
    return os.environ.get(key) or getattr(settings, key, '') or default


def get_bureau_provider() -> BureauProvider:
    """Resolve the configured provider, or NullProvider when unconfigured."""
    name = _cfg('CREDIT_BUREAU_PROVIDER').upper()
    api_key = _cfg('CREDIT_BUREAU_API_KEY')
    base_url = _cfg('CREDIT_BUREAU_BASE_URL')
    cls = _PROVIDERS.get(name)
    if not cls or not api_key:
        return NullProvider()
    return cls(api_key=api_key, base_url=base_url)


def is_configured() -> bool:
    return not isinstance(get_bureau_provider(), NullProvider)


def lookup_customer(customer) -> BureauResult:
    """Convenience: look up a Customer-like object (uses its bureau-relevant fields)."""
    return get_bureau_provider().lookup(
        registration_number=getattr(customer, 'registration_number', None),
        name=getattr(customer, 'company', None) or getattr(customer, 'name', None),
        vat_number=getattr(customer, 'vat_number', None),
    )
