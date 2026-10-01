"""Commercial credit-bureau adapters for debtor scoring (fake / null / live).

Selected by ``settings.CAPITAL_BUREAU_ADAPTER``:

* ``fake``  reads ``fixtures/bureau.json`` keyed by normalised CIPC number. An
  unknown number gets a deterministic score 35..84 and 0 judgments derived
  from a hash of the number, marked ``is_fake`` and ``raw['derived']=True``.
* ``null``  honest no-data: ``available=False``.
* ``live``  wraps ``core.integrations.bureau_adapter`` (which itself degrades
  to no-data when no provider/key is configured, and never raises).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cipc import _hash_int, _load_fixture


@dataclass
class BureauResult:
    available: bool
    score: int | None = None          # normalised 0-100, higher = better
    judgments: int = 0
    source: str = ''
    is_fake: bool = False
    raw: dict = field(default_factory=dict)
    note: str = ''

    def to_payload(self) -> dict:
        return {'available': self.available, 'score': self.score, 'judgments': self.judgments,
                'source': self.source, 'is_fake': self.is_fake, 'note': self.note, 'raw': self.raw}

    @classmethod
    def from_payload(cls, payload: dict) -> 'BureauResult':
        score = payload.get('score')
        return cls(available=bool(payload.get('available')), score=int(score) if score is not None else None,
                   judgments=int(payload.get('judgments') or 0), source=payload.get('source') or '',
                   is_fake=bool(payload.get('is_fake')), raw=payload.get('raw') or {},
                   note=payload.get('note') or '')

    def summary(self) -> dict:
        d = self.to_payload()
        d.pop('raw', None)
        return d


def _clamp_score(v) -> int | None:
    if v is None:
        return None
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return None


class BureauAdapter:
    name = 'base'
    is_fake = False

    def lookup(self, *, registration_number: str, name: str = '', vat_number: str = '') -> BureauResult:  # pragma: no cover
        raise NotImplementedError


class FakeBureauAdapter(BureauAdapter):
    name = 'fake'
    is_fake = True

    def lookup(self, *, registration_number: str, name: str = '', vat_number: str = '') -> BureauResult:
        row = _load_fixture('bureau.json').get(registration_number)
        if row is not None:
            score = _clamp_score(row.get('score'))
            return BureauResult(available=score is not None, score=score,
                                judgments=int(row.get('judgments') or 0), source='fake:fixture',
                                is_fake=True, raw=dict(row))
        h = _hash_int('bureau:' + registration_number)
        score = 35 + h % 50
        return BureauResult(available=True, score=score, judgments=0, source='fake:derived', is_fake=True,
                            raw={'derived': True, 'registration_number': registration_number, 'score': score},
                            note='Fake bureau result derived from a hash of the number (no fixture row).')


class NullBureauAdapter(BureauAdapter):
    name = 'null'

    def lookup(self, *, registration_number: str, name: str = '', vat_number: str = '') -> BureauResult:
        return BureauResult(available=False, source='null',
                            note='No bureau adapter configured (CAPITAL_BUREAU_ADAPTER=null).')


class LiveBureauAdapter(BureauAdapter):
    """Maps core.integrations.bureau_adapter onto BureauResult. Never raises."""
    name = 'live'

    def lookup(self, *, registration_number: str, name: str = '', vat_number: str = '') -> BureauResult:
        try:
            from core.integrations.bureau_adapter import get_bureau_provider
            res = get_bureau_provider().lookup(registration_number=registration_number or None,
                                               name=name or None, vat_number=vat_number or None)
        except Exception as exc:  # defensive: the integration already degrades, this is belt and braces
            return BureauResult(available=False, source='live', note=f'lookup error: {exc}')
        raw = res.raw if isinstance(res.raw, dict) else {}
        judgments = raw.get('judgments', raw.get('judgement_count', 0))
        try:
            judgments = int(judgments or 0)
        except (TypeError, ValueError):
            judgments = 0
        score = _clamp_score(res.score)
        return BureauResult(available=bool(res.available and score is not None), score=score,
                            judgments=judgments, source=f'live:{res.source}', is_fake=False, raw=raw,
                            note=res.note or '')


ADAPTERS = {cls.name: cls for cls in (FakeBureauAdapter, NullBureauAdapter, LiveBureauAdapter)}


def get_bureau_adapter(name: str | None = None) -> BureauAdapter:
    from django.conf import settings
    key = (name or getattr(settings, 'CAPITAL_BUREAU_ADAPTER', 'null') or 'null').strip().lower()
    return ADAPTERS.get(key, NullBureauAdapter)()
