"""LLM-backed natural-language quote extraction using Claude.

This is the primary path for /api/v1/ai/chat-quote/. It degrades gracefully:
when the Anthropic SDK isn't installed or no API key is configured, the caller
falls back to the regex extractor in views_ai_quote.py.

Model is configurable via CLAUDE_QUOTE_MODEL (default: claude-opus-4-8).
For a faster / cheaper per-quote path, set CLAUDE_QUOTE_MODEL=claude-haiku-4-5.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    ANTHROPIC_AVAILABLE = False

QUOTE_MODEL = os.environ.get("CLAUDE_QUOTE_MODEL", "claude-opus-4-8")

VEHICLE_TYPES = ["Flatbed", "Tautliner", "Refrigerated", "Tanker", "Box Truck", "Danger Load"]

SYSTEM_PROMPT = (
    "You are the quoting assistant for TruckWys, a South African road-freight platform. "
    "Extract structured load details from the user's message and the conversation so far.\n"
    "- Locations are South African or SADC cities/towns. Normalise abbreviations "
    "(JHB->Johannesburg, CPT->Cape Town, DBN->Durban, PTA->Pretoria, PE->Port Elizabeth, BFN->Bloemfontein).\n"
    "- weight_kg must be in kilograms. Convert tons/tonnes to kg (1 ton = 1000 kg).\n"
    f"- vehicle_type must be exactly one of: {', '.join(VEHICLE_TYPES)} "
    "(map reefer/fridge->Refrigerated, curtainsider->Tautliner, dg/dangerous goods->Danger Load).\n"
    "- cargo_description is the goods being moved (e.g. 'steel coils', 'pallets of beverages').\n"
    "- For any field you cannot determine from the conversation, return an empty string \"\" "
    "(or 0 for weight_kg). Do NOT guess or invent values.\n"
    "- 'reply' is one short, friendly sentence: confirm what you captured and ask for any "
    "still-missing essentials (pickup, delivery, cargo, weight)."
)

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "pickup_location": {"type": "string"},
        "delivery_location": {"type": "string"},
        "weight_kg": {"type": "number"},
        "vehicle_type": {"type": "string"},
        "cargo_description": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": [
        "pickup_location", "delivery_location", "weight_kg",
        "vehicle_type", "cargo_description", "reply",
    ],
    "additionalProperties": False,
}


def is_enabled() -> bool:
    """True when Claude-backed extraction can run."""
    if not ANTHROPIC_AVAILABLE:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or getattr(settings, "ANTHROPIC_API_KEY", ""))


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
            current_fields: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], str]:
    """Return (extracted_fields, reply). Raises on SDK/API error so the caller can fall back."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=QUOTE_MODEL,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=_build_messages(message, history, current_fields),
        output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
    )

    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)

    extracted: Dict[str, Any] = {}
    if data.get("pickup_location"):
        extracted["pickup_location"] = data["pickup_location"].strip()
    if data.get("delivery_location"):
        extracted["delivery_location"] = data["delivery_location"].strip()
    if data.get("vehicle_type") and data["vehicle_type"] in VEHICLE_TYPES:
        extracted["vehicle_type"] = data["vehicle_type"]
    if data.get("cargo_description"):
        extracted["cargo_description"] = data["cargo_description"].strip()
    try:
        weight = float(data.get("weight_kg") or 0)
    except (TypeError, ValueError):
        weight = 0
    if weight > 0:
        extracted["weight"] = weight

    return extracted, (data.get("reply") or "").strip()
