"""Plain-language explanation of a decision, built only from its reason codes.

The deterministic template below is the default and the fallback. When
``CAPITAL_AI_ENABLED`` is on, ``core.capital.ai.reword`` may rephrase the
template for readability; it receives only the template text, the
transporter-safe reasons and the decision numbers, and is rejected if it
changes any number or adds a reason (see core.capital.ai).
"""
from __future__ import annotations

from core.capital.reasons import for_transporter

DECISION_LEAD = {
    'FUND': 'This invoice can be advanced in full.',
    'PART_FUND': 'Part of this invoice can be advanced now.',
    'QUEUE': 'This invoice qualifies, but there is no funding capacity for it right now, so it is queued.',
    'REFER': 'This invoice needs a quick manual review before it can be funded.',
    'DECLINE': 'This invoice cannot be funded at the moment.',
}


def _r(v) -> str:
    return f'R{v:,.2f}'


def template(ev) -> str:
    parts = [DECISION_LEAD.get(ev.decision, '')]
    if ev.decision in ('FUND', 'PART_FUND', 'REFER') and ev.fundable_amount > 0:
        parts.append(
            f'The advance is {_r(ev.fundable_amount)} ({ev.advance_rate_pct.normalize():f}% of what is still owed). '
            f'The fee is {_r(ev.fee_amount)} plus {_r(ev.fee_vat_amount)} VAT on the platform part, '
            f'so you would receive {_r(ev.net_payout)}. The remaining {_r(ev.holdback_amount)} is paid to you '
            'when your customer pays, less any credit notes.')
    if ev.queued_amount > 0 and ev.decision != 'DECLINE':
        parts.append(f'{_r(ev.queued_amount)} is queued and offered when capacity frees up.')
    if ev.decision != 'DECLINE' and ev.expected_payment_date:
        parts.append(f'We expect your customer to pay around {ev.expected_payment_date:%d %B %Y}.')
    reasons = [r['text'] for r in for_transporter(ev.reasons) if r['direction'] in ('!', '-')]
    if reasons:
        parts.append('Why: ' + '; '.join(dict.fromkeys(reasons)) + '.')
    if ev.decision in ('FUND', 'PART_FUND', 'REFER'):
        parts.append('An independent finance provider approves each advance; TruckWys is not a lender.')
    return ' '.join(p for p in parts if p)


def explain(ev) -> tuple[str, str]:
    text = template(ev)
    try:
        from core.capital import ai
        return ai.reword(text, ev)
    except Exception:  # never let wording break a decision
        return text, 'TEMPLATE'
