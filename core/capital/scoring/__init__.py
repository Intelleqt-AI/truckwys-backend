"""Debtor and transporter scorecards (Phase 1: deterministic expert scorecards).

Public interface (used by core.capital.engine, monitoring and the desk API):

    ScoreOutput                       dataclass, see below
    debtor.score_debtor(debtor, policy, *, as_of=None) -> ScoreOutput
    debtor.network_features(debtor, *, as_of=None) -> dict
    debtor.pair_features(company, debtor, *, as_of=None) -> dict
    debtor.expected_dtp(debtor, policy, *, company=None, as_of=None) -> (Decimal days, dict basis)
    transporter.score_transporter(company, policy, *, as_of=None) -> ScoreOutput
    transporter.receivables_share(company, debtor, *, as_of=None) -> Decimal   (0-1)
    transporter.dilution_reserve_pct(company, policy, *, as_of=None) -> Decimal (percent, e.g. 11.2)
    persist(output, *, debtor=None, company=None) -> CapitalScore
    current_debtor_score(debtor, policy, *, refresh=True) -> CapitalScore
    current_transporter_score(company, policy, *, refresh=True) -> CapitalScore

Phase 2 models replace the scorecards behind the same functions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone


@dataclass
class ScoreOutput:
    kind: str                         # 'DEBTOR' | 'TRANSPORTER'
    grade: str                        # 'A'..'E'
    points: int                       # 0-100, higher = safer
    pd_12m: Decimal                   # 0-1
    reason_codes: list = field(default_factory=list)   # core.capital.reasons.reason(...) dicts, most important first
    inputs: dict = field(default_factory=dict)          # JSON-safe snapshot of every input used
    model_version: str = ''
    expected_dtp_days: Decimal | None = None
    hard_stop: bool = False
    cold_start: bool = False


def inputs_hash(inputs: dict) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def persist(output: ScoreOutput, *, debtor=None, company=None):
    from core.models import CapitalScore
    ttl = int(getattr(settings, 'CAPITAL_SCORE_TTL_HOURS', 24))
    return CapitalScore.objects.create(
        kind=output.kind, debtor=debtor, company=company, grade=output.grade, points=int(output.points),
        pd_12m=Decimal(output.pd_12m).quantize(Decimal('0.00001')),
        expected_dtp_days=(Decimal(output.expected_dtp_days).quantize(Decimal('0.1'))
                           if output.expected_dtp_days is not None else None),
        hard_stop=output.hard_stop, cold_start=output.cold_start,
        reason_codes=output.reason_codes, inputs=output.inputs, inputs_hash=inputs_hash(output.inputs),
        model_version=output.model_version, valid_until=timezone.now() + timedelta(hours=ttl),
    )


def current_debtor_score(debtor, policy, *, refresh: bool = True):
    """Newest still-valid debtor score, or a fresh one (persisted) when refresh=True."""
    from core.models import CapitalScore
    row = (CapitalScore.objects.filter(kind='DEBTOR', debtor=debtor, valid_until__gt=timezone.now())
           .order_by('-created_at', '-id').first())
    if row is not None or not refresh:
        return row
    from .debtor import score_debtor
    return persist(score_debtor(debtor, policy), debtor=debtor)


def current_transporter_score(company, policy, *, refresh: bool = True):
    from core.models import CapitalScore
    row = (CapitalScore.objects.filter(kind='TRANSPORTER', company=company, valid_until__gt=timezone.now())
           .order_by('-created_at', '-id').first())
    if row is not None or not refresh:
        return row
    from .transporter import score_transporter
    return persist(score_transporter(company, policy), company=company)
