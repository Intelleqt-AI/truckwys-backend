"""LLM-backed natural-language quote extraction using Claude.

This is the primary path for /api/v1/ai/chat-quote/. It degrades gracefully:
when the Anthropic SDK isn't installed or no API key is configured, the caller
falls back to the regex extractor in views_ai_quote.py.

Model is configurable via CLAUDE_QUOTE_MODEL (default: claude-opus-4-8).
For a faster / cheaper per-quote path, set CLAUDE_QUOTE_MODEL=claude-haiku-4-5.
"""
import difflib
import json
import logging
import os
import re
from collections import Counter
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    ANTHROPIC_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    OPENAI_AVAILABLE = False

QUOTE_MODEL = os.environ.get("CLAUDE_QUOTE_MODEL", "claude-opus-4-8")
OPENAI_QUOTE_MODEL = (
    os.environ.get("OPENAI_QUOTE_MODEL")
    or getattr(settings, "OPENAI_CHAT_MODEL", "") or "gpt-4o"
)

# Fallback only — used when the caller has no company context (tests, or a
# request whose user has no company) to offer the LLM as candidates. The real
# fleet's vehicle types (fetched per-request from VehicleType, including the
# global company=None defaults every company can see) always take priority —
# see extract()'s vehicle_types param. Kept in sync with the names actually
# seeded by core/migrations/0041_seed_default_vehicle_types.py so this
# fallback never diverges from what a real company could have on file.
VEHICLE_TYPES = [
    "Light Delivery Vehicle (LDV)", "Medium Truck (4–8 tonnes)", "Heavy Truck (8–16 tonnes)",
    "Interlink / B-Train (34 tonnes)", "Semi-Truck / Horse & Trailer (30 tonnes)",
    "Flatbed Truck", "Refrigerated Truck (Reefer)", "Tanker",
]

