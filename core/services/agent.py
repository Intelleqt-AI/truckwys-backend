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

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    OPENAI_AVAILABLE = False

AGENT_MODEL = os.environ.get("CLAUDE_AGENT_MODEL", "claude-opus-4-8")
OPENAI_CHAT_MODEL = (
    os.environ.get("OPENAI_CHAT_MODEL") or getattr(settings, "OPENAI_CHAT_MODEL", "") or "gpt-4o"
)


def _anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or getattr(settings, "ANTHROPIC_API_KEY", "")


def _openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY") or getattr(settings, "OPENAI_API_KEY", "")


def _provider() -> str:
    """Which LLM provider generates the reply, based on configured keys.

    COPILOT_LLM_PROVIDER ('auto'|'openai'|'anthropic') can force one. In 'auto'
    (default) Anthropic is preferred when its key is set — preserving existing
    installs — otherwise OpenAI. Returns '' when neither is usable (→ rules).
    Generation and embeddings are independent: this only picks the generator.
    """
    pref = (os.environ.get("COPILOT_LLM_PROVIDER")
            or getattr(settings, "COPILOT_LLM_PROVIDER", "auto") or "auto").lower()
    has_anthropic = ANTHROPIC_AVAILABLE and bool(_anthropic_key())
    has_openai = OPENAI_AVAILABLE and bool(_openai_key())

    if pref == "anthropic":
        return "anthropic" if has_anthropic else ""
    if pref == "openai":
        return "openai" if has_openai else ""
    # auto
    if has_anthropic:
        return "anthropic"
    if has_openai:
        return "openai"
    return ""


def _llm_enabled() -> bool:
    return bool(_provider())


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

    # Billing & short-pay audit (carrier-side): money unbilled / underbilled / owed.
    billing = {}
    try:
        from core.services.billing_audit import audit_billing
        audit = audit_billing(company)
        billing = audit.get('summary', {})
        billing['top_unbilled'] = audit.get('unbilled', [])[:3]
        billing['top_shortpaid'] = audit.get('shortpaid', [])[:3]
    except Exception as exc:
        logger.warning('billing audit for agent failed: %s', exc)

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
        "billing_audit": billing,
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
        return 0, 0.0, None


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
    if any(w in t for w in ["invoice", "overdue", "outstanding", "collect", "debtor", "cash", "bill", "unbilled", "short pay", "owed", "recover", "audit"]):
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

    if any(w in t for w in ["bill", "unbilled", "short pay", "short-pay", "underbilled", "recover", "owed", "uninvoiced", "audit"]):
        b = ctx.get("billing_audit") or {}
        rec = b.get("total_recoverable", 0)
        if not rec:
            return "Your billing looks clean — nothing unbilled, underbilled or short-paid that I can see."
        bits = []
        if b.get("unbilled_value"):
            bits.append(f"{fmt(b['unbilled_value'])} in {b.get('unbilled_count', 0)} delivered load(s) not yet invoiced")
        if b.get("underbilled_value"):
            bits.append(f"{fmt(b['underbilled_value'])} under-billed vs the load value")
        if b.get("shortpaid_value"):
            bits.append(f"{fmt(b['shortpaid_value'])} owed across {b.get('shortpaid_count', 0)} short-paid/outstanding invoice(s)")
        return (f"I found {fmt(rec)} of recoverable cash: " + "; ".join(bits)
                + ". Open Invoices to bill and chase it.")

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

    # Billing/collections: chase the most overdue short-paid invoice.
    wants_chase = any(w in t for w in [
        "chase", "remind", "reminder", "collect", "short pay", "short-pay", "owed", "recover", "billing",
    ])
    shortpaid = ((ctx.get("billing_audit") or {}).get("top_shortpaid") or [])
    if wants_chase and shortpaid:
        inv = shortpaid[0]
        return {
            "type": "send_reminder",
            "method": "POST",
            "endpoint": f"api/v1/invoices/{inv['invoice_id']}/send_reminder/",
            "body": {},
            "label": f"Send payment reminder for {inv['invoice_number']}",
            "detail": f"R{inv['balance']:,.0f} owed · {inv['customer']}"
                      + (f" · {inv['days_overdue']} days overdue" if inv.get('days_overdue') else ""),
            "confirm_text": "Send reminder",
            "success_text": f"Payment reminder sent for {inv['invoice_number']} (R{inv['balance']:,.0f}).",
        }
    return None


def _retrieved_block(company, query: str) -> str:
    """Per-account RAG: pull the invoices most relevant to the question and render
    them as a grounding block. Empty string if RAG is unavailable (graceful fallback)."""
    if not query:
        return ""
    try:
        from core.services import rag
        hits = rag.retrieve(company, query, k=8)
    except Exception as exc:  # pragma: no cover - never break the chat
        logger.warning("RAG retrieve failed, continuing without it: %s", exc)
        return ""
    if not hits:
        return ""
    docs = "\n".join(f"- {h['content']}" for h in hits)
    return (
        "\n\nRetrieved invoice records (account-scoped, most relevant to the question — "
        "prefer these exact figures when answering):\n" + docs
    )


def _llm_generate(system: str, convo: list) -> str:
    """Generate a reply from the selected provider. Caller handles exceptions.

    Both providers receive the same instructions + grounding: Anthropic takes the
    system prompt as a top-level param; OpenAI takes it as the first message. That
    message-shape difference is the only divergence.
    """
    provider = _provider()
    if provider == "anthropic":
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=AGENT_MODEL,
            max_tokens=700,
            system=system,
            messages=convo,
        )
        return next((b.text for b in response.content if b.type == "text"), "").strip()
    if provider == "openai":
        client = OpenAI(api_key=_openai_key())
        response = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            max_tokens=700,
            temperature=0,  # grounding: quote the provided figures exactly, don't "helpfully" derive
            messages=[{"role": "system", "content": system}, *convo],
        )
        return (response.choices[0].message.content or "").strip()
    return ""


