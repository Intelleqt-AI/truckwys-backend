"""CIPC enterprise-status adapters (fake / null / live).

Selected by ``settings.CAPITAL_CIPC_ADAPTER``:

* ``fake``  reads ``fixtures/cipc.json`` keyed by normalised CIPC number. An
  unknown number gets a deterministic default derived from a hash of the
  number (always IN_BUSINESS, with an incorporation year from the hash),
  marked ``is_fake`` and ``raw['derived']=True``. Never used for real money.
* ``null``  honest no-data: ``available=False``.
* ``live``  there is no CIPC data contract yet: returns ``available=False``
  with a note. It never raises and never calls the network.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

CIPC_STATUSES = ('UNKNOWN', 'IN_BUSINESS', 'DEREGISTRATION', 'DEREGISTERED', 'BUSINESS_RESCUE', 'LIQUIDATION')
HARD_STATUSES = ('DEREGISTRATION', 'DEREGISTERED', 'BUSINESS_RESCUE', 'LIQUIDATION')

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


@dataclass
class CIPCResult:
    available: bool
    status: str = 'UNKNOWN'
    legal_name: str = ''
    incorporation_date: date | None = None
    source: str = ''
    is_fake: bool = False
    raw: dict = field(default_factory=dict)
    note: str = ''

    def to_payload(self) -> dict:
        return {
            'available': self.available, 'status': self.status, 'legal_name': self.legal_name,
            'incorporation_date': self.incorporation_date.isoformat() if self.incorporation_date else None,
            'source': self.source, 'is_fake': self.is_fake, 'note': self.note, 'raw': self.raw,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> 'CIPCResult':
        inc = payload.get('incorporation_date')
        return cls(
            available=bool(payload.get('available')), status=payload.get('status') or 'UNKNOWN',
            legal_name=payload.get('legal_name') or '',
            incorporation_date=date.fromisoformat(inc) if inc else None,
            source=payload.get('source') or '', is_fake=bool(payload.get('is_fake')),
            raw=payload.get('raw') or {}, note=payload.get('note') or '',
        )

    def summary(self) -> dict:
        """JSON-safe summary for score inputs (no raw payload)."""
        d = self.to_payload()
        d.pop('raw', None)
        return d


@lru_cache(maxsize=4)
def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding='utf-8') as fh:
        data = json.load(fh)
    return {k: v for k, v in data.items() if not k.startswith('_')}


def _hash_int(key: str) -> int:
    return int(hashlib.sha256(key.encode('utf-8')).hexdigest()[:12], 16)


class CIPCAdapter:
    name = 'base'
    is_fake = False

    def lookup(self, registration_number: str) -> CIPCResult:  # pragma: no cover - interface
        raise NotImplementedError


class FakeCIPCAdapter(CIPCAdapter):
    name = 'fake'
    is_fake = True

    def lookup(self, registration_number: str) -> CIPCResult:
        row = _load_fixture('cipc.json').get(registration_number)
        if row is not None:
            status = row.get('status') if row.get('status') in CIPC_STATUSES else 'UNKNOWN'
            inc = row.get('incorporation_date')
            return CIPCResult(
                available=True, status=status, legal_name=row.get('legal_name', ''),
                incorporation_date=date.fromisoformat(inc) if inc else None,
                source='fake:fixture', is_fake=True, raw=dict(row),
            )
        # Deterministic default for an unknown number: always in business,
        # incorporated 1995..2022 on a hash-derived date.
        h = _hash_int(registration_number)
        inc = date(1995 + h % 28, 1 + (h // 28) % 12, 1 + (h // 336) % 28)
        return CIPCResult(
            available=True, status='IN_BUSINESS', legal_name='', incorporation_date=inc,
            source='fake:derived', is_fake=True,
            raw={'derived': True, 'registration_number': registration_number,
                 'incorporation_date': inc.isoformat(), 'status': 'IN_BUSINESS'},
            note='Fake CIPC result derived from a hash of the number (no fixture row).',
        )


class NullCIPCAdapter(CIPCAdapter):
    name = 'null'

    def lookup(self, registration_number: str) -> CIPCResult:
        return CIPCResult(available=False, source='null',
                          note='No CIPC adapter configured (CAPITAL_CIPC_ADAPTER=null).')


class LiveCIPCAdapter(CIPCAdapter):
    """Placeholder until a CIPC data contract exists. Never raises, never calls out."""
    name = 'live'

    def lookup(self, registration_number: str) -> CIPCResult:
        return CIPCResult(available=False, source='live',
                          note='Live CIPC lookups are not contracted yet; status unavailable.')


ADAPTERS = {cls.name: cls for cls in (FakeCIPCAdapter, NullCIPCAdapter, LiveCIPCAdapter)}


def get_cipc_adapter(name: str | None = None) -> CIPCAdapter:
    from django.conf import settings
    key = (name or getattr(settings, 'CAPITAL_CIPC_ADAPTER', 'null') or 'null').strip().lower()
    return ADAPTERS.get(key, NullCIPCAdapter)()
