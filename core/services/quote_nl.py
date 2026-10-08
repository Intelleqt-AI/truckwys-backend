"""Natural-language / voice quote understanding: deterministic rules first,
LLM only when it can add something, then a strict merge.

    understand(message, ...) -> NLResult

1. quote_preparse.preparse() runs on the message (and on the other-language
   transcript of the same audio, when voice sent one — code-switched speech
   is often half right in each pass).
2. If the rules explained every content word (PreParse.sufficient) the paid
   LLM call is skipped (cost guard, setting QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE,
   default on). Otherwise llm_quote.extract() runs with a 20 s ceiling and no
   retries, on a message with the matched client name redacted; the customer
   list itself is never sent.
3. Merge per field: agreement raises confidence; on disagreement the rules
   win for numbers/dates/flags they are sure of (LLMs mis-convert Afrikaans
   number words), the model wins for free text (street addresses, unusual
   cargo). The model's typed fields were already validated strictly
   (llm_quote.validate_extraction).
4. Reply: the model's reply when it ran, else a deterministic summary written
   natively in English or Afrikaans (no translation call needed for either).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from django.conf import settings

from core.services import quote_preparse as qp

logger = logging.getLogger(__name__)

RULE_PREFERRED = {"weight", "abnormal_load", "pickup_date", "delivery_date", "valid_until", "trip_type", "return_load_booked",
                  "international", "border_post", "driver_nights", "fuel_price_override"}
ESSENTIALS = ("pickup_location", "delivery_location", "cargo_description", "weight")
FIELD_ORDER = ("pickup_location", "delivery_location", "stops", "weight", "cargo_description", "vehicle_type",
               "customer_id", "customer_name", "pickup_date", "delivery_date", "valid_until", "trip_type",
               "return_load_booked", "international", "border_post", "abnormal_load", "driver_nights",
               "fuel_price_override")


@dataclass
class NLResult:
    extracted: Dict[str, Any] = field(default_factory=dict)
    field_confidence: Dict[str, float] = field(default_factory=dict)
    not_understood: List[str] = field(default_factory=list)
    unmatched: Dict[str, Optional[str]] = field(default_factory=lambda: {"customer_name": None,
                                                                          "vehicle_type": None})
    reply: str = ""
    language: Optional[str] = None
    mixed_language: bool = False
    source: str = "rules"          # rules | llm+rules | rules+regex
    llm_used: bool = False
    llm_error: Optional[str] = None
    vehicle_hint: Optional[str] = None
    conflicts: List[str] = field(default_factory=list)
    said: Dict[str, str] = field(default_factory=dict)  # field -> user's own wording (display only)
    vehicle_hint_label: Optional[str] = None  # "Superlink" — the truck word, when no fleet type matched

    def spoken_places(self) -> Dict[str, Optional[str]]:
        """{pickup, delivery}: how the user said each filled place (falls back
        to the filled value when they said it that way), None when not filled."""
        out: Dict[str, Optional[str]] = {}
        for key, name in (("pickup_location", "pickup"), ("delivery_location", "delivery")):
            value = self.extracted.get(key)
            out[name] = (self.said.get(key) or value) if value else None
        return out


def _skip_llm_when_sufficient() -> bool:
    return bool(getattr(settings, "QUOTE_NL_SKIP_LLM_WHEN_RULES_SUFFICE", True))


# ── merge helpers ────────────────────────────────────────────────────────────
def _agree(key: str, a: Any, b: Any) -> bool:
    if a == b:
        return True
    if key == "weight":
        try:
            return abs(float(a) - float(b)) <= 0.02 * max(float(a), float(b))
        except (TypeError, ValueError):
            return False
    if key in ("pickup_location", "delivery_location"):
        ca, cb = qp.canonical_place(str(a)), qp.canonical_place(str(b))
        return bool(ca and ca == cb) or qp.norm_phrase(str(a)) == qp.norm_phrase(str(b))
    if key == "stops" and isinstance(a, list) and isinstance(b, list):
        return [qp.canonical_place(x) or qp.norm_phrase(x) for x in a] == \
               [qp.canonical_place(x) or qp.norm_phrase(x) for x in b]
    if isinstance(a, str) and isinstance(b, str):
        na, nb = qp.norm_phrase(a), qp.norm_phrase(b)
        return na == nb or (len(na) > 3 and (na in nb or nb in na))
    return False


def _looks_like_address(v: Any) -> bool:
    return isinstance(v, str) and bool(re.search(r"\d", v)) and len(v) > 6


def merge(rules: qp.PreParse, llm_fields: Dict[str, Any], llm_conf: Dict[str, float],
          ) -> Tuple[Dict[str, Any], Dict[str, float], List[str]]:
    out: Dict[str, Any] = {}
    conf: Dict[str, float] = {}
    conflicts: List[str] = []
    keys = [k for k in FIELD_ORDER if k in rules.fields or k in llm_fields]
    for k in keys:
        if k == "customer_name" and "customer_id" in keys:
            continue  # handled with customer_id
        r, l = rules.fields.get(k), llm_fields.get(k)
        rc = rules.confidence.get(k, 0.0)
        lc = min(llm_conf.get(k, 0.75), 0.9)
        if k == "customer_id":
            if r is not None and (l is None or rc >= 0.85 or l == r):
                out["customer_id"], out["customer_name"] = r, rules.fields.get("customer_name")
                conf["customer_id"] = conf["customer_name"] = min(0.99, rc + (0.05 if l == r else 0))
            elif l is not None:
                out["customer_id"], out["customer_name"] = l, llm_fields.get("customer_name")
                conf["customer_id"] = conf["customer_name"] = 0.6 if r is not None else lc
                if r is not None:
                    conflicts.append("customer")
            continue
        if r is None:
            out[k], conf[k] = l, lc
        elif l is None:
            out[k], conf[k] = r, rc
        elif _agree(k, r, l):
            # same meaning: keep the more specific wording (a street address the
            # model kept whole beats the bare city the rules found)
            out[k] = l if (k in ("pickup_location", "delivery_location") and _looks_like_address(l)) else r
            conf[k] = round(min(0.99, max(rc, lc) + 0.05), 2)
        else:
            conflicts.append(k)
            if k in RULE_PREFERRED:
                out[k], conf[k] = (r, 0.6) if rc >= 0.85 else (l, 0.55)
            elif k in ("pickup_location", "delivery_location"):
                if _looks_like_address(l):
                    out[k], conf[k] = l, 0.75
                else:
                    out[k], conf[k] = (r, 0.6) if rc >= 0.9 else (l, 0.55)
            else:
                out[k], conf[k] = l, 0.6
    # trip_date (what toll tariffs and border schedules are priced on) is the
    # pickup date, whichever source set it.
    out.pop("trip_date", None)
    if out.get("pickup_date"):
        out["trip_date"], conf["trip_date"] = out["pickup_date"], conf.get("pickup_date", 0.6)
    # Cross-border follows from the final places, whichever source set them.
    countries = {qp.place_country(qp.canonical_place(str(out.get(k) or ""))) for k in
                 ("pickup_location", "delivery_location")}
    countries.discard(None)
    if countries - {"ZA"} and out.get("international") is not True:
        out["international"], conf["international"] = True, 0.9
    return out, conf, conflicts


# ── privacy: client names never reach the model ──────────────────────────────
_LEGAL_SUFFIXES = {"ltd", "limited", "pty", "proprietary", "edms", "bpk", "inc", "plc", "llc", "cc"}
_SOFT_SUFFIXES = {"holdings", "group", "sa", "stores", "co", "company"}
_COMMON_WORDS = qp.COMMON_NAME_WORDS


def _phrase_rx(phrase: str) -> Optional[str]:
    toks = re.findall(r"[A-Za-z0-9&]+", phrase)
    if not toks:
        return None
    parts = []
    for t in toks:
        if t.lower() == "n":
            parts.append(r"(?:n|'n|and|&)")
        elif t == "&":
            parts.append(r"(?:&|and|en)")
        else:
            parts.append(re.escape(t))
    return r"(?i)(?<![A-Za-z0-9])" + r"[\s\W_]*".join(parts) + r"(?![A-Za-z0-9])"


def _client_phrases(name: str) -> List[str]:
    """Phrases that identify one client: the full name, the name without its
    legal suffixes ("Tiger Brands", "Super Group", "SA Steel Mills"), and the
    core without Holdings/Group/SA/Stores ("Pick n Pay", "Steel Mills") —
    each only when it is 2+ words, or a single distinctive word (5+ letters,
    not a common word or a place), or an acronym ("AVI", "RCL")."""
    words = re.findall(r"[A-Za-z0-9&]+", re.sub(r"\((?:pty|edms)\)", " ", name or "", flags=re.I))
    if not words:
        return []
    no_legal = [w for w in words if w.lower() not in _LEGAL_SUFFIXES]
    core = [w for w in no_legal if w.lower() not in _SOFT_SUFFIXES]
    out = []
    for ws in (words, no_legal, core):
        if not ws:
            continue
        phrase = " ".join(ws)
        if len(ws) >= 2:
            out.append(phrase)
        else:
            w = ws[0]
            if (len(w) >= 5 and w.lower() not in _COMMON_WORDS and not qp.canonical_place(w)
                    and w.lower() not in qp._KNOWN_VOCAB) or (w.isupper() and len(w) >= 3):
                out.append(w)
    return list(dict.fromkeys(out))


def _redaction_names(rules: qp.PreParse, customers: Optional[List[Dict[str, Any]]],
                     current_fields: Optional[Dict[str, Any]],
                     history: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Every client mention the model must never see: each customer's
    identifying phrases (see _client_phrases), the client the form has
    selected, and whatever the user called the client in this message or in
    an earlier turn ("for X", "client is X")."""
    names: List[str] = []
    if rules.customer_span_text:
        names.append(rules.customer_span_text)
    for turn in history or []:
        if isinstance(turn, dict) and turn.get("role") == "user":
            said = qp.explicit_customer_mention(turn.get("content") or turn.get("text") or "", customers)
            if said:
                names.append(said)
    full = [c.get("name") or "" for c in (customers or [])]
    for k, v in (current_fields or {}).items():
        if isinstance(v, str) and re.search(r"client|customer|klient", k, re.I):
            full.append(v)
    if rules.fields.get("customer_name"):
        full.append(rules.fields["customer_name"])
    for n in full:
        names.extend(_client_phrases(n))
    return [n for n in dict.fromkeys(names) if n]