# Appended to the system prompt when the agent is allowed to create quotes.
QUOTE_TOOL_SYSTEM = (
    "\n\nYOU CAN CREATE FREIGHT QUOTES. When the user wants a quote, collect: customer name, pickup "
    "location, delivery location, cargo description, weight, and (optionally) vehicle type. You must NEVER "
    "decide or invent the price — always ASK the user what price to quote. Only once you have the customer, "
    "pickup, delivery, cargo, weight, AND a price the user has explicitly given, call the create_quote tool. "
    "A new customer is created automatically. After the tool returns, tell the user the new quote number and "
    "total in one short sentence. Do not call the tool before you have a user-provided price."
)


def _openai_tool_loop(system: str, convo: list, company, user):
    """OpenAI function-calling loop exposing the create_quote tool.

    Returns (reply_text, created_quote_or_None). The caller wraps this in try/except
    so any failure degrades to the rules reply.
    """
    from core.services import quote_agent
    client = OpenAI(api_key=_openai_key())
    messages = [{"role": "system", "content": system}, *convo]
    created_quote = None
    for _ in range(4):  # cap the tool loop
        response = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            max_tokens=700,
            temperature=0,
            tools=[quote_agent.CREATE_QUOTE_TOOL],
            messages=messages,
        )
        msg = response.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return (msg.content or "").strip(), created_quote
        # Echo the assistant's tool-call request, then answer each call.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            if tc.function.name == "create_quote":
                if created_quote:
                    # Idempotent within a turn: never create a second quote — return the
                    # one already made so the model can confirm without duplicating.
                    result = created_quote
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        result = quote_agent.create_quote(company, user, **args)
                        created_quote = result
                    except Exception as exc:  # surface a tool error back to the model
                        result = {"error": str(exc)}
            else:
                result = {"error": f"unknown tool {tc.function.name}"}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)})
    if created_quote:
        return (f"Created quote {created_quote['quote_number']} "
                f"(R{created_quote['total_amount']:,.2f}) for {created_quote['customer_name']}."), created_quote
    return "Tell me the remaining details and I'll create the quote.", created_quote


def agent_respond(company, messages: list, *, query: str = None, user=None, enable_tools: bool = False) -> dict:
    """Return {reply, source, ai_available, actions, proposed_action, ...}. Never raises.

    messages: [{"role": "user"|"assistant", "content": str}, ...]
    query: the current user question to retrieve account records for (RAG). Falls back
        to the last user message when omitted.
    user: the acting user (required for write actions like creating a quote).
    enable_tools: when True (and the OpenAI provider is active), the agent can CREATE
        quotes via function-calling. Read-only callers leave this False.
    """
    ctx = build_agent_context(company)
    last_user = next((m.get("content", "") for m in reversed(messages or []) if m.get("role") == "user"), "")
    rag_query = query if query is not None else last_user
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

    tools_on = bool(enable_tools and _provider() == "openai")

    try:
        readonly_clause = (
            "You can CREATE freight quotes for the user via the quote tool below; apart from that you are "
            "read-only and must never claim to have changed other data."
            if tools_on else
            "You are read-only: suggest what the user should do, but never claim to have changed anything."
        )
        system = (
            "You are the TruckWys copilot — an AI agent for a South African road-freight operator. "
            "TruckWys is a fleet finance/data/AI platform (quotes, bookings, invoicing, and a Capital "
            "fast-pay/factoring product). Answer concisely and practically, grounded ONLY in the JSON "
            "company snapshot and the retrieved invoice records provided — never invent figures. Use "
            "ZAR (R). When a number isn't in the snapshot or retrieved records, say you don't have it "
            "rather than guessing. Quote amounts and dates EXACTLY as they appear — do not add VAT, "
            "interest, or any derived calculation unless the user explicitly asks. A PAID invoice is "
            "settled (balance 0) and is never overdue. " + readonly_clause + " Keep replies under ~120 words."
            + (QUOTE_TOOL_SYSTEM if tools_on else "")
            + f"\n\nCompany snapshot:\n{json.dumps(ctx, default=str)}"
            + f"{_retrieved_block(company, rag_query)}"
        )
        convo = [
            {"role": m["role"], "content": str(m.get("content", ""))}
            for m in (messages or [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ][-12:]
        if not convo:
            convo = [{"role": "user", "content": "Give me a quick status of my business."}]

        created_quote = None
        if tools_on:
            reply, created_quote = _openai_tool_loop(system, convo, company, user)
        else:
            reply = _llm_generate(system, convo)

        result = {
            "reply": reply,
            "source": "llm",
            "ai_available": True,
            "provider": _provider(),
            "actions": list(actions),
            "proposed_action": proposed_action,
        }
        if created_quote and created_quote.get("quote_id"):
            result["created_quote"] = created_quote
            result["actions"] = [{
                "label": f"Open quote {created_quote['quote_number']}",
                "route": f"/quotes/{created_quote['quote_id']}",
            }] + list(actions)
        return result
    except Exception as exc:
        logger.warning("agent LLM failed, using fallback: %s", exc)
        return {
            "reply": _fallback_reply(ctx, last_user),
            "source": "rules",
            "ai_available": False,
            "actions": actions,
            "proposed_action": proposed_action,
        }