SYSTEM_PROMPT_BASE = (
    "You are the quoting assistant for TruckWys, a South African road-freight platform. "
    "Extract structured load details from the user's message and the conversation so far.\n"
    "- Locations: if the user gives a specific street address (a street number/name, building, or "
    "landmark — not just a city), extract and return that exact address as they said it, in full — "
    "do NOT shorten it to just the city or area name. Only when they mention nothing more specific "
    "than a city/town, normalise abbreviations (JHB->Johannesburg, CPT->Cape Town, DBN->Durban, "
    "PTA->Pretoria, PE->Port Elizabeth, BFN->Bloemfontein) and return that city/town name. Always "
    "prefer the most specific location detail actually given, for both pickup and delivery.\n"
    "- weight_kg must be in kilograms. Convert tons/tonnes to kg (1 ton = 1000 kg).\n"
    "- vehicle_type: if 'This fleet's configured vehicle types' is listed below and the vehicle the "
    "user describes plausibly corresponds to one of them — by body style (flatbed, reefer, tanker...), "
    "by tonnage/weight mentioned anywhere in the conversation, or by common synonym (e.g. 'rigid "
    "truck'/'box truck' most likely means one of the fleet's Truck entries, sized by whatever weight "
    "was mentioned) — return that configured entry's PLAIN NAME EXACTLY as listed, not a paraphrase. "
    "Some entries show their real max load as '(max ~X t)' — that is this fleet's actual capacity for "
    "that type; weigh it when a weight was mentioned (don't pick one whose max is clearly below the "
    "stated weight if a bigger configured type would fit), but NEVER include the '(max ~X t)' text "
    "itself in the value you return, only the name before it. Only when nothing in that list is a "
    "reasonable match, extract whatever the user said AS FREE TEXT instead — the caller will offer to "
    "add it as a new type. Do NOT reject a value just because it looks unfamiliar. If the message is "
    "not in English, translate the vehicle/truck type phrase into its closest common ENGLISH "
    "description before returning it (e.g. Afrikaans 'bakvrachtmotor' -> 'flatbed truck', Spanish "
    "'camión refrigerado' -> 'refrigerated truck') — the fleet's real vehicle types are named in "
    "English, and matching only works against English wording. If not mentioned, return \"\".\n"
    "- customer_name: the name of the client/customer this quote is for, as free text, if the user "
    "mentions one (e.g. 'client is Acme', 'for John', 'customer will be Maru'). Extract exactly what "
    "they said, even a short/partial name — the caller matches it against real customer records "
    "afterwards. If no client is mentioned, return \"\".\n"
    "- cargo_description is the goods being moved (e.g. 'steel coils', 'pallets of beverages').\n"
    "- pickup_date, delivery_date and valid_until are dates in YYYY-MM-DD format. Resolve relative "
    "phrases ('today', 'tomorrow', 'in 5 days', '5 days from now', 'next Monday') against TODAY'S DATE "
    "given below — do the arithmetic yourself, don't guess. If a date isn't mentioned, return \"\".\n"
    "- trip_type is \"ONE_WAY\" or \"ROUND_TRIP\" — infer from phrases like 'one way'/'one-way' -> "
    "ONE_WAY, 'round trip'/'return trip'/'there and back' -> ROUND_TRIP. If not mentioned, return \"\".\n"
    "- For any field you cannot determine from the conversation, return an empty string \"\" "
    "(or 0 for weight_kg). Do NOT guess or invent values.\n"
    "- 'reply' is one short, friendly sentence that RESPONDS TO WHAT THE USER ACTUALLY SAID:\n"
    "  * If they just greet you ('hi', 'hello') or ask what you can do / how you can help, greet "
    "them back and say in one sentence that you build freight quotes from a plain-English "
    "description of a load, then invite them to describe the trip — or, if some details are already "
    "captured, ask for the next missing essential. Do NOT answer as if they had given load details.\n"
    "  * If they give or add load details, confirm what you captured and ask for any still-missing "
    "essentials (pickup, delivery, cargo, weight).\n"
    "  * If they ask what vehicle types or clients/customers are available, answer using the fleet's "
    "configured vehicle types / known clients lists given below (if provided) — list them out — then "
    "invite them to continue describing the load. If no list was provided, say you don't have that "
    "list handy and suggest they check the vehicle-type/client dropdowns instead.\n"
    "  * If they ask something unrelated to freight or quoting, politely say you're focused on "
    "building quotes and steer back to the load.\n"
    "  Never ignore a direct question by simply repeating a field request."
)


def _system_prompt(vehicle_types: Optional[List[str]] = None, customer_names: Optional[List[str]] = None,
                    detected_language: Optional[str] = None) -> str:
    # Built per-call (not a module constant) so "today"/"tomorrow"/"in N days"
    # always resolve against the real current date, not whenever this process
    # happened to start, and so the fleet's actual vehicle types/customers (a
    # per-company list) can be offered as hints without hard-constraining them.
    today = date.today()
    extra = ""
    if vehicle_types:
        extra += f"\n\nThis fleet's configured vehicle types (return one of these EXACTLY when the user's described vehicle plausibly matches — see the vehicle_type rule above): {', '.join(vehicle_types)}."
    if customer_names:
        extra += f"\n\nKnown clients for this company (prefer matching one of these if the user's wording is close): {', '.join(customer_names)}."
    if detected_language:
        # Authoritative — sourced from Whisper's own language detection (voice)
        # or a dedicated text-language detector (typed), never from this model's
        # own reading of the message. Deliberately unconditional (fires even for
        # 'en') so there is one rule, not an English-default special case.
        extra += (
            f"\n\nLANGUAGE (authoritative — from the transcription/language-detection pipeline, not "
            f"your own guess): the user's message has been confidently detected as language code "
            f"'{detected_language}'. You MUST write the 'reply' field entirely in that language. Do not "
            f"switch to English unless this code is exactly 'en'. Never invent or guess a different "
            f"language than the one given here."
        )
    return f"{SYSTEM_PROMPT_BASE}\n\nTODAY'S DATE: {today.isoformat()} ({today.strftime('%A')}).{extra}"


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "pickup_location": {
            "type": "string",
            "description": "The pickup location exactly as given — a full street address if the "
                            "user provided one, otherwise just the city/town name. Never shorten a "
                            "given street address down to only its city.",
        },
        "delivery_location": {
            "type": "string",
            "description": "The delivery location exactly as given — a full street address if the "
                            "user provided one, otherwise just the city/town name. Never shorten a "
                            "given street address down to only its city.",
        },
        "weight_kg": {"type": "number"},
        "vehicle_type": {"type": "string"},
        "customer_name": {"type": "string"},
        "cargo_description": {"type": "string"},
        "pickup_date": {"type": "string"},
        "delivery_date": {"type": "string"},
        "valid_until": {"type": "string"},
        "trip_type": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": [
        "pickup_location", "delivery_location", "weight_kg",
        "vehicle_type", "customer_name", "cargo_description",
        "pickup_date", "delivery_date", "valid_until", "trip_type",
        "reply",
    ],
    "additionalProperties": False,
}