def _redact(text: str, names: List[str]) -> str:
    for n in sorted(set(names), key=len, reverse=True):
        rx = _phrase_rx(n)
        if rx:
            text = re.sub(rx, "the client", text)
    return text


def _redact_value(v: Any, names: List[str]) -> Any:
    if isinstance(v, str):
        return _redact(v, names)
    if isinstance(v, dict):
        return {k: _redact_value(x, names) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact_value(x, names) for x in v]
    return v


# ── validation shared by every source that isn't the strict LLM validator ──
def validate_fields(fields: Dict[str, Any], today: Optional[date] = None) -> Tuple[Dict[str, Any], List[str]]:
    """The same range/date/enum checks llm_quote.validate_extraction applies,
    for values from the legacy regex (and as a final guard on the merge).
    Returns (kept, notes)."""
    from datetime import timedelta
    today = today or qp._sast_today()
    out: Dict[str, Any] = {}
    notes: List[str] = []
    for k, v in fields.items():
        if v in (None, "", []):
            continue
        if k == "weight":
            try:
                w = float(v)
            except (TypeError, ValueError):
                continue
            if qp.MIN_WEIGHT_KG <= w <= qp.MAX_WEIGHT_KG:
                out[k] = w
            else:
                notes.append(f"weight {w / 1000:g} t looks wrong")
        elif k in ("pickup_date", "delivery_date", "valid_until", "trip_date"):
            try:
                d = date.fromisoformat(str(v)[:10])
            except ValueError:
                continue
            if today - timedelta(days=1) <= d <= today + timedelta(days=qp.MAX_DATE_AHEAD_DAYS):
                out[k] = d.isoformat()
            else:
                notes.append(f"date {d.isoformat()} is out of range")
        elif k == "trip_type":
            if str(v).upper() in ("ONE_WAY", "ROUND_TRIP"):
                out[k] = str(v).upper()
        elif k in ("pickup_location", "delivery_location"):
            text = str(v).strip()[:200]
            if text and qp.norm_phrase(text) not in qp._FILLER:
                out[k] = qp.geocodable_place(text)
        elif k in ("cargo_description", "customer_name", "vehicle_type"):
            text = str(v).strip()[:200]
            if text:
                out[k] = text
        else:
            out[k] = v
    if out.get("pickup_date") and out.get("delivery_date") and out["delivery_date"] < out["pickup_date"]:
        out.pop("delivery_date")
        notes.append("delivery date is before pickup date")
    return out, notes


