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
    "- Locations are South African or SADC cities/towns. Normalise abbreviations "
    "(JHB->Johannesburg, CPT->Cape Town, DBN->Durban, PTA->Pretoria, PE->Port Elizabeth, BFN->Bloemfontein).\n"
    "- weight_kg must be in kilograms. Convert tons/tonnes to kg (1 ton = 1000 kg).\n"
    "- vehicle_type: extract whatever vehicle/truck type the user mentions AS FREE TEXT, in their own "
    "words (e.g. 'rigid truck', 'flatbed', 'reefer', 'semi'). Do NOT restrict this to any fixed list or "
    "reject a value because it looks unfamiliar — the caller matches it against the fleet's real vehicle "
    "types afterwards. If the message is not in English, translate the vehicle/truck type phrase into "
    "its closest common ENGLISH description before returning it (e.g. Afrikaans 'bakvrachtmotor' -> "
    "'flatbed truck', Spanish 'camión refrigerado' -> 'refrigerated truck') — the fleet's real vehicle "
    "types are named in English, and matching only works against English wording. If not mentioned, "
    "return \"\".\n"
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
        extra += f"\n\nThis fleet's configured vehicle types (prefer matching one of these if the user's wording is close): {', '.join(vehicle_types)}."
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
        "pickup_location": {"type": "string"},
        "delivery_location": {"type": "string"},
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


def _fuzzy_match(raw: str, candidates: List[str], cutoff: float = 0.45) -> Optional[str]:
    """Match free text the LLM extracted against a real list of names (vehicle
    types, customers). Exact/case-insensitive first, then a whole-word overlap
    on the meaningful words (handles "rigid" -> "Rigid Truck", but a bare
    "truck" can't collide-match every "*Truck" candidate), then a fuzzy ratio
    as a last resort. Returns None rather than forcing a bad guess when
    nothing is close enough.
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
        for c in candidates:
            if raw_words & set(_significant_words(c)):
                return c

    matches = difflib.get_close_matches(raw, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None


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
            vehicle_types: Optional[List[str]] = None,
            customers: Optional[List[Dict[str, Any]]] = None,
            detected_language: Optional[str] = None,
            ) -> Tuple[Dict[str, Any], str, Dict[str, Optional[str]]]:
    """Return (extracted_fields, reply, unmatched). Raises on SDK/API error so the
    caller can fall back.

    Provider-agnostic: uses Claude when ANTHROPIC_API_KEY is set, else OpenAI (gpt-4o)
    with JSON mode. Both return the same {pickup_location, delivery_location, weight_kg,
    vehicle_type, cargo_description, reply} shape.

    vehicle_types: the calling company's actual VehicleType names (e.g. "Rigid
    Truck", "Semi-Trailer Truck") — the LLM extracts vehicle_type as free text
    and it's fuzzy-matched against this real list, not a hardcoded generic one.
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
    vt_candidates = vehicle_types or VEHICLE_TYPES
    customer_names = [c["name"] for c in customers] if customers else None

    if provider == "anthropic":
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=QUOTE_MODEL,
            max_tokens=600,
            system=_system_prompt(vt_candidates, customer_names, detected_language),
            messages=msgs,
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
    elif provider == "openai":
        client = OpenAI(api_key=_openai_key())
        sys = _system_prompt(vt_candidates, customer_names, detected_language) + (
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
    raw_vt = (data.get("vehicle_type") or "").strip()
    matched_vt = _fuzzy_match(raw_vt, vt_candidates)
    if matched_vt:
        extracted["vehicle_type"] = matched_vt
    elif raw_vt:
        unmatched["vehicle_type"] = raw_vt
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

    return extracted, (data.get("reply") or "").strip(), unmatched
