"""TruckWys AI Agent — a conversational fleet-finance copilot.

Built on the same principle as llm_insights: Claude when an ANTHROPIC_API_KEY is
configured, and a genuinely useful, deterministic rule-based responder otherwise.
Either way it answers grounded ONLY in the company's real data and never invents
figures. The agent is read-only: it answers questions and proposes navigation
("control") actions the UI renders as buttons — it does not mutate data itself.
"""
import json
import logging
import os
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum, Count
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    ANTHROPIC_AVAILABLE = False

AGENT_MODEL = os.environ.get("CLAUDE_AGENT_MODEL", "claude-opus-4-8")


def _llm_enabled() -> bool:
    return ANTHROPIC_AVAILABLE and bool(
        os.environ.get("ANTHROPIC_API_KEY") or getattr(settings, "ANTHROPIC_API_KEY", "")
    )


def _money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def build_agent_context(company) -> dict:
    """Assemble a compact, real snapshot of the company for grounding the agent."""
    from core.models import Invoice, Quote, Vehicle, Customer

    today = timezone.now().date()

    invoices = Invoice.objects.filter(company=company)
    outstanding = invoices.exclude(status='PAID').aggregate(s=Sum('balance'))['s'] or Decimal('0')
    overdue_qs = invoices.filter(due_date__lt=today).exclude(status='PAID')
    overdue = overdue_qs.aggregate(s=Sum('balance'))['s'] or Decimal('0')
    collected = invoices.filter(status='PAID').aggregate(s=Sum('total_amount'))['s'] or Decimal('0')

    quotes = Quote.objects.filter(company=company)
    quote_counts = {row['status']: row['n'] for row in quotes.values('status').annotate(n=Count('id'))}

    vehicles = Vehicle.objects.filter(company=company)
    vehicle_counts = {row['status']: row['n'] for row in vehicles.values('status').annotate(n=Count('id'))}

    # Capital / fast-pay eligibility (mirror the real engine count, cheaply)
    eligible_count, eligible_value, top_eligible = _capital_summary(company)

    top_overdue = [
        {
            "invoice": inv.invoice_number,
            "customer": inv.customer.name if inv.customer else "—",
            "balance": _money(inv.balance),
            "days_overdue": (today - inv.due_date).days if inv.due_date else 0,
        }
        for inv in overdue_qs.select_related('customer').order_by('due_date')[:5]
    ]

    top_customers = [
        {"name": c.name, "outstanding": _money(
            invoices.filter(customer=c).exclude(status='PAID').aggregate(s=Sum('balance'))['s'] or 0
        )}
        for c in Customer.objects.filter(company=company)[:50]
    ]
    top_customers = sorted(top_customers, key=lambda x: x['outstanding'], reverse=True)[:5]

    return {
        "company_name": getattr(company, "company_name", "Your company"),
        "currency": "ZAR (R)",
        "today": today.isoformat(),
        "invoices": {
            "count": invoices.count(),
            "outstanding_total": _money(outstanding),
            "overdue_total": _money(overdue),
            "overdue_count": overdue_qs.count(),
            "revenue_collected": _money(collected),
            "top_overdue": top_overdue,
        },
        "quotes": {
            "total": quotes.count(),
            "by_status": quote_counts,
        },
        "fleet": {
            "total": vehicles.count(),
            "by_status": vehicle_counts,
        },
        "capital": {
            "eligible_invoices": eligible_count,
            "eligible_value": eligible_value,
            "top_eligible": top_eligible,
        },
        "top_customers_by_outstanding": top_customers,
    }


def _capital_summary(company):
    """Eligible-invoice summary + the single best advanceable invoice (best-effort)."""
    from core.models import Invoice, Facility
    try:
        facility = Facility.objects.filter(company=company, status='ACTIVE').first()
        if not facility:
            return 0, 0.0, None
        from core.services.risk_engine import RiskEngine
        candidates = Invoice.objects.filter(
            company=company, status__in=['SENT', 'OVERDUE']
        ).exclude(
            advance_requests__status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
        ).select_related('customer', 'trip', 'load')[:25]
        count = 0
        value = Decimal('0')
        eligible = []
        for inv in candidates:
            try:
                res = RiskEngine(invoice=inv, facility=facility).calculate_risk_score()
            except Exception:
                continue
            if res.is_eligible:
                count += 1
                value += Decimal(str(res.net_advance))
                eligible.append({
                    'invoice_id': inv.id,
                    'invoice_number': inv.invoice_number,
                    'customer': inv.customer.name if inv.customer else '—',
                    'net_payout': _money(res.net_advance),
                    'tier': res.risk_tier,
                })
        eligible.sort(key=lambda x: x['net_payout'], reverse=True)
        return count, _money(value), (eligible[0] if eligible else None)
    except Exception as exc:
        logger.warning("capital summary failed for agent: %s", exc)
        return 0, 0.0