# Words generic enough that sharing one is meaningless for matching — every
# "*Truck" vehicle-type name contains "truck", so a noisy transcription that
# only picks up that word must not be allowed to collide-match any of them.
_GENERIC_MATCH_WORDS = {"truck", "vehicle", "trailer"}


def _significant_words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _GENERIC_MATCH_WORDS]


# Legal/corporate-entity suffix words common across almost any real client
# list, regardless of how a specific tenant's names happen to be spelled —
# never trustworthy as a word-overlap match signal on their own, unlike the
# per-call frequency check below (which only catches a word once it's
# actually repeated in THIS company's own candidate list).
_GENERIC_ENTITY_WORDS = {
    "ltd", "limited", "pty", "proprietary", "inc", "incorporated", "corp",
    "corporation", "llc", "plc", "co", "group", "holdings", "company",
    "enterprises", "sa",
}


def _fuzzy_match(raw: str, candidates: List[str], cutoff: float = 0.45) -> Optional[str]:
    """Match free text the LLM extracted against a real list of names
    (customers — the only remaining caller; vehicle types use the stricter
    match_vehicle_type). Exact/case-insensitive first, then a whole-word
    overlap on the meaningful words (handles "shefat" -> "Shefat Ahmed"),
    then a fuzzy ratio as a last resort. Returns None rather than forcing a
    bad guess when nothing is close enough.

    The word-overlap tier only accepts a shared word that DISTINGUISHES one
    candidate from the rest of this same candidate list — i.e. it occurs in
    exactly one of them AND isn't a generic legal/corporate-entity suffix
    (_GENERIC_ENTITY_WORDS). A word shared by several real customers (a
    common corporate suffix like "Ltd"/"Group"/"Holdings"/"(Pty)", found in
    most company name lists) carries no matching signal on its own: a
    mis-heard "Nempec Ltd" must not resolve to whichever "... Ltd" candidate
    happens to come first, when the real "Nampak Ltd" is further down the
    list. The per-candidate-list frequency check is computed per call, so it
    adapts to whatever names each tenant actually has on file — but it isn't
    enough by itself: a generic suffix can still look "rare" by pure spelling
    coincidence (e.g. one client spelled "Limited" while every other client
    in the same list spells the identical suffix "Ltd" instead, making
    "limited" numerically unique without being an identifying word) — hence
    the fixed stoplist on top of it.
    """
    raw = (raw or "").strip()
    if not raw or not candidates:
        return None
    raw_lc = raw.lower()
    for c in candidates:
        if c.lower() == raw_lc:
            return c

    raw_words = set(_significant_words(raw))
    if raw_words:
        word_counts = Counter(w for c in candidates for w in set(_significant_words(c)))
        for c in candidates:
            distinguishing = raw_words & {
                w for w in _significant_words(c)
                if w not in _GENERIC_ENTITY_WORDS and word_counts[w] == 1
            }
            if distinguishing:
                return c

    matches = difflib.get_close_matches(raw, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None


_VEHICLE_MATCH_CUTOFF = 0.75


def _alpha_words(text: str) -> List[str]:
    """Significant words with pure-digit tokens dropped too — "(Variant 16)"
    must not overlap-match "Heavy Truck (8-16 tonnes)" on the shared "16"."""
    return [w for w in _significant_words(text) if not w.isdigit()]


def match_vehicle_type(raw: str, names: List[str]) -> Optional[str]:
    """Match free text (LLM output, or a user's chat correction) to a real
    fleet vehicle-type name. Deliberately not `_fuzzy_match`: every name in a
    real fleet tends to end in "Truck", so difflib on the raw strings scores
    e.g. "Heavy Truck" against "Tanker Truck" at ~0.61 — high enough to
    silently substitute an unrelated real type for one that was removed from
    the candidate list (because the company owns none of it). So the fuzzy
    tier here compares the significant-word form (generic
    "truck"/"vehicle"/"trailer" stripped) at a much stricter cutoff instead of
    comparing raw strings loosely.
    """
    raw = (raw or "").strip()
    if not raw or not names:
        return None
    raw_lc = raw.lower()
    for n in names:
        if n.lower() == raw_lc:
            return n

    raw_words = set(_alpha_words(raw))
    if raw_words:
        for n in names:
            if raw_words & set(_alpha_words(n)):
                return n

    norm = {" ".join(_significant_words(n)): n for n in names}
    hit = difflib.get_close_matches(
        " ".join(_significant_words(raw)), list(norm), n=1, cutoff=_VEHICLE_MATCH_CUTOFF)
    return norm[hit[0]] if hit else None


def _candidate_labels(records: List[Dict[str, Any]]) -> List[str]:
    """"Flatbed Truck (max ~20 t)" when capacity is known and plausible, bare
    name otherwise — never a raw capacity value, since VehicleType.capacity
    is a live mix of tonnes and kilograms across rows (see
    vehicle_types.capacity_tonnes)."""
    out = []
    for r in records:
        c = r.get("capacity_t")
        out.append(f"{r['name']} (max ~{c:g} t)" if c else r["name"])
    return out


_CAPACITY_TOLERANCE = 0.02  # ignore rounding noise, not real overload


def _resolve_vehicle_type(raw_vt: str, records: List[Dict[str, Any]], weight_kg: Optional[float],
                           ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """-> (matched_name, unmatched_raw, note).

    Matches `raw_vt` against `records` (this company's real, fulfillable
    vehicle types), then — only when a numeric weight is known — checks the
    match's real capacity against it. An undersized match is upgraded to the
    smallest available type that actually covers the weight; if nothing in
    the fleet can carry it, the type is left unset rather than forcing a bad
    pick. `note`, when set, is one plain-English sentence explaining what
    happened — a substitution or an "nothing fits" admission must never be
    silent, or the mismatch just resurfaces later as confusion over pricing.
    """
    names = [r["name"] for r in records]
    caps = {r["name"]: r.get("capacity_t") for r in records}
    matched = match_vehicle_type(raw_vt, names)

    if not weight_kg or weight_kg <= 0:
        return (matched, None, None) if matched else (None, (raw_vt or None), None)

    known = sorted((c, n) for n, c in caps.items() if c)  # plausible capacities only
    cap = caps.get(matched) if matched else None
    if matched and cap and weight_kg > cap * 1000 * (1 + _CAPACITY_TOLERANCE):
        covering = [(c, n) for c, n in known if c * 1000 >= weight_kg]
        if covering:
            better = covering[0][1]
            return better, None, (
                f"{matched} tops out at {cap:g} t, so I've set {better} for this "
                f"{weight_kg / 1000:g} t load."
            )
        biggest = known[-1] if known else None
        note = (
            f"Nothing in your available fleet carries {weight_kg / 1000:g} t"
            + (f" — the largest is {biggest[1]} at {biggest[0]:g} t." if biggest else ".")
            + " I've left the vehicle type unset."
        )
        return None, None, note

    return (matched, None, None) if matched else (None, (raw_vt or None), None)


def _known_weight_kg(current_fields: Optional[Dict[str, Any]], extracted: Dict[str, Any]) -> Optional[float]:
    """The weight (kg) known for this quote so far, checked in priority
    order: this turn's own extraction, then `weight_kg` (what the live
    frontend actually sends in current_fields), then `weight` (what
    extract() itself emits, and what existing tests construct) — extraction
    used to only ever check the last of these, silently missing the real
    frontend payload shape."""
    for v in (extracted.get("weight"),
              (current_fields or {}).get("weight_kg"),
              (current_fields or {}).get("weight")):
        try:
            f = float(v or 0)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def _anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or getattr(settings, "ANTHROPIC_API_KEY", "")


def _openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY") or getattr(settings, "OPENAI_API_KEY", "")


def _provider() -> str:
    """Pick the LLM provider for extraction, preferring Anthropic when both keys exist."""
    if ANTHROPIC_AVAILABLE and _anthropic_key():
        return "anthropic"
    if OPENAI_AVAILABLE and _openai_key():
        return "openai"
    return ""


def is_enabled() -> bool:
    """True when LLM-backed extraction can run on either provider."""
    return bool(_provider())


def _build_messages(message: str, history: Optional[List[Dict[str, Any]]],
                    current_fields: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    for turn in (history or []):
        role = turn.get("role")
        content = turn.get("content") or turn.get("text")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})

    known = {k: v for k, v in (current_fields or {}).items() if v not in (None, "", 0)}
    prefix = ""
    if known:
        prefix = f"Already captured so far: {json.dumps(known)}\n\n"
    messages.append({"role": "user", "content": f"{prefix}New message: {message}"})

    # The Messages API requires the first message to be from the user.
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": message})
    return messages


def extract(message: str, history: Optional[List[Dict[str, Any]]] = None,
            current_fields: Optional[Dict[str, Any]] = None,
            vehicle_types: Optional[List[Any]] = None,
            customers: Optional[List[Dict[str, Any]]] = None,
            detected_language: Optional[str] = None,
            ) -> Tuple[Dict[str, Any], str, Dict[str, Optional[str]]]:
    """Return (extracted_fields, reply, unmatched). Raises on SDK/API error so the
    caller can fall back.

    Provider-agnostic: uses Claude when ANTHROPIC_API_KEY is set, else OpenAI (gpt-4o)
    with JSON mode. Both return the same {pickup_location, delivery_location, weight_kg,
    vehicle_type, cargo_description, reply} shape.

    vehicle_types: the calling company's actual, currently fulfillable
    VehicleType records — either [{'name': str, 'capacity_t': float|None}, ...]
    (what views_ai_quote.py's AIChatQuoteView passes, from
    core.services.vehicle_types.available_vehicle_types) or a flat list of
    plain names (accepted for backward compatibility). The LLM extracts
    vehicle_type as free text; it's matched against this real list (never a
    hardcoded generic one) and, when a weight is known, cross-checked against
    the matched entry's real capacity — see _resolve_vehicle_type. None means
    no company context (tests, superuser): falls back to the hardcoded
    VEHICLE_TYPES catalog with capacity unknown. [] is meaningful — "this
    company can fulfil nothing right now" — and is never replaced by the
    fallback catalog.
    customers: [{'id': int, 'name': str}, ...] for the calling company — same
    fuzzy-match treatment, returned as customer_id/customer_name when matched.

    unmatched: {'customer_name': str|None, 'vehicle_type': str|None} — the raw
    free text the caller mentioned when it did NOT match any real record (as
    opposed to not being mentioned at all). Lets the view offer to create it
    instead of silently dropping it — a real name that doesn't exist must never
    be presented to the user as if it had been captured.

    detected_language: an authoritative language code from the transcription
    pipeline (Whisper for voice) or a dedicated text-language detector (typed),
    NOT this model's own guess — see language_detect.py. None means uncertain/
    unavailable, in which case no language instruction is added and today's
    default (English-leaning) model judgment applies, unchanged.
    """
    msgs = _build_messages(message, history, current_fields)
    provider = _provider()
    # None (no company context — tests, superuser) still falls back to the
    # hardcoded catalog, capacity unknown. [] (a company that can fulfil
    # NOTHING right now) must NOT be replaced by it — that's exactly how an
    # unavailable global default ("Heavy Truck (8-16 tonnes)") got offered as
    # if this company actually had one.
    if vehicle_types is None:
        vt_records = [{"name": n, "capacity_t": None} for n in VEHICLE_TYPES]
    elif vehicle_types and not isinstance(vehicle_types[0], dict):
        vt_records = [{"name": n, "capacity_t": None} for n in vehicle_types]
    else:
        vt_records = list(vehicle_types)
    customer_names = [c["name"] for c in customers] if customers else None

    if provider == "anthropic":
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=QUOTE_MODEL,
            max_tokens=600,
            temperature=0,
            system=_system_prompt(_candidate_labels(vt_records), customer_names, detected_language),
            messages=msgs,
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
    elif provider == "openai":
        client = OpenAI(api_key=_openai_key())
        sys = _system_prompt(_candidate_labels(vt_records), customer_names, detected_language) + (
            "\n\nRespond ONLY with a JSON object with exactly these keys: pickup_location, "
            "delivery_location, weight_kg, vehicle_type, customer_name, cargo_description, "
            "pickup_date, delivery_date, valid_until, trip_type, reply."
        )
        response = client.chat.completions.create(
            model=OPENAI_QUOTE_MODEL,
            max_tokens=600,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys}, *msgs],
        )
        data = json.loads(response.choices[0].message.content or "{}")
    else:
        raise RuntimeError("No LLM provider configured for quote extraction")

    extracted: Dict[str, Any] = {}
    unmatched: Dict[str, Optional[str]] = {"customer_name": None, "vehicle_type": None}
    if data.get("pickup_location"):
        extracted["pickup_location"] = data["pickup_location"].strip()
    if data.get("delivery_location"):
        extracted["delivery_location"] = data["delivery_location"].strip()
    raw_customer_name = (data.get("customer_name") or "").strip()
    if customers:
        matched_name = _fuzzy_match(raw_customer_name, customer_names)
        if matched_name:
            match = next(c for c in customers if c["name"] == matched_name)
            extracted["customer_id"] = match["id"]
            extracted["customer_name"] = matched_name
        elif raw_customer_name:
            unmatched["customer_name"] = raw_customer_name
    if data.get("cargo_description"):
        extracted["cargo_description"] = data["cargo_description"].strip()
    try:
        weight = float(data.get("weight_kg") or 0)
    except (TypeError, ValueError):
        weight = 0
    if weight > 0:
        extracted["weight"] = weight

    # Vehicle type is resolved after weight so a stated tonnage can be
    # cross-checked against the matched type's real capacity — see
    # _resolve_vehicle_type. known_weight_kg falls back to whatever the form
    # already had when this turn didn't mention a new weight.
    raw_vt = (data.get("vehicle_type") or "").strip()
    known_weight_kg = _known_weight_kg(current_fields, extracted)
    matched_vt, unmatched_vt, note = _resolve_vehicle_type(raw_vt, vt_records, known_weight_kg)
    if matched_vt:
        extracted["vehicle_type"] = matched_vt
    if unmatched_vt:
        unmatched["vehicle_type"] = unmatched_vt

    for date_field in ("pickup_date", "delivery_date", "valid_until"):
        raw = (data.get(date_field) or "").strip()
        if raw:
            try:
                date.fromisoformat(raw[:10])  # validate shape; reject anything malformed
                extracted[date_field] = raw[:10]
            except ValueError:
                logger.debug("llm_quote: ignoring unparseable %s %r", date_field, raw)

    trip_type = (data.get("trip_type") or "").strip().upper()
    if trip_type in ("ONE_WAY", "ROUND_TRIP"):
        extracted["trip_type"] = trip_type

    reply = (data.get("reply") or "").strip()
    if note:
        from core.services import language_detect
        reply = f"{reply} {language_detect.translate_template(note, detected_language)}".strip()

    return extracted, reply, unmatched
