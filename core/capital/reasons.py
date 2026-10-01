"""Reason-code library: the only plain-language explanations a Fast Pay decision may give.

Each code has a direction (``+`` helps, ``-`` hurts, ``!`` blocks) and an
audience: ``transporter`` text may be shown to the transporter, ``desk`` text
only to the capital desk and the funder (it can mention other parties'
behaviour, which is never shown to a transporter; design §9, POPIA s57).

Usage::

    reason('D-NET-LATE', pct=38, n=6)  ->  {'code': 'D-NET-LATE', 'direction': '-',
                                           'text': 'Paid 38% of invoices more than 30 days late across 6 transporters',
                                           'transporter_text': '...', 'params': {...}}

An LLM may rephrase reasons for readability (core.capital.ai) but may never
add one; unknown codes raise.
"""
from __future__ import annotations

from typing import Any

# code: (direction, desk text, transporter text or None if desk-only)
REASONS: dict[str, tuple[str, str, str | None]] = {
    # ---- Eligibility (hard rules, invoice) ----
    'E-STATUS': ('!', 'Invoice status {status} cannot be funded', 'Only sent, unpaid invoices can be funded (this one is {status_label})'),
    'E-BALANCE': ('!', 'Nothing left to collect on this invoice', 'This invoice has nothing left to collect'),
    'E-ALREADY': ('!', 'Invoice already has a live advance', 'This invoice already has a Fast Pay request'),
    'E-NO-LOAD': ('!', 'Invoice is not linked to a load', 'Link this invoice to its delivered load to unlock Fast Pay'),
    'E-NOT-DELIVERED': ('!', 'Load {load} is not delivered', 'The load on this invoice is not marked delivered yet'),
    'E-POD-V0': ('!', 'No proof of delivery on file', 'Add a delivery photo (proof of delivery) to unlock Fast Pay'),
    'E-POD-V1': ('-', 'Proof of delivery is an uploaded file without capture data (V1): needs debtor confirmation', 'Take the delivery photo in the TruckWys app (with location) for a faster decision'),
    'E-AMOUNT-MISMATCH': ('!', 'Invoice total differs from the load value by more than {tolerance}%', 'The invoice total does not match the load value'),
    'E-INVOICE-AGE': ('!', 'Invoice is {days} days old (limit {limit})', 'Invoices older than {limit} days cannot be funded'),
    'E-DELIVERY-AGE': ('!', 'Delivered {days} days ago (limit {limit})', 'Fast Pay is available for {limit} days after delivery'),
    'E-DISPUTE': ('!', 'Invoice is disputed or has a credit note', 'Invoices with a dispute or credit note cannot be funded'),
    'E-DEBTOR-UNIDENTIFIED': ('!', 'Customer has no CIPC registration or VAT number', 'Add your customer\'s company registration or VAT number to unlock Fast Pay'),
    'E-DEBTOR-GOVERNMENT': ('!', 'Government / SOE debtors are excluded at launch', 'Invoices to government customers are not eligible yet'),
    'E-DEBTOR-FOREIGN': ('!', 'Foreign debtors are excluded at launch', 'Invoices to customers outside South Africa are not eligible yet'),
    'E-DEBTOR-HOLD': ('!', 'Debtor is on hold: {reason}', 'Fast Pay is not available for this customer right now'),
    'E-DEBTOR-CESSION': ('!', 'Debtor prohibits cession of its invoices', 'This customer\'s terms do not allow invoices to be financed'),
    'E-DEBTOR-E': ('!', 'Debtor grade E', 'Fast Pay is not available for this customer right now'),
    'E-CROSS-AGEING': ('!', '{pct}% of this debtor\'s funded balance is over 60 days past due', 'Fast Pay is not available for this customer right now'),
    'E-APPLICATION': ('!', 'Fast Pay application not approved ({status})', 'Complete your Fast Pay application to request funding'),
    'E-CONSENT': ('!', 'Missing consent: {missing}', 'Accept the Fast Pay terms and consents in your application'),
    'E-GIT': ('!', 'Goods-in-transit insurance missing or expired', 'Upload a current goods-in-transit insurance certificate'),
    'E-TRANSPORTER-HOLD': ('!', 'Transporter is on hold: {reason}', 'Fast Pay is paused on your account. Contact support.'),
    'E-TRANSPORTER-E': ('!', 'Transporter grade E', 'Fast Pay is not available on your account right now'),
    'E-NO-LINE': ('!', 'No active Fast Pay line for this transporter', 'You do not have an active Fast Pay line yet'),
    'E-NO-FUNDER': ('!', 'Line is not attached to a funder', 'You do not have an active Fast Pay line yet'),
    'E-FUNDER-PAUSED': ('!', 'Funder is not accepting new advances', 'Fast Pay is paused for new requests'),
    'E-DEMO': ('!', 'Demo company: no money moves', 'This is a demo account, so no money is advanced. The offer shows what you would see.'),
    'E-DUPLICATE': ('!', 'Possible duplicate: {detail}', 'This invoice looks like one already submitted. Contact support.'),

    # ---- Debtor score ----
    'D-CIPC-HARD': ('!', 'CIPC status: {status} (hard stop)', None),
    'D-CIPC-OK': ('+', 'CIPC: in business, incorporated {years} years ago', None),
    'D-CIPC-UNKNOWN': ('-', 'CIPC status not checked', None),
    'D-YOUNG': ('-', 'Incorporated {years} years ago', None),
    'D-BUREAU-GOOD': ('+', 'Bureau score {score}/100', None),
    'D-BUREAU-WEAK': ('-', 'Bureau score {score}/100', None),
    'D-BUREAU-NONE': ('-', 'No bureau data', None),
    'D-JUDGMENTS': ('-', '{n} judgment(s) on the bureau file', None),
    'D-NET-ONTIME': ('+', 'Paid {pct}% of invoices on time across {n} transporters', None),
    'D-NET-LATE': ('-', 'Paid {pct}% of invoices more than 30 days late across {n} transporters', None),
    'D-NET-DTP': ('+', 'Pays in {days} days on average ({n} paid invoices)', None),
    'D-NET-TREND-UP': ('-', 'Paying {days} days slower in the last 60 days than over 12 months', None),
    'D-NET-DISPUTES': ('-', 'Credit notes on {pct}% of invoices', None),
    'D-NET-BREADTH': ('+', 'Pays {n} transporters on TruckWys', None),
    'D-COLD-START': ('-', 'New debtor: fewer than {n} invoices paid on TruckWys (limit R{cap} until then)', None),
    'D-SECTOR': ('+', 'Sector: {sector}', None),
    'D-SECTOR-RISK': ('-', 'Sector: {sector}', None),

    # ---- Transporter score ----
    'T-KYC-COMPLETE': ('+', 'Registration, VAT and bank details on file', 'Your company details are complete'),
    'T-KYC-GAP': ('-', 'Missing: {missing}', 'Add your {missing} in Settings'),
    'T-TENURE': ('+', '{months} months on TruckWys', None),
    'T-NEW': ('-', 'Only {months} months on TruckWys', None),
    'T-VOLUME': ('+', '{n} invoices (R{value}) in the last 6 months', None),
    'T-VOLUME-LOW': ('-', 'Only {n} invoices in the last 6 months', None),
    'T-VOLATILE': ('-', 'Monthly invoicing varies a lot (CV {cv})', None),
    'T-MARGIN': ('+', 'Trip margin {pct}% ({basis} costs)', None),
    'T-MARGIN-THIN': ('-', 'Trip margin {pct}% ({basis} costs)', None),
    'T-MARGIN-UNKNOWN': ('-', 'No trip costs recorded, margin unknown', None),
    'T-DILUTION': ('-', 'Credit notes are {pct}% of invoicing over 12 months', None),
    'T-DILUTION-LOW': ('+', 'Credit notes are {pct}% of invoicing over 12 months', None),
    'T-CONCENTRATION': ('-', 'Top customer is {pct}% of receivables', None),
    'T-STRESS-SUB': ('-', 'Subscription status: {status}', None),
    'T-STRESS-FEES': ('-', '{n} failed delivery-fee charge(s) in 90 days', None),
    'T-SUB-OK': ('+', 'Subscription active', None),
    'T-HARD': ('!', 'Transporter hard stop: {detail}', None),

    # ---- Invoice assessment / sizing ----
    'P-HIST': ('+', '{n} previous invoices to this customer paid, average {days} days', '{n} previous invoices to this customer paid, average {days} days'),
    'P-NEW-PAIR': ('-', 'Fewer than {n} paid invoices to this customer on TruckWys', 'Few paid invoices to this customer on TruckWys yet, so the advance is a little lower'),
    'V3': ('+', 'Delivery verified by photo, GPS and vehicle tracking', 'Delivery verified by photo, GPS and vehicle tracking'),
    'V2': ('+', 'Delivery photo taken in the app with location and time', 'Delivery photo taken in the app with location and time'),
    'V2-FAR': ('-', 'Delivery photo taken {km} km from the delivery address', 'The delivery photo location is far from the delivery address'),
    'F-ROUND': ('-', 'Round-number invoice amount', None),
    'F-SAME-AMOUNT': ('-', '{n} invoice(s) of the same amount to this debtor within 7 days', None),
    'F-POD-REUSE': ('!', 'The same POD file is attached to load {other}', 'This delivery photo is already used on another load'),
    'F-NIGHT-POD': ('-', 'POD captured at {hour}:00', None),
    'F-ROUTE': ('-', 'Telematics position does not match the route', None),
    'F-CAPACITY': ('-', 'Invoicing is above the fleet\'s physical capacity', None),
    'L-POT': ('-', 'Funder pot headroom R{headroom}', 'Fast Pay funding is in high demand, so we can advance R{amount} now'),
    'L-DEBTOR': ('-', 'Debtor limit headroom R{headroom}', 'Funding for this customer is near its limit, so we can advance R{amount} now'),
    'L-TRANSPORTER': ('-', 'Transporter line headroom R{headroom}', 'Your Fast Pay line has R{headroom} available'),
    'L-PAIR': ('-', 'Pair limit headroom R{headroom}', 'Funding for this customer is near its limit, so we can advance R{amount} now'),
    'L-SECTOR': ('-', 'Sector limit headroom R{headroom}', 'Fast Pay funding is in high demand, so we can advance R{amount} now'),
    'L-TOP10': ('-', 'Top-10 debtor concentration {pct}%: {detail}', 'Funding for this customer is near its limit, so we can advance R{amount} now'),
    'L-GRADE-MIX': ('-', 'C/D-grade share headroom R{headroom}', 'Fast Pay funding is in high demand, so we can advance R{amount} now'),
    'L-QUEUED': ('-', 'R{amount} queued until capacity frees', 'The remaining R{amount} is queued and offered when capacity frees up'),
    'R-BOOK-RED': ('-', 'Book risk index is red ({index}): no auto-approval; the funder reviews each advance', None),
    'R-REFER-GRADE': ('-', 'Referred: {party} grade {grade}', 'This request needs a quick manual review'),
    'R-REFER-INVOICE': ('-', 'Referred: invoice grade {grade} (expected loss {el}%)', 'This request needs a quick manual review'),
    'R-REFER-FRAUD': ('-', 'Referred: fraud score {score}', 'This request needs a quick manual review'),
    'R-REFER-AMOUNT': ('-', 'Referred: above R{limit}, debtor confirmation needed', 'Large invoices need a quick confirmation with your customer'),
    'R-DECLINE-EL': ('!', 'Expected loss {el}% is above policy', 'This invoice cannot be funded right now'),
    'R-DECLINE-FRAUD': ('!', 'Fraud score {score}', 'This invoice cannot be funded right now'),
    'R-MODE-A': ('+', 'Needs the funder\'s approval (Mode A)', 'The finance provider approves each advance before it is paid out.'),
    'R-AUTO': ('+', 'Inside the funder-signed auto-approval envelope', 'Approved automatically'),
    'R-FUND': ('+', 'All checks passed', 'All checks passed'),
}


class UnknownReason(KeyError):
    pass


class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown placeholders visible rather than crash
        return '{' + key + '}'


def reason(code: str, **params: Any) -> dict:
    if code not in REASONS:
        raise UnknownReason(code)
    direction, desk, transporter = REASONS[code]
    p = _SafeDict({k: v for k, v in params.items()})
    return {
        'code': code,
        'direction': direction,
        'text': desk.format_map(p),
        'transporter_text': transporter.format_map(p) if transporter else None,
        'params': {k: (str(v) if not isinstance(v, (int, float, str, bool, type(None))) else v)
                   for k, v in params.items()},
    }


def for_transporter(reasons: list[dict]) -> list[dict]:
    """Only the reasons a transporter may see, in their wording."""
    return [{'code': r['code'], 'direction': r['direction'], 'text': r['transporter_text']}
            for r in reasons if r.get('transporter_text')]
