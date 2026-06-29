"""LLM-backed business intelligence: executive briefings and explainable risk
narratives, built on top of the existing (deterministic) analytics.

Designed around Claude with a graceful fallback: when no ANTHROPIC_API_KEY is
configured, every function still returns a useful, honest, deterministic result
— and the narrative simply gets richer the moment a key is provided.
"""
import logging
import os
from datetime import datetime, date
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


def _aware_start(d):
    return timezone.make_aware(datetime.combine(d, datetime.min.time()))


def _aware_end(d):
    return timezone.make_aware(datetime.combine(d, datetime.max.time()))


def build_company_metrics(company, from_date=None, to_date=None) -> dict:
    """Assemble a compact, real metrics snapshot from the company's data, scoped to
    the reporting window [from_date, to_date] (defaults to month-to-date).

    Flow metrics (collections, expenses, margin, activity counts) are computed
    WITHIN the window; balance-sheet metrics (outstanding, overdue) are a snapshot
    AS-OF to_date — those are point-in-time, not a window."""
    from core.models import Invoice, Expense, Load, Quote
    from core.services.intelligence import IntelligenceService

    today = timezone.now().date()
    if to_date is None:
        to_date = today
    if from_date is None:
        from_date = to_date.replace(day=1)
    start, end = _aware_start(from_date), _aware_end(to_date)

    invoices = Invoice.objects.filter(company=company)

    # --- Flow: within the window ---
    revenue_collected = invoices.filter(
        status='PAID', paid_at__gte=start, paid_at__lte=end
    ).aggregate(s=Sum('total_amount'))['s'] or Decimal('0')
    expenses_period = Expense.objects.filter(
        company=company, expense_date__gte=from_date, expense_date__lte=to_date
    ).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    net_margin = revenue_collected - expenses_period
    net_margin_pct = (
        float(round((net_margin / revenue_collected * 100), 1)) if revenue_collected else 0.0
    )

    invoices_issued = invoices.filter(issue_date__gte=from_date, issue_date__lte=to_date).count()
    loads_delivered = Load.objects.filter(
        company=company, status='DELIVERED', delivery_date__gte=start, delivery_date__lte=end
    ).count()
    quotes_in_period = Quote.objects.filter(
        company=company, created_at__gte=start, created_at__lte=end
    ).count()

    # --- Snapshot: as-of to_date ---
    overdue_cutoff = min(to_date, today)
    outstanding = invoices.exclude(status='PAID').filter(
        issue_date__lte=to_date
    ).aggregate(s=Sum('balance'))['s'] or Decimal('0')
    overdue = invoices.exclude(status='PAID').filter(
        due_date__lt=overdue_cutoff
    ).aggregate(s=Sum('balance'))['s'] or Decimal('0')

    try:
        recommendations = IntelligenceService(company).generate_recommendations() or []
    except Exception as exc:
        logger.warning("IntelligenceService failed for briefing: %s", exc)
        recommendations = []

    return {
        "company_name": getattr(company, "company_name", "Your company"),
        "period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "invoice_count": invoices.count(),
        "invoices_issued_in_period": invoices_issued,
        "loads_delivered_in_period": loads_delivered,
        "quotes_in_period": quotes_in_period,
        "revenue_collected": _money(revenue_collected),
        "expenses_period": _money(expenses_period),
        "net_margin": _money(net_margin),
        "net_margin_pct": net_margin_pct,
        "outstanding_total": _money(outstanding),
        "overdue_total": _money(overdue),
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
    """Deterministic, honest, period-aware briefing used when no LLM is configured."""
    p = m.get("period") or {}
    window = f"{p.get('from')} to {p.get('to')}" if p.get("from") else "the selected period"
    lines = [
        f"{m['company_name']} — {window}: collected R{m['revenue_collected']:,.0f} against "
        f"R{m['expenses_period']:,.0f} of costs (net margin R{m['net_margin']:,.0f}, "
        f"{m['net_margin_pct']:.1f}%). {m['invoices_issued_in_period']} invoices issued, "
        f"{m['loads_delivered_in_period']} loads delivered, {m['quotes_in_period']} quotes.",
        f"As of {p.get('to', 'today')}: R{m['outstanding_total']:,.0f} outstanding across "
        f"{m['invoice_count']} invoices, of which R{m['overdue_total']:,.0f} is overdue.",
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


def executive_briefing(company, from_date=None, to_date=None) -> dict:
    """Return {narrative, source, ai_available, metrics} for the reporting window.

    Period-aware, RAG-grounded (per-account invoice records), and provider-agnostic
    (reuses the copilot's Anthropic→OpenAI→rules selection). Never raises."""
    metrics = build_company_metrics(company, from_date, to_date)

    # RAG grounding: freshen the per-account invoice index, then pull the records
    # most relevant to a cash/collections briefing. Degrades to "" if RAG is off.
    grounding = ""
    try:
        from core.services import rag
        rag.index_company_invoices(company)
    except Exception as exc:
        logger.warning("RAG index failed for briefing: %s", exc)
    try:
        from core.services import agent as agent_svc
        grounding = agent_svc._retrieved_block(
            company,
            "largest outstanding and overdue invoices, collections and cash exposure this period",
        )
    except Exception as exc:
        logger.warning("RAG retrieve failed for briefing: %s", exc)

    # Provider-agnostic generation: Anthropic if keyed, else OpenAI, else rules.
    try:
        from core.services import agent as agent_svc
        provider = agent_svc._provider()
    except Exception:
        provider = ""

    if not provider:
        result = _fallback_briefing(metrics)
        result["metrics"] = metrics
        return result

    try:
        import json
        period = metrics["period"]
        system = (
            "You are the CFO co-pilot for a South African road-freight operator using TruckWys, "
            "a fleet finance/data/AI platform. Write a crisp executive briefing (3 short paragraphs, "
            f"no preamble) for the reporting window {period['from']} to {period['to']}: "
            "1) profitability & revenue for the period, 2) cash-flow outlook & collections (call out "
            "overdue exposure as-of the period end), 3) the single most important action this week. "
            "Use ZAR (R). Be direct and concrete, and ground EVERY figure ONLY in the metrics JSON and "
            "the retrieved invoice records below — never invent numbers." + grounding
        )
        narrative = agent_svc._llm_generate(
            system,
            [{"role": "user", "content": f"Metrics for the period:\n{json.dumps(metrics, default=str)}"}],
        )
        if not narrative:
            raise ValueError("empty narrative from provider")
        return {"narrative": narrative, "source": provider, "ai_available": True, "metrics": metrics}
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
