"""LLM-backed business intelligence: executive briefings and explainable risk
narratives, built on top of the existing (deterministic) analytics.

Designed around Claude with a graceful fallback: when no ANTHROPIC_API_KEY is
configured, every function still returns a useful, honest, deterministic result
— and the narrative simply gets richer the moment a key is provided.
"""
import logging
import os
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    ANTHROPIC_AVAILABLE = False

INSIGHTS_MODEL = os.environ.get("CLAUDE_INSIGHTS_MODEL", "claude-opus-4-8")


def _llm_enabled() -> bool:
    return ANTHROPIC_AVAILABLE and bool(
        os.environ.get("ANTHROPIC_API_KEY") or getattr(settings, "ANTHROPIC_API_KEY", "")
    )


def _money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def build_company_metrics(company) -> dict:
    """Assemble a compact, real metrics snapshot from the company's data."""
    from core.models import Invoice
    from core.services.intelligence import IntelligenceService

    today = timezone.now().date()
    invoices = Invoice.objects.filter(company=company)

    outstanding = invoices.exclude(status='PAID').aggregate(s=Sum('balance'))['s'] or Decimal('0')
    overdue = invoices.filter(due_date__lt=today).exclude(status='PAID').aggregate(s=Sum('balance'))['s'] or Decimal('0')
    paid_total = invoices.filter(status='PAID').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')

    try:
        recommendations = IntelligenceService(company).generate_recommendations() or []
    except Exception as exc:
        logger.warning("IntelligenceService failed for briefing: %s", exc)
        recommendations = []

    return {
        "company_name": getattr(company, "company_name", "Your company"),
        "invoice_count": invoices.count(),
        "outstanding_total": _money(outstanding),
        "overdue_total": _money(overdue),
        "revenue_collected": _money(paid_total),
        "top_recommendations": [
            {
                "type": r.get("type") or r.get("category"),
                "severity": r.get("severity"),
                "title": r.get("title") or r.get("message"),
                "detail": r.get("detail") or r.get("description"),
            }
            for r in recommendations[:6]
        ],
    }


def _fallback_briefing(m: dict) -> dict:
    """Deterministic, honest briefing used when Claude isn't configured."""
    lines = [
        f"{m['company_name']} has R{m['outstanding_total']:,.0f} outstanding across "
        f"{m['invoice_count']} invoices, of which R{m['overdue_total']:,.0f} is overdue. "
        f"R{m['revenue_collected']:,.0f} has been collected to date."
    ]
    recs = m.get("top_recommendations") or []
    if recs:
        lines.append("Top actions: " + "; ".join(
            f"{r['title']}" for r in recs if r.get("title")
        )[:600] + ".")
    else:
        lines.append("No critical alerts right now — cash position looks stable.")
    return {
        "narrative": " ".join(lines),
        "source": "rules",
        "ai_available": False,
    }


def executive_briefing(company) -> dict:
    """Return {narrative, source, ai_available, metrics}. Never raises."""
    metrics = build_company_metrics(company)

    if not _llm_enabled():
        result = _fallback_briefing(metrics)
        result["metrics"] = metrics
        return result

    try:
        client = anthropic.Anthropic()
        system = (
            "You are the CFO co-pilot for a South African road-freight operator using TruckWys, "
            "a fleet finance/data/AI platform. Write a crisp executive briefing (3 short paragraphs, "
            "no preamble): 1) profitability & revenue, 2) cash-flow outlook & collections (call out "
            "overdue exposure), 3) the single most important action this week. Use ZAR (R). Be direct, "
            "concrete, and grounded ONLY in the numbers provided — never invent figures."
        )
        import json
        response = client.messages.create(
            model=INSIGHTS_MODEL,
            max_tokens=900,
            system=system,
            messages=[{"role": "user", "content": f"Here is the data:\n{json.dumps(metrics, default=str)}"}],
        )
        narrative = next((b.text for b in response.content if b.type == "text"), "").strip()
        return {"narrative": narrative, "source": "llm", "ai_available": True, "metrics": metrics}
    except Exception as exc:
        logger.warning("LLM briefing failed, using fallback: %s", exc)
        result = _fallback_briefing(metrics)
        result["metrics"] = metrics
        return result


def explain_risk_score(risk_score) -> dict:
    """Plain-English underwriting rationale for a RiskScore (Claude or fallback)."""
    factors = {
        "payment_history": getattr(risk_score, "factor_payment_history", None),
        "invoice_age": getattr(risk_score, "factor_invoice_age", None),
        "pod_quality": getattr(risk_score, "factor_pod_quality", None),
        "debtor_credit": getattr(risk_score, "factor_debtor_credit", None),
        "client_financial": getattr(risk_score, "factor_client_financial", None),
        "operational": getattr(risk_score, "factor_operational", None),
        "macro_market": getattr(risk_score, "factor_macro_market", None),
    }
    factors = {k: v for k, v in factors.items() if v is not None}
    total = getattr(risk_score, "total_score", None)
    tier = getattr(risk_score, "tier", None)

    if not _llm_enabled():
        strong = [k for k, v in factors.items() if v and v >= 80]
        weak = [k for k, v in factors.items() if v and v <= 50]
        bits = [f"Overall score {total} ({tier})."]
        if strong:
            bits.append("Strengths: " + ", ".join(s.replace('_', ' ') for s in strong) + ".")
        if weak:
            bits.append("Watch-outs: " + ", ".join(w.replace('_', ' ') for w in weak) + ".")
        return {"explanation": " ".join(bits), "source": "rules", "ai_available": False}

    try:
        import json
        client = anthropic.Anthropic()
        system = (
            "You are a freight-finance credit analyst. In 2-3 sentences, explain this carrier/invoice "
            "risk score to a lender in plain English: what drives it and the key risk. Ground ONLY in "
            "the factor scores (0-100, higher=better) provided."
        )
        payload = {"total_score": total, "tier": tier, "factors": factors}
        response = client.messages.create(
            model=INSIGHTS_MODEL,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        return {"explanation": text, "source": "llm", "ai_available": True}
    except Exception as exc:
        logger.warning("LLM risk explanation failed: %s", exc)
        return {"explanation": f"Overall score {total} ({tier}).", "source": "rules", "ai_available": False}
