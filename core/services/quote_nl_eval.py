"""Accuracy measurement for the deterministic quote pre-parser against the
phrase fixtures (core/tests/voice_quote_fixtures.py). Used by the test suite
(which asserts a floor) and by `manage.py quote_nl_accuracy` (which prints it).
"""
from typing import Any, Dict, List

from core.services.quote_preparse import preparse


def _match(key, want, got):
    """Exact, except: a list (other than stops) lists acceptable values; place
    and text values compare case-insensitively; numbers to 0.01."""
    if isinstance(want, list) and key != "stops":
        return any(_match(key, w, got) for w in want)
    if isinstance(want, bool) or want is None:
        return got is want
    if isinstance(want, (int, float)):
        try:
            return abs(float(got) - float(want)) < 0.01 and not isinstance(got, bool)
        except (TypeError, ValueError):
            return False
    if isinstance(want, str) and isinstance(got, str):
        return want.strip().lower() == got.strip().lower()
    if isinstance(want, list) and isinstance(got, list):
        return [str(x).lower() for x in want] == [str(x).lower() for x in got]
    return want == got


def evaluate(cases: List[Dict[str, Any]], today) -> Dict[str, Any]:
    fields_total = fields_ok = 0
    cases_ok = 0
    per_field: Dict[str, List[int]] = {}
    failures = []
    for c in cases:
        p = preparse(c["text"], today=today, customers=c.get("customers"), vehicle_types=c.get("fleet"))
        got = dict(p.fields)
        got["vehicle_hint"] = p.vehicle_hint
        errs = []
        for k, v in c.get("expect", {}).items():
            ok = _match(k, v, got.get(k))
            per_field.setdefault(k, [0, 0])
            per_field[k][0] += ok
            per_field[k][1] += 1
            fields_total += 1
            fields_ok += ok
            if not ok:
                errs.append(f"{k}: want {v!r}, got {got.get(k)!r}")
        for k in c.get("absent", []):
            ok = got.get(k) in (None, "", [])
            per_field.setdefault("(absent) " + k, [0, 0])
            per_field["(absent) " + k][0] += ok
            per_field["(absent) " + k][1] += 1
            fields_total += 1
            fields_ok += ok
            if not ok:
                errs.append(f"{k}: invented {got.get(k)!r}")
        if c.get("not_understood") and not p.not_understood:
            errs.append("expected a not_understood note")
        if errs:
            failures.append({"id": c["id"], "text": c["text"], "errors": errs})
        else:
            cases_ok += 1
    return {
        "cases": len(cases), "cases_ok": cases_ok,
        "fields": fields_total, "fields_ok": fields_ok,
        "field_accuracy": fields_ok / fields_total if fields_total else 1.0,
        "case_accuracy": cases_ok / len(cases) if cases else 1.0,
        "per_field": per_field, "failures": failures,
    }
