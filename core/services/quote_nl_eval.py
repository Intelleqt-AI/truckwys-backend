"""Accuracy measurement for the deterministic quote pre-parser against the
phrase fixtures (core/tests/voice_quote_fixtures.py). Used by the test suite
(which asserts a floor) and by `manage.py quote_nl_accuracy` (which prints it).
"""
from typing import Any, Dict, List

from core.services.quote_preparse import preparse


def evaluate(cases: List[Dict[str, Any]], today) -> Dict[str, Any]:
    fields_total = fields_ok = 0
    cases_ok = 0
    per_field: Dict[str, List[int]] = {}
    failures = []
    for c in cases:
        p = preparse(c["text"], today=today, customers=c.get("customers"))
        got = dict(p.fields)
        got["vehicle_hint"] = p.vehicle_hint
        errs = []
        for k, v in c.get("expect", {}).items():
            ok = got.get(k) == v
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
