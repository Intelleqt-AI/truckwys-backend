"""CIPC and credit-bureau lookups for debtor scoring, settings-gated and cached.

    lookup_cipc(debtor)   -> CIPCResult
    lookup_bureau(debtor) -> BureauResult

Each lookup reuses a stored ``ExternalCheck`` for the same debtor, provider
and adapter: an available result for 30 days, an unavailable one for 1 day
(so a transient live failure is retried, while a null/unconfigured adapter
does not write a row on every score). Otherwise it calls the adapter selected
by ``settings.CAPITAL_CIPC_ADAPTER`` / ``CAPITAL_BUREAU_ADAPTER`` and writes an
``ExternalCheck`` row (cost 0 for fakes and null). A debtor with no
registration number gets an unavailable result without calling anything.

A CIPC lookup that returns data also updates the DebtorIdentity's
``cipc_status``, ``cipc_checked_at``, ``incorporation_date`` and (if blank)
``legal_name``.

Fakes read recorded JSON fixtures; nothing here touches the network except
the ``live`` bureau adapter, which wraps core.integrations.bureau_adapter.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .bureau import BureauResult, get_bureau_adapter
from .cipc import HARD_STATUSES, CIPCResult, get_cipc_adapter

__all__ = ['CIPCResult', 'BureauResult', 'HARD_STATUSES', 'lookup_cipc', 'lookup_bureau',
           'get_cipc_adapter', 'get_bureau_adapter', 'normalised_registration', 'CACHE_DAYS']

CACHE_DAYS = 30
UNAVAILABLE_CACHE_DAYS = 1


def normalised_registration(debtor) -> str:
    reg = (getattr(debtor, 'registration_number', None) or '').strip()
    if not reg:
        return ''
    try:
        from core.services.identity import normalise_registration_number
        return normalise_registration_number(reg, getattr(debtor, 'country', 'ZA') or 'ZA')
    except ValueError:
        return reg.upper()


def _cached(debtor, provider: str, adapter_name: str):
    from core.models import ExternalCheck
    now = timezone.now()
    row = (ExternalCheck.objects
           .filter(debtor=debtor, provider=provider, adapter=adapter_name,
                   fetched_at__gte=now - timedelta(days=CACHE_DAYS))
           .order_by('-fetched_at', '-id').first())
    if row is None:
        return None
    if not row.available and row.fetched_at < now - timedelta(days=UNAVAILABLE_CACHE_DAYS):
        return None
    return row


def _record(debtor, provider: str, adapter, result, *, status: str, score):
    from core.models import ExternalCheck
    return ExternalCheck.objects.create(
        provider=provider, adapter=adapter.name, debtor=debtor, available=result.available,
        status=status or '', score=score, payload=result.to_payload(),
        is_fake=bool(result.is_fake), cost_zar=Decimal('0.00'),
    )


def lookup_cipc(debtor) -> CIPCResult:
    reg = normalised_registration(debtor)
    if not reg:
        return CIPCResult(available=False, source='none', note='Debtor has no registration number.')
    adapter = get_cipc_adapter()
    row = _cached(debtor, 'CIPC', adapter.name)
    if row is not None:
        return CIPCResult.from_payload(row.payload or {})
    try:
        result = adapter.lookup(reg)
    except Exception as exc:  # an adapter must never break scoring
        result = CIPCResult(available=False, source=adapter.name, note=f'lookup error: {exc}')
    _record(debtor, 'CIPC', adapter, result, status=result.status if result.available else 'UNAVAILABLE',
            score=None)
    if result.available:
        _apply_to_identity(debtor, result)
    return result


def _apply_to_identity(debtor, result: CIPCResult) -> None:
    fields = []
    if debtor.cipc_status != result.status:
        debtor.cipc_status = result.status
        fields.append('cipc_status')
    debtor.cipc_checked_at = timezone.now()
    fields.append('cipc_checked_at')
    if result.incorporation_date and debtor.incorporation_date != result.incorporation_date:
        debtor.incorporation_date = result.incorporation_date
        fields.append('incorporation_date')
    if result.legal_name and not (debtor.legal_name or '').strip():
        debtor.legal_name = result.legal_name[:200]
        fields.append('legal_name')
    debtor.save(update_fields=fields + ['updated_at'])


def lookup_bureau(debtor) -> BureauResult:
    reg = normalised_registration(debtor)
    if not reg:
        return BureauResult(available=False, source='none', note='Debtor has no registration number.')
    adapter = get_bureau_adapter()
    row = _cached(debtor, 'BUREAU', adapter.name)
    if row is not None:
        return BureauResult.from_payload(row.payload or {})
    try:
        result = adapter.lookup(registration_number=reg, name=getattr(debtor, 'legal_name', '') or '',
                                vat_number=getattr(debtor, 'vat_number', '') or '')
    except Exception as exc:
        result = BureauResult(available=False, source=adapter.name, note=f'lookup error: {exc}')
    _record(debtor, 'BUREAU', adapter, result, status='OK' if result.available else 'UNAVAILABLE',
            score=result.score)
    return result