# ---------------------------------------------------------------------------
# Suggested "control" actions — navigation the UI renders as buttons.
# ---------------------------------------------------------------------------
_ACTION_LIBRARY = {
    "quotes": {"label": "Open Quotes", "route": "/quotes"},
    "new_quote": {"label": "New Quote", "route": "/quotes/new"},
    "bookings": {"label": "Open Bookings", "route": "/bookings"},
    "invoices": {"label": "Open Invoices", "route": "/finance/invoices"},
    "capital": {"label": "Open Fast Pay", "route": "/capital"},
    "fleet": {"label": "Open Fleet", "route": "/fleet/vehicles"},
    "insights": {"label": "Open Insights", "route": "/insights"},
}


def _suggest_actions(text: str) -> list:
    t = (text or "").lower()
    keys = []
    if any(w in t for w in ["quote", "pricing", "rate", "pipeline", "board"]):
        keys += ["quotes", "new_quote"]
    if any(w in t for w in ["booking", "load", "shipment", "trip"]):
        keys.append("bookings")
    if any(w in t for w in ["invoice", "overdue", "outstanding", "collect", "debtor", "cash"]):
        keys.append("invoices")
    if any(w in t for w in ["advance", "fast pay", "capital", "factor", "finance"]):
        keys.append("capital")
    if any(w in t for w in ["vehicle", "fleet", "truck", "driver"]):
        keys.append("fleet")
    if any(w in t for w in ["margin", "insight", "brief", "forecast", "performance"]):
        keys.append("insights")
    # de-dup preserving order
    seen, actions = set(), []
    for k in keys:
        if k not in seen and k in _ACTION_LIBRARY:
            seen.add(k)
            actions.append(_ACTION_LIBRARY[k])
    return actions[:4]


# ---------------------------------------------------------------------------
# Rule-based fallback responder (used when Claude isn't configured).
# ---------------------------------------------------------------------------
def _fallback_reply(ctx: dict, user_text: str) -> str:
    t = (user_text or "").lower().strip()
    inv = ctx["invoices"]
    cap = ctx["capital"]
    fleet = ctx["fleet"]
    quotes = ctx["quotes"]

    def fmt(n):
        return f"R{n:,.0f}"

    if not t or any(w in t for w in ["help", "what can you", "hello", "hi ", "hey"]):
        return (
            f"I'm your TruckWys copilot for {ctx['company_name']}. I can answer questions about your "
            f"cash position, overdue invoices, quotes pipeline, fleet status and fast-pay eligibility. "
            f"Try: \"What's overdue?\", \"How much can I advance?\", \"How's my pipeline?\" or \"Fleet status\"."
        )

    if any(w in t for w in ["overdue", "late", "collect", "debtor"]):
        if inv["overdue_count"]:
            lines = [f"You have {inv['overdue_count']} overdue invoice(s) totalling {fmt(inv['overdue_total'])}."]
            for o in inv["top_overdue"][:3]:
                lines.append(f"• {o['invoice']} — {o['customer']}: {fmt(o['balance'])}, {o['days_overdue']} days overdue.")
            lines.append("Chasing the oldest first usually recovers cash fastest.")
            return " ".join(lines)
        return "Good news — nothing is overdue right now."

    if any(w in t for w in ["outstanding", "owed", "receivable", "cash position", "how much.*owed"]):
        return (
            f"{fmt(inv['outstanding_total'])} is outstanding across {inv['count']} invoices "
            f"({fmt(inv['overdue_total'])} overdue). {fmt(inv['revenue_collected'])} has been collected to date."
        )

    if any(w in t for w in ["advance", "fast pay", "capital", "factor"]):
        if cap["eligible_invoices"]:
            top = cap.get("top_eligible")
            msg = (
                f"{cap['eligible_invoices']} invoice(s) are eligible for fast pay right now, "
                f"worth about {fmt(cap['eligible_value'])} in net advances."
            )
            if top:
                msg += (
                    f" The best is {top['invoice_number']} ({top['customer']}) at "
                    f"R{top['net_payout']:,.0f} net — I can request that advance for you now."
                )
            return msg
        return "No invoices are currently eligible for fast pay (they need a delivered load with proof of delivery)."

    if any(w in t for w in ["quote", "pipeline", "pricing"]):
        by = quotes["by_status"]
        parts = ", ".join(f"{v} {k.lower()}" for k, v in by.items()) or "none yet"
        return f"Your pipeline has {quotes['total']} quotes ({parts}). I can open the board or start a new quote."

    if any(w in t for w in ["fleet", "vehicle", "truck", "driver"]):
        by = fleet["by_status"]
        parts = ", ".join(f"{v} {k.replace('_',' ').lower()}" for k, v in by.items()) or "none"
        return f"Your fleet has {fleet['total']} vehicles ({parts})."

    # Default: give the cash headline and point to insights.
    return (
        f"Here's the headline for {ctx['company_name']}: {fmt(inv['outstanding_total'])} outstanding, "
        f"{fmt(inv['overdue_total'])} overdue, {cap['eligible_invoices']} invoice(s) ready for fast pay. "
        f"Ask me about overdue accounts, fast-pay capacity, your quotes pipeline or fleet status."
    )


