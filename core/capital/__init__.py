"""Fast Pay (Capital) risk scoring, book engine and automation.

Modules:
    policy       defaults, policy in force, PD master scale
    reasons      reason-code library (the only explanation text allowed)
    scoring/     debtor and transporter scorecards, persistence, current-score lookup
    verification POD tiers, duplicate and fraud checks
    adapters/    CIPC and bureau adapters (fake / null / live), recorded fixtures
    ledger       append-only ledger writes and derived balances, reconciliation
    book         exposures, limits and headroom, concentration, risk index, stress
    engine       the one decision path: evaluate -> FUND / PART_FUND / QUEUE / REFER / DECLINE
    queue        queue release and part-fund top-ups
    monitoring   early warnings and alerts
    dataroom     monthly funder export
    ai           LLM document extraction and decision wording (never decides)
    access       who may see / act on which funder's book
"""
