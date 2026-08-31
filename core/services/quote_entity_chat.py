"""Conversational create-on-the-fly for a customer/vehicle type mentioned in the
quote chat that doesn't match any real record for the company.

Reuses the Copilot's propose_create/execute_proposal (backend/core/services/
copilot_tools.py) rather than a parallel creation path — that machinery is
already tenant-scoped, RBAC-checked, serializer-validated, and (per the FK
cross-tenant fix) FK-scope-validated. Called with conversation=None throughout,
exactly as the Copilot's own test suite already does for one-shot creates.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from core.services.copilot_entities import ENTITY_REGISTRY, role_can
from core.services import copilot_tools
from core.services import language_detect

CUSTOMER_LINK = {'label': 'Add client manually', 'href': '/customers/new'}
VEHICLE_TYPE_LINK = {'label': 'Manage vehicle types', 'href': '/settings/vehicle-types'}

# Field collection order per table — kept short and specific rather than the
# full writable-field set, so this stays a quick in-chat exchange.
FIELD_ORDER = {
    'customers': ['email'],
    'vehicle_types': ['capacity', 'max_distance', 'base_rate'],
}
FIELD_PROMPTS = {
    'email': "What's their email address?",
    'capacity': "What's its max load capacity, in kg?",
    'max_distance': "What's its max distance per trip, in km?",
    'base_rate': "What's its base rate, in ZAR?",
}
CONFIRM = '__confirm__'

_YES_RE = re.compile(r"^\s*(y|yes|yeah|yep|sure|ok(ay)?|confirm|correct|do it|add (it|them|him|her))\b", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(n|no|nope|cancel|skip|never\s*mind|forget it|don't)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _try_resolve_existing(table: str, text: str, company) -> Optional[Dict[str, Any]]:
    """If the free text names a real EXISTING record for `table`, resolve to
    it directly instead of failing the current field's validation. Lets a
    user redirect mid-creation-dialog ("actually that's an existing client
    named X", or simply retyping/correcting the name) instead of getting
    stuck re-answering the same field prompt forever — the state machine
    otherwise has no way back out to a real record once it starts collecting
    fields for a new one. Returns the same {'table', 'id', 'name'} shape a
    real creation returns, so callers can treat it identically."""
    from core.models import Customer
    from core.services.llm_quote import _fuzzy_match, match_vehicle_type

    if table == 'customers':
        candidates = list(Customer.objects.filter(company=company).values('id', 'name'))
        names = [c['name'] for c in candidates]
        matched_name = _fuzzy_match(text, names)
    else:
        # Restricted to types the company can actually fulfil (>=1 AVAILABLE
        # vehicle) — otherwise a mid-dialog correction could redirect back to
        # an unavailable global default the New Quote dropdown never offers.
        from core.services.vehicle_types import available_vehicle_types
        candidates = available_vehicle_types(company)
        names = [c['name'] for c in candidates]
        matched_name = match_vehicle_type(text, names)
    if not matched_name:
        return None
    match = next(c for c in candidates if c['name'] == matched_name)
    return {'table': table, 'id': match['id'], 'name': matched_name}


def detect_unmatched(unmatched: Dict[str, Optional[str]], declined: Optional[List[str]]) -> Optional[Tuple[str, str]]:
    """(table, raw_name) for the first unmatched entity not already declined this
    session — customer takes priority when both are mentioned in one message,
    so only one entity conversation ever runs at a time."""
    declined_lc = {(d or '').strip().lower() for d in (declined or [])}
    name = (unmatched.get('customer_name') or '').strip()
    if name and name.lower() not in declined_lc:
        return 'customers', name
    vt = (unmatched.get('vehicle_type') or '').strip()
    if vt and vt.lower() not in declined_lc:
        return 'vehicle_types', vt
    return None


def start_pending(table: str, name: str, user, detected_language: Optional[str] = None
                   ) -> Tuple[Optional[Dict[str, Any]], str, Dict[str, str]]:
    """Returns (pending_entity_or_None, reply_text, link). RBAC-gated up front —
    if the user's role can't create this table, there's no point asking for
    details; just point at the manual-add link instead. `detected_language`,
    when a confidently-detected non-English code, translates the reply text
    on the fly (see language_detect.translate_template); falls through to
    English unchanged when None/'en' or on any translation failure."""
    label = ENTITY_REGISTRY[table]['label']
    link = CUSTOMER_LINK if table == 'customers' else VEHICLE_TYPE_LINK
    if not role_can(user, table, 'create'):
        reply = (f"There's no {label.lower()} named '{name}' in your system, and your role "
                 f"doesn't allow adding one — ask an admin, or add it here.")
        return None, language_detect.translate_template(reply, detected_language), link
    fields = FIELD_ORDER[table]
    pending = {'type': table, 'name': name, 'collected': {}, 'missing': list(fields)}
    question = FIELD_PROMPTS[fields[0]]
    reply = (f"I couldn't find a {label.lower()} named '{name}'. Want me to add them? "
             f"{question} (Or add them manually here.)")
    return pending, language_detect.translate_template(reply, detected_language), link


def advance_pending(pending: Dict[str, Any], message: str, company, user, detected_language: Optional[str] = None
                     ) -> Tuple[Optional[Dict[str, Any]], str, Optional[Dict[str, Any]],
                                Optional[Dict[str, str]], Optional[str]]:
    """One state-machine step for an in-progress entity creation.

    Returns (pending_entity_or_None, reply, created_or_None, link_or_None,
    declined_name_or_None). `created` is {'table', 'id', 'name'} on a
    successful write. `detected_language` translates every reply the same
    way as start_pending (English passthrough when None/'en').
    """
    table = pending.get('type')
    name = pending.get('name', '')
    label = ENTITY_REGISTRY[table]['label']
    text = (message or '').strip()

    def _tr(s: str) -> str:
        return language_detect.translate_template(s, detected_language)

    if _NO_RE.match(text):
        return None, _tr(f"No problem — I'll leave the {label.lower()} unset for now."), None, None, name

    if pending.get('missing') == [CONFIRM]:
        if _YES_RE.match(text):
            return _create(table, name, pending.get('collected') or {}, company, user, pending, detected_language)
        existing = _try_resolve_existing(table, text, company)
        if existing:
            reply = f"Got it — using the existing {label.lower()} '{existing['name']}' instead."
            return None, _tr(reply), existing, None, None
        # Not a clear yes/no — re-ask instead of guessing.
        return pending, _tr(_confirm_summary(table, name, pending.get('collected') or {})), None, None, None

    missing = list(pending.get('missing') or [])
    if not missing:
        return _create(table, name, pending.get('collected') or {}, company, user, pending, detected_language)

    field = missing[0]
    value = _parse_value(field, text)
    if value is None:
        existing = _try_resolve_existing(table, text, company)
        if existing:
            reply = f"Got it — using the existing {label.lower()} '{existing['name']}' instead."
            return None, _tr(reply), existing, None, None
        hint = "That doesn't look like a valid email." if field == 'email' else "I didn't catch a number there."
        return pending, _tr(f"{hint} {FIELD_PROMPTS[field]}"), None, None, None

    collected = {**(pending.get('collected') or {}), field: value}
    remaining = missing[1:]
    if remaining:
        nxt = {'type': table, 'name': name, 'collected': collected, 'missing': remaining}
        return nxt, _tr(FIELD_PROMPTS[remaining[0]]), None, None, None

    nxt = {'type': table, 'name': name, 'collected': collected, 'missing': [CONFIRM]}
    return nxt, _tr(_confirm_summary(table, name, collected)), None, None, None


def _parse_value(field: str, text: str):
    """Parse the raw answer for whichever field is currently being collected.
    'email' is validated by shape; the rest (capacity/max_distance/base_rate)
    are numeric, with the same tons->kg convention as the main weight parser."""
    if field == 'email':
        candidate = text.strip()
        return candidate if _EMAIL_RE.match(candidate) else None
    m = _NUMBER_RE.search(text.replace(',', ''))
    if not m:
        return None
    value = float(m.group(1))
    if field == 'capacity' and re.search(r'\bton', text, re.IGNORECASE) and 'kg' not in text.lower():
        value *= 1000
    return value


def _confirm_summary(table: str, name: str, collected: Dict[str, Any]) -> str:
    if table == 'customers':
        return f"Add '{name}' as a new client with email {collected.get('email')}? (yes/no)"
    return (f"Add '{name}' as a new vehicle type — capacity {collected.get('capacity', 0):.0f} kg, "
            f"max distance {collected.get('max_distance', 0):.0f} km, "
            f"base rate R{collected.get('base_rate', 0):.0f}? (yes/no)")


def _create(table, name, collected, company, user, pending, detected_language: Optional[str] = None):
    """Propose then immediately execute — the user already confirmed via chat,
    so no separate UI confirmation step is needed on top of the yes/no."""
    fields = {'name': name, **collected}
    result = copilot_tools.propose_create(company, user, None, {'table': table, 'fields': fields})
    if 'error' in result:
        reply = f"Couldn't add that: {result['error']} Try again, or say cancel."
        return pending, language_detect.translate_template(reply, detected_language), None, None, None

    from core.models import CopilotProposal
    proposal = CopilotProposal.objects.get(id=result['proposal_id'])
    ok, payload = copilot_tools.execute_proposal(proposal, user, company)
    if not ok:
        reply = f"Couldn't add that: {payload.get('error')} Try again, or say cancel."
        return pending, language_detect.translate_template(reply, detected_language), None, None, None

    new_id = payload['result']['id']
    label = ENTITY_REGISTRY[table]['label']
    reply = f"Added! {label} '{name}' is set."
    return (None, language_detect.translate_template(reply, detected_language),
            {'table': table, 'id': new_id, 'name': name}, None, None)