def _propose_action(ctx: dict, user_text: str):
    """Detect an intent the copilot can ACT on, and return a confirmable action.

    Currently supports requesting a fast-pay advance on the best eligible invoice.
    The action is a proposal — the UI must confirm before the endpoint is called.
    """
    t = (user_text or "").lower()
    wants_advance = any(w in t for w in [
        "advance", "fast pay", "fast-pay", "factor", "finance this", "get cash", "draw down", "request advance",
    ])
    top = (ctx.get("capital") or {}).get("top_eligible")
    if wants_advance and top:
        return {
            "type": "request_advance",
            "method": "POST",
            "endpoint": "api/v1/advances/",
            "body": {"invoice_id": top["invoice_id"]},
            "label": f"Request advance on {top['invoice_number']}",
            "detail": f"Net payout R{top['net_payout']:,.0f} · {top['customer']} · {top['tier']} tier",
            "confirm_text": "Request advance",
            "success_text": f"Advance requested on {top['invoice_number']} — R{top['net_payout']:,.0f} net.",
        }
    return None


def agent_respond(company, messages: list) -> dict:
    """Return {reply, source, ai_available, actions, proposed_action}. Never raises.

    messages: [{"role": "user"|"assistant", "content": str}, ...]
    """
    ctx = build_agent_context(company)
    last_user = next((m.get("content", "") for m in reversed(messages or []) if m.get("role") == "user"), "")
    actions = _suggest_actions(last_user)
    proposed_action = _propose_action(ctx, last_user)

    if not _llm_enabled():
        return {
            "reply": _fallback_reply(ctx, last_user),
            "source": "rules",
            "ai_available": False,
            "actions": actions,
            "proposed_action": proposed_action,
        }

    try:
        client = anthropic.Anthropic()
        system = (
            "You are the TruckWys copilot — an AI agent for a South African road-freight operator. "
            "TruckWys is a fleet finance/data/AI platform (quotes, bookings, invoicing, and a Capital "
            "fast-pay/factoring product). Answer concisely and practically, grounded ONLY in the JSON "
            "company snapshot provided — never invent figures. Use ZAR (R). When a number isn't in the "
            "snapshot, say you don't have it rather than guessing. You are read-only: suggest what the "
            "user should do, but never claim to have changed anything. Keep replies under ~120 words."
            f"\n\nCompany snapshot:\n{json.dumps(ctx, default=str)}"
        )
        convo = [
            {"role": m["role"], "content": str(m.get("content", ""))}
            for m in (messages or [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ][-12:]
        if not convo:
            convo = [{"role": "user", "content": "Give me a quick status of my business."}]
        response = client.messages.create(
            model=AGENT_MODEL,
            max_tokens=700,
            system=system,
            messages=convo,
        )
        reply = next((b.text for b in response.content if b.type == "text"), "").strip()
        return {"reply": reply, "source": "llm", "ai_available": True, "actions": actions, "proposed_action": proposed_action}
    except Exception as exc:
        logger.warning("agent LLM failed, using fallback: %s", exc)
        return {
            "reply": _fallback_reply(ctx, last_user),
            "source": "rules",
            "ai_available": False,
            "actions": actions,
            "proposed_action": proposed_action,
        }