# ── replies ──────────────────────────────────────────────────────────────────
def _num(v: float) -> str:
    s = f"{v:,.1f}".rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",")


def _money(v: float) -> str:
    """SA money: "R 24,10", "R 1 250,00"."""
    return "R " + f"{float(v):,.2f}".replace(",", " ").replace(".", ",")


def _short_date(iso: str, lang: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    months_en = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    months_af = "Jan Feb Mrt Apr Mei Jun Jul Aug Sep Okt Nov Des".split()
    return f"{d.day} {(months_af if lang == 'af' else months_en)[d.month - 1]}"


def _summary(f: Dict[str, Any], lang: str) -> List[str]:
    af = lang == "af"
    bits = []
    if f.get("weight") or f.get("cargo_description"):
        cargo = f.get("cargo_description") or ""
        w = f"{_num(f['weight'] / 1000)} t" if f.get("weight") else ""
        bits.append(" ".join(x for x in (w, cargo) if x))
    if f.get("pickup_location") or f.get("delivery_location"):
        loc = (lambda x: x)  # the filled value, exactly as it appears in the field
        route = f"{loc(f.get('pickup_location') or '?')} → {loc(f.get('delivery_location') or '?')}"
        if f.get("stops"):
            route += (" oor " if af else " via ") + ", ".join(loc(x) for x in f["stops"])
        bits.append(route)
    if f.get("trip_type") == "ROUND_TRIP":
        bits.append("heen en terug, gelaai" if af else "round trip, loaded both ways")
    elif f.get("return_load_booked") is True:
        bits.append("eenrigting, retoervrag bespreek" if af else "one way, return load booked")
    elif f.get("return_load_booked") is False:
        bits.append("eenrigting, leeg terug" if af else "one way, empty back")
    elif f.get("trip_type") == "ONE_WAY":
        bits.append("eenrigting" if af else "one way")
    if f.get("vehicle_type"):
        bits.append(f["vehicle_type"])
    if f.get("customer_name"):
        bits.append(("kliënt " if af else "client ") + f["customer_name"])
    if f.get("pickup_date"):
        bits.append(("oplaai " if af else "pickup ") + _short_date(f["pickup_date"], lang))
    if f.get("delivery_date"):
        bits.append(("aflewer " if af else "deliver ") + _short_date(f["delivery_date"], lang))
    if f.get("international"):
        bp = f.get("border_post")
        bits.append(("oorgrens" if af else "cross-border") + (f" ({bp})" if bp else ""))
    if f.get("abnormal_load"):
        bits.append("abnormale vrag" if af else "abnormal load")
    if f.get("driver_nights"):
        n = f["driver_nights"]
        bits.append(f"{n} {'nagte' if af else 'nights'}" if n != 1 else ("1 nag" if af else "1 night"))
    if f.get("fuel_price_override"):
        bits.append(f"diesel {_money(f['fuel_price_override'])}/L")
    return bits


_MISSING_LABEL = {
    "en": {"pickup_location": "pickup", "delivery_location": "delivery", "cargo_description": "cargo",
           "weight": "weight"},
    "af": {"pickup_location": "oplaaiplek", "delivery_location": "aflaaiplek", "cargo_description": "goedere",
           "weight": "gewig"},
}

_AF_NOTES = [
    (r"^weight (.+) t looks wrong$", r"gewig \1 t lyk verkeerd"),
    (r"^more than one weight mentioned$", "meer as een gewig genoem"),
    (r"^number (.+) — tons or kg\?$", r"getal \1 — ton of kg?"),
    (r"^volume (.+) L given — what does it weigh\?$", r"volume \1 L — hoeveel weeg dit?"),
    (r"^place “(.+)” not recognised — check it on the map$",
     r"plek “\1” nie herken nie — kyk op die kaart"),
    (r"^pickup and delivery are the same place$", "oplaai- en aflaaiplek is dieselfde"),
    (r"^delivery date is before pickup date$", "afleweringsdatum is voor die oplaaidatum"),
    (r"^date (.+) is out of range$", r"datum \1 val buite bereik"),
    (r"^“(.+)” — which day\?$", r"“\1” — watter dag?"),
    (r"^(.+) driver nights looks wrong$", r"\1 bestuurdersnagte lyk verkeerd"),
    (r"^fuel price (.+) looks wrong$", r"brandstofprys \1 lyk verkeerd"),
    (r"^one-way and round trip both mentioned$", "eenrigting en heen-en-terug albei genoem"),
    (r"^round trip and a return-load note both mentioned$", "heen-en-terug en retoervrag albei genoem"),
    (r"^two (.+)s mentioned$", r"twee datums genoem"),
    (r"^(\d+) (\w+) is in the past \u2014 which date\?$",
     lambda m: f"{m.group(1)} {_AF_MON.get(m.group(2), m.group(2))} is verby \u2014 watter datum?"),
    (r"^more than one weight mentioned \u2014 which is the load\?$", "meer as een gewig genoem \u2014 watter is die vrag?"),
    (r"^border post \u201c(.+)\u201d not recognised$", r"grenspos \u201c\1\u201d nie herken nie"),
]


_AF_MON = {"Mar": "Mrt", "May": "Mei", "Oct": "Okt", "Dec": "Des"}


def localise_note(note: str, lang: Optional[str]) -> str:
    if lang != "af":
        return note
    for pat, rep in _AF_NOTES:
        if re.match(pat, note):
            return re.sub(pat, rep, note)
    return note


def _end(text: str) -> str:
    return text if text.endswith(("?", ".", "!")) else text + "."


def compose_reply(merged: Dict[str, Any], extracted: Dict[str, Any], not_understood: List[str],
                  lang: Optional[str]) -> str:
    """Short confirmation of what was filled, what's still needed and what
    wasn't understood — natively English or Afrikaans; other languages get the
    English text translated by the caller's existing translate_template."""
    L = "af" if lang == "af" else "en"
    parts = []
    if extracted:
        s = "; ".join(_summary(extracted, L))
        parts.append(("Ingevul: " if L == "af" else "Filled: ") + s + ".")
    missing = [_MISSING_LABEL[L][k] for k in ESSENTIALS
               if not (merged.get(k) or (k == "weight" and merged.get("weight_kg")))]
    if not_understood:
        parts.append(("Nie verstaan nie: " if L == "af" else "Didn't catch: ")
                     + _end("; ".join(not_understood[:3])))
    if missing and extracted:
        parts.append(("Nog nodig: " if L == "af" else "Still need: ") + ", ".join(missing) + ".")
    elif not missing and extracted:
        parts.append("Gereed om te prys." if L == "af" else "Ready to price.")
    if not extracted:
        parts.append("Ek kon nie vragbesonderhede uitmaak nie. Probeer bv. “28 ton staalrolle van Joburg na "
                     "Durban, môre”." if L == "af" else
                     "I couldn't pick out load details. Try e.g. “28 t steel coils, Joburg to Durban, "
                     "tomorrow”.")
    return " ".join(parts)


# ── entry point ──────────────────────────────────────────────────────────────
def understand(message: str, *, history: Optional[List[Dict[str, Any]]] = None,
               current_fields: Optional[Dict[str, Any]] = None,
               vehicle_types: Optional[List[Any]] = None,
               customers: Optional[List[Dict[str, Any]]] = None,
               detected_language: Optional[str] = None,
               alternate_text: Optional[str] = None,
               today: Optional[date] = None,
               legacy_extract: Optional[Callable[[], Tuple[Dict[str, Any], Dict[str, Optional[str]]]]] = None,
               all_vehicle_types: Optional[List[Any]] = None,
               ) -> NLResult:
    """`vehicle_types` = types the company can fulfil now (the dropdown; what
    the model is offered). `all_vehicle_types` = every type the company has
    (a spoken truck word is matched against these too; default: the same)."""
    from core.services import llm_quote

    match_types = all_vehicle_types if all_vehicle_types is not None else vehicle_types
    rules = qp.preparse(message, today=today, customers=customers, vehicle_types=match_types)
    if alternate_text and alternate_text.strip() and alternate_text.strip() != (message or "").strip():
        alt = qp.preparse(alternate_text, today=today, customers=customers, vehicle_types=match_types)
        for k, v in alt.fields.items():
            if k not in rules.fields:
                rules.set(k, v, alt.confidence.get(k, 0.5) * 0.8)
        if not rules.vehicle_hint and alt.vehicle_hint:
            rules.vehicle_hint, rules.vehicle_hint_label = alt.vehicle_hint, alt.vehicle_hint_label
            rules.unmatched["vehicle_type"] = rules.unmatched["vehicle_type"] or alt.unmatched["vehicle_type"]
        if not rules.customer_span_text:
            rules.customer_span_text = alt.customer_span_text
        # an alternate pass that explains the leftovers lets the rules stand alone
        if alt.sufficient and set(alt.fields) >= set(rules.fields):
            rules.residue = []
    res = NLResult(language=detected_language or rules.language_hint, mixed_language=rules.mixed_language,
                   vehicle_hint=rules.vehicle_hint)
    res.unmatched = dict(rules.unmatched)

    llm_fields: Dict[str, Any] = {}
    llm_conf: Dict[str, float] = {}
    llm_notes: List[str] = []
    llm_reply = ""
    # Cost guard: a message the rules fully explained, carrying at least two of
    # the essentials, gains nothing from a paid call. Short follow-ups ("28
    # ton") still go to the model — they lean on conversation context.
    essentials_found = sum(1 for k in ESSENTIALS if k in rules.fields)
    rules_suffice = rules.sufficient and essentials_found >= 2
    want_llm = llm_quote.is_enabled() and not (_skip_llm_when_sufficient() and rules_suffice)
    if want_llm:
        try:
            # Privacy: every client name is removed from the message, from ALL
            # history turns (the user's and this backend's own replies, e.g.
            # "client Clover Industries Ltd") and from the form fields, before
            # anything is sent. The customer list itself never is.
            names = _redaction_names(rules, customers, current_fields, history)
            msg = _redact(message, names)
            hist = [{**t, **{k: _redact(t[k], names) for k in ("content", "text") if isinstance(t.get(k), str)}}
                    for t in (history or []) if isinstance(t, dict)]
            cur = {k: _redact_value(v, names) for k, v in (current_fields or {}).items()
                   if not re.search(r"client|customer|klient", str(k), re.I)}
            got = llm_quote.extract(msg, hist, cur, vehicle_types=vehicle_types, customers=customers,
                                    detected_language=res.language, return_meta=True)
            if len(got) == 4:
                llm_fields, llm_reply, llm_unmatched, meta = got
                llm_conf = meta.get("field_confidence") or {}
                llm_notes = meta.get("not_understood") or []
            else:  # older 3-tuple shape (tests' mocks)
                llm_fields, llm_reply, llm_unmatched = got
            for k, v in (llm_unmatched or {}).items():
                if v and not res.unmatched.get(k):
                    res.unmatched[k] = v
            res.llm_used = True
            res.source = "llm+rules"
        except Exception as exc:  # timeout, provider error, bad JSON — rules carry on
            logger.warning("quote_nl: LLM extraction failed, using rules only: %s", exc)
            res.llm_error = type(exc).__name__

    # A client the model offered that the user never named (it only saw "the
    # client" or nothing at all) is not filled.
    if llm_fields.get("customer_id") and not rules.fields.get("customer_id"):
        named = llm_fields.get("customer_name") or ""
        user_text = " ".join([message or ""] + [str(t.get("content") or t.get("text") or "")
                                                for t in (history or []) if isinstance(t, dict)
                                                and t.get("role") == "user"])
        if not any(re.search(_phrase_rx(p) or "$^", user_text) for p in _client_phrases(named)):
            llm_fields = {k: v for k, v in llm_fields.items() if k not in ("customer_id", "customer_name")}
    extracted, conf, conflicts = merge(rules, llm_fields, llm_conf)
    res.conflicts = conflicts

    # Fields the rules found and rejected (900 t, two weights, a date in the
    # past) are never filled from another source: the note stands instead.
    flagged = set()
    for about in rules.not_understood_fields:
        if about == "weight_invalid":
            flagged.add("weight")
        elif about == "date":
            flagged.update({"pickup_date", "delivery_date", "trip_date"} - set(rules.fields))
        elif about in ("delivery_date", "driver_nights", "fuel_price_override"):
            flagged.add(about)
    for k in flagged:
        if k not in rules.fields:
            extracted.pop(k, None)
            conf.pop(k, None)

    if not res.llm_used and legacy_extract is not None:
        try:
            legacy, legacy_unmatched = legacy_extract()
            legacy, _ = validate_fields(legacy, today)
            added = False
            for k, v in legacy.items():
                if k not in extracted and k not in flagged:
                    extracted[k], conf[k] = v, 0.6
                    added = True
            for k, v in (legacy_unmatched or {}).items():
                if v and not res.unmatched.get(k) and k != "vehicle_type":
                    res.unmatched[k] = v
            if added:
                res.source = "rules+regex"
        except Exception:
            logger.warning("quote_nl: legacy extractor failed", exc_info=True)

    # Cross-border only on evidence: a real border post, a place outside SA,
    # or the rules' own keyword — never just the model's say-so.
    if extracted.get("international") is True and rules.fields.get("international") is not True:
        countries = {qp.place_country(qp.canonical_place(str(extracted.get(k) or "")))
                     for k in ("pickup_location", "delivery_location")}
        if not extracted.get("border_post") and not (countries - {None, "ZA"}):
            extracted.pop("international")
            conf.pop("international", None)
    final, final_notes = validate_fields(extracted, today)
    for k in set(extracted) - set(final):
        conf.pop(k, None)
    extracted = final
    if extracted.get("pickup_date"):
        extracted["trip_date"], conf["trip_date"] = extracted["pickup_date"], conf.get("pickup_date", 0.6)
    else:
        extracted.pop("trip_date", None)

    # A truck word that isn't one of the fleet's types is never turned into an
    # "add a vehicle type?" dialog here: it's returned as vehicle_hint and the
    # client offers "Pick a truck". It's matched against EVERY type the
    # company has first (not only the ones with a vehicle free today).
    raw_vt = res.unmatched.get("vehicle_type")
    if not extracted.get("vehicle_type") and raw_vt and match_types:
        records = [v if isinstance(v, dict) else {"name": v} for v in match_types]
        hit = llm_quote.match_vehicle_type(raw_vt, [r["name"] for r in records])
        if hit:
            extracted["vehicle_type"], conf["vehicle_type"] = hit, 0.7
    res.vehicle_hint_label = None if extracted.get("vehicle_type") else (rules.vehicle_hint_label or raw_vt)
    if not res.vehicle_hint and res.vehicle_hint_label:
        res.vehicle_hint = "other"
    if extracted.get("vehicle_type"):
        res.vehicle_hint = res.vehicle_hint if res.vehicle_hint != "other" else None
    res.unmatched["vehicle_type"] = None
    if extracted.get("customer_id"):
        res.unmatched["customer_name"] = None

    # Notes: every rule note stays, except a bare-number/volume question the
    # model answered from context with a valid weight.
    notes = []
    for text, about in zip(rules.not_understood, rules.not_understood_fields):
        if about == "weight_missing" and extracted.get("weight") and res.llm_used:
            continue
        notes.append(localise_note(text, res.language))
    for n in llm_notes + final_notes:
        n = localise_note(n, res.language)
        if n not in notes:
            notes.append(n)
    res.not_understood = notes[:5]
    res.extracted = extracted
    # The user's own wording ("Kaapstad") for display beside the geocodable
    # value ("Cape Town"), only where the final value is the one the rules read.
    res.said = {k: v for k, v in rules.said.items() if extracted.get(k) == rules.fields.get(k)}
    res.field_confidence = {k: conf[k] for k in extracted if k in conf}

    if res.llm_used and llm_reply:
        res.reply = llm_reply
    else:
        merged = {**(current_fields or {}), **extracted}
        res.reply = compose_reply(merged, extracted, res.not_understood, res.language) if extracted else ""
    return res
