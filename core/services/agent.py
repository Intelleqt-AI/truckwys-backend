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
# Hard wall-clock cap per LLM call so a slow/hung provider can't pin a worker
# indefinitely (the OpenAI SDK default is 600s). One retry keeps transient blips
# from failing the turn while still bounding total wait.
LLM_TIMEOUT_SECONDS = float(os.environ.get("COPILOT_LLM_TIMEOUT", "30") or 30)
LLM_MAX_RETRIES = 1


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


_TOOLS_DISABLED_WARNED = False


def _warn_tools_disabled_once() -> None:
    """Copilot database tools (query/create/update/delete/email + guided entry)
    only run under the OpenAI provider. If a deployment sets ANTHROPIC_API_KEY,
    _provider() 'auto' silently switches to Anthropic and ALL tools go dark with
    no user-visible error. Log this loudly once per process so operators can spot
    the misconfiguration (fix: set COPILOT_LLM_PROVIDER=openai + OPENAI_API_KEY)."""
    global _TOOLS_DISABLED_WARNED
    if _TOOLS_DISABLED_WARNED:
        return
    _TOOLS_DISABLED_WARNED = True
    logger.warning(
        "Copilot tools requested but the active provider is %r, not 'openai' — "
        "database tools/proposals/guided-entry are DISABLED. Set "
        "COPILOT_LLM_PROVIDER=openai and OPENAI_API_KEY to enable them.",
        _provider() or 'rules',
    )


# Invoice statuses that are NOT collectable receivables and must be excluded from
# "outstanding"/"overdue"/top-debtor math: PAID (settled), CANCELLED (void),
# DRAFT (never issued). Mirrors customer_risk._EXCLUDED_STATUSES + PAID.
_NON_OUTSTANDING = ('PAID', 'CANCELLED', 'DRAFT')


def _money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def build_agent_context(company, user=None) -> dict:
    """Assemble a compact, real snapshot of the company for grounding the agent.

    When a user is given, snapshot sections are stripped to what their role may
    read (mirrors the copilot entity permissions) so e.g. a VIEWER never gets
    invoice balances or banking details in the prompt."""
    from core.models import Invoice, Quote, Vehicle, Customer, Driver

    today = timezone.now().date()

    invoices = Invoice.objects.filter(company=company)
    # Only real receivables count as money owed: PAID is settled, CANCELLED is
    # void, and DRAFT was never issued — including any of them overstates what the
    # company is owed and can make the copilot chase invoices that don't exist.
    outstanding_qs = invoices.exclude(status__in=_NON_OUTSTANDING)
    outstanding = outstanding_qs.aggregate(s=Sum('balance'))['s'] or Decimal('0')
    overdue_qs = outstanding_qs.filter(due_date__lt=today)
    overdue = overdue_qs.aggregate(s=Sum('balance'))['s'] or Decimal('0')
    # Cash collected = every rand actually received, including partial payments,
    # not just fully-PAID invoices' face value.
    collected = (invoices.exclude(status='CANCELLED')
                 .aggregate(s=Sum('paid_amount'))['s'] or Decimal('0'))

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

    # Biggest debtors in a SINGLE grouped query over ALL customers — the old
    # code sliced the first 50 customers alphabetically and sorted AFTER, so the
    # true top debtor was silently dropped for any company with >50 customers
    # (and it cost one query per customer).
    top_customers = [
        {"name": row['customer__name'] or "—", "outstanding": _money(row['outstanding'])}
        for row in (outstanding_qs.values('customer__name')
                    .annotate(outstanding=Sum('balance'))
                    .order_by('-outstanding')[:5])
        if row['outstanding']
    ]

    # Contact information — first 100 customers by name (a sample, NOT the full
    # book; customers_total below tells the model the real count so it never
    # states the capped list length as a total).
    customers_qs = Customer.objects.filter(company=company)
    customers_total = customers_qs.count()
    contacts = [
        {
            "name": c.name,
            "company": c.company_name or "",
            "email": c.email,
            "phone": c.phone,
            "city": c.city,
            "payment_terms": c.payment_terms_default,
            "credit_limit": _money(c.credit_limit) if c.credit_limit else None,
            "status": "active" if c.is_active else "inactive",
        }
        for c in customers_qs.order_by('name')[:100]
    ]

    # Recent quote line items — last 25 quotes with full route and pricing detail
    recent_quotes = [
        {
            "quote_number": q.quote_number,
            "customer": q.customer.name if q.customer else "—",
            "pickup": q.pickup_location,
            "delivery": q.delivery_location,
            "cargo": q.cargo_description,
            "weight_kg": float(q.weight) if q.weight else None,
            "vehicle_type": q.vehicle_type or "—",
            "distance_km": float(q.distance) if q.distance else None,
            "base_rate": _money(q.base_rate),
            "fuel_surcharge": _money(q.fuel_surcharge),
            "toll_charges": _money(q.toll_charges),
            "total": _money(q.total_amount),
            "margin_pct": float(q.margin_percentage) if q.margin_percentage else 0,
            "status": q.status,
            "valid_until": q.valid_until.isoformat() if q.valid_until else None,
            "trip_type": q.trip_type,
            "created": q.created_at.date().isoformat() if q.created_at else None,
        }
        for q in quotes.select_related('customer').order_by('-created_at')[:25]
    ]

    # Driver details — all drivers for this company
    drivers = []
    try:
        for d in Driver.objects.filter(company=company).select_related('user').order_by('user__first_name'):
            drivers.append({
                "name": d.user.get_full_name() if d.user else "—",
                "email": d.user.email if d.user else "—",
                "license_number": d.license_number,
                "license_expiry": d.license_expiry.isoformat() if d.license_expiry else None,
                "status": d.status,
                "experience_years": d.experience_years,
                "efficiency_score": d.efficiency_score,
                "on_time_rate": float(d.on_time_rate),
                "safety_score": d.safety_score,
                "trips_this_month": d.trips_this_month,
                "violations": d.violation_count,
                "accidents": d.accident_history,
            })
    except Exception as exc:
        logger.warning('driver data for agent failed: %s', exc)

    # Company banking details (stored in contact JSONField under "banking" key)
    contact_json = getattr(company, 'contact', {}) or {}
    banking = contact_json.get('banking', {})

    ctx = {
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
            "recent": recent_quotes,
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
        "customers_total": customers_total,
        "contacts": contacts,
        "drivers": drivers,
        "banking": banking,
    }

    if user is not None and not getattr(user, 'is_superuser', False):
        from core.services.copilot_entities import role_can
        if not role_can(user, 'invoices', 'read'):
            for key in ('invoices', 'capital', 'billing_audit',
                        'top_customers_by_outstanding', 'banking'):
                ctx.pop(key, None)
        if not role_can(user, 'drivers', 'read'):
            ctx.pop('drivers', None)
        if not role_can(user, 'customers', 'read'):
            ctx.pop('contacts', None)
            ctx.pop('customers_total', None)
        if not role_can(user, 'quotes', 'read'):
            ctx.pop('quotes', None)
        if not role_can(user, 'vehicles', 'read'):
            ctx.pop('fleet', None)
    return ctx


def _capital_summary(company):
    """Eligible-invoice summary + best advanceable invoice, cached per company.

    This is the most expensive part of the snapshot — it runs the RiskEngine per
    candidate invoice (up to 25). Underwriting inputs (RiskScore) are ~24h-valid,
    so caching the result for a few minutes bounds the cost to once per window
    instead of once per chat message, without changing any figure the user sees."""
    from django.core.cache import cache
    ck = f'copilot_capital_{getattr(company, "id", "0")}'
    cached = cache.get(ck)
    if cached is not None:
        return cached['count'], cached['value'], cached['top']
    count, value, top = _capital_summary_uncached(company)
    try:
        cache.set(ck, {'count': count, 'value': value, 'top': top},
                  int(os.environ.get('COPILOT_CAPITAL_TTL', '300') or 300))
    except Exception:
        pass
    return count, value, top


def _capital_summary_uncached(company):
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
    # Sections may be absent when the snapshot was role-stripped (VIEWER/DRIVER).
    # Use .get so a restricted role gets a helpful answer, never a KeyError → 500.
    inv = ctx.get("invoices") or {}
    cap = ctx.get("capital") or {}
    fleet = ctx.get("fleet") or {}
    quotes = ctx.get("quotes") or {}
    no_access = "You don't have access to that information — ask an admin if you need it."

    def fmt(n):
        return f"R{n or 0:,.0f}"

    if not t or any(w in t for w in ["help", "what can you", "hello", "hi ", "hey"]):
        return (
            f"I'm your TruckWys copilot for {ctx['company_name']}. I can answer questions about your "
            f"cash position, overdue invoices, quotes pipeline, fleet status, fast-pay eligibility, "
            f"customer contacts, driver details and banking info. "
            f"Try: \"What's overdue?\", \"Show me driver performance\", \"Contact details for [customer]\" or \"Fleet status\"."
        )

    if any(w in t for w in ["overdue", "late", "collect", "debtor"]):
        if not inv:
            return no_access
        if inv.get("overdue_count"):
            lines = [f"You have {inv['overdue_count']} overdue invoice(s) totalling {fmt(inv.get('overdue_total'))}."]
            for o in inv.get("top_overdue", [])[:3]:
                lines.append(f"• {o['invoice']} — {o['customer']}: {fmt(o['balance'])}, {o['days_overdue']} days overdue.")
            lines.append("Chasing the oldest first usually recovers cash fastest.")
            return " ".join(lines)
        return "Good news — nothing is overdue right now."

    if any(w in t for w in ["outstanding", "owed", "receivable", "cash position"]):
        if not inv:
            return no_access
        return (
            f"{fmt(inv.get('outstanding_total'))} is outstanding across {inv.get('count', 0)} invoices "
            f"({fmt(inv.get('overdue_total'))} overdue). {fmt(inv.get('revenue_collected'))} has been collected to date."
        )

    if any(w in t for w in ["advance", "fast pay", "capital", "factor"]):
        if not cap:
            return no_access
        if cap.get("eligible_invoices"):
            top = cap.get("top_eligible")
            msg = (
                f"{cap['eligible_invoices']} invoice(s) are eligible for fast pay right now, "
                f"worth about {fmt(cap.get('eligible_value'))} in net advances."
            )
            if top:
                msg += (
                    f" The best is {top['invoice_number']} ({top['customer']}) at "
                    f"R{top['net_payout']:,.0f} net — I can request that advance for you now."
                )
            return msg
        return "No invoices are currently eligible for fast pay (they need a delivered load with proof of delivery)."

    if any(w in t for w in ["quote", "pipeline", "pricing"]):
        if not quotes:
            return no_access
        by = quotes.get("by_status", {})
        parts = ", ".join(f"{v} {k.lower()}" for k, v in by.items()) or "none yet"
        return f"Your pipeline has {quotes.get('total', 0)} quotes ({parts}). I can open the board or start a new quote."

    if any(w in t for w in ["bill", "unbilled", "short pay", "short-pay", "underbilled", "recover", "uninvoiced", "audit"]):
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

    if any(w in t for w in ["fleet", "vehicle", "truck"]):
        if not fleet:
            return no_access
        by = fleet.get("by_status", {})
        parts = ", ".join(f"{v} {k.replace('_',' ').lower()}" for k, v in by.items()) or "none"
        return f"Your fleet has {fleet.get('total', 0)} vehicles ({parts})."

    if any(w in t for w in ["driver", "drivers"]):
        if "drivers" not in ctx:
            return no_access
        drivers = ctx.get("drivers") or []
        if not drivers:
            return "No drivers are recorded for your company yet."
        lines = [f"You have {len(drivers)} driver(s):"]
        for d in drivers[:5]:
            # on_time_rate is already a percentage value (e.g. 95.0) — format as
            # a plain number with a % sign; ':.0%' would multiply by 100 → "9500%".
            lines.append(
                f"• {d['name']} — {d['status']}, license {d['license_number']}, "
                f"on-time {d['on_time_rate']:.0f}%, safety score {d['safety_score']}"
            )
        return " ".join(lines)

    if any(w in t for w in ["contact", "customer email", "customer phone", "phone number", "email address"]):
        contacts = ctx.get("contacts") or []
        if not contacts:
            return "No contacts found."
        lines = [f"You have {len(contacts)} contact(s) on record:"]
        for c in contacts[:5]:
            lines.append(f"• {c['name']} ({c['company'] or '—'}) — {c['phone']}, {c['email']}, {c['city']}")
        return " ".join(lines)

    if any(w in t for w in ["bank", "banking", "account number", "branch"]):
        banking = ctx.get("banking") or {}
        if not banking:
            return "No banking details are saved on your company profile yet. Update them in Settings."
        parts = [f"{k.replace('_', ' ').title()}: {v}" for k, v in banking.items()]
        return "Banking details: " + " | ".join(parts)

    # Default: give the cash headline and point to insights (only for roles that
    # can see finances; others get a capability prompt instead of a KeyError).
    if not inv:
        return (
            f"I'm your TruckWys copilot for {ctx['company_name']}. Ask me about your fleet status, "
            "quotes, drivers or contacts — whatever your role has access to."
        )
    return (
        f"Here's the headline for {ctx['company_name']}: {fmt(inv.get('outstanding_total'))} outstanding, "
        f"{fmt(inv.get('overdue_total'))} overdue, {cap.get('eligible_invoices', 0)} invoice(s) ready for fast pay. "
        f"Ask me about overdue accounts, fast-pay capacity, your quotes pipeline, contacts, drivers or fleet status."
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
        "\n\nRetrieved invoice records (account-scoped, most relevant to the question). "
        "These are INDEXED records that may lag the live data — use them to locate the "
        "right invoices and their descriptive detail, but whenever a figure here (balance, "
        "paid amount, status, days overdue) disagrees with the company snapshot above, "
        "TRUST THE SNAPSHOT: it is computed live this request.\n" + docs
    )


def _llm_generate(system: str, convo: list) -> str:
    """Generate a reply from the selected provider. Caller handles exceptions.

    Both providers receive the same instructions + grounding: Anthropic takes the
    system prompt as a top-level param; OpenAI takes it as the first message. That
    message-shape difference is the only divergence.
    """
    provider = _provider()
    if provider == "anthropic":
        client = anthropic.Anthropic(timeout=LLM_TIMEOUT_SECONDS, max_retries=LLM_MAX_RETRIES)
        response = client.messages.create(
            model=AGENT_MODEL,
            max_tokens=700,
            system=system,
            messages=convo,
        )
        return next((b.text for b in response.content if b.type == "text"), "").strip()
    if provider == "openai":
        client = OpenAI(api_key=_openai_key(), timeout=LLM_TIMEOUT_SECONDS, max_retries=LLM_MAX_RETRIES)
        response = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            max_tokens=700,
            temperature=0,  # grounding: quote the provided figures exactly, don't "helpfully" derive
            messages=[{"role": "system", "content": system}, *convo],
        )
        return (response.choices[0].message.content or "").strip()
    return ""


_TITLE_SYSTEM = (
    "Summarize the user's message as a short chat title: 3-6 words, Title Case, no quotes, "
    "no trailing punctuation, no generic filler like 'Chat' or 'Conversation'. Reply with ONLY the title."
)


def generate_conversation_title(text: str) -> str:
    """A short, human-readable title for a new conversation, from its first message.
    Never raises — falls back to a truncated/cleaned version of the text itself."""
    fallback = ' '.join((text or '').strip().split())[:60] or 'New conversation'
    if not _llm_enabled():
        return fallback
    try:
        title = _llm_generate(_TITLE_SYSTEM, [{"role": "user", "content": text[:500]}])
        title = title.strip().strip('"\'').split('\n')[0].rstrip('.!, ')
        return title[:60] if title else fallback
    except Exception as exc:
        logger.warning("conversation title generation failed, using fallback: %s", exc)
        return fallback


def _capability_block(user) -> str:
    """Role-aware description of the copilot's database tools for the system prompt."""
    from core.services import copilot_entities as entities

    read_tables = entities.allowed_tables(user, 'read')
    create_tables = entities.allowed_tables(user, 'create')
    update_tables = entities.allowed_tables(user, 'update')
    delete_tables = entities.allowed_tables(user, 'delete')
    role = (getattr(user, 'role', '') or 'user').upper()

    parts = [
        "\n\nDATA TOOLS: Use query_records to answer from the database precisely — lists, "
        f"lookups, counts and sums the snapshot lacks. Readable tables: {', '.join(read_tables)}. "
        "For ANY total, sum, count or average, ALWAYS use the `aggregate` parameter — NEVER "
        "manually add up the `rows` a query_records call returns, because rows can be "
        "truncated (a `truncated`/`note` field says so) and a manual sum over a partial page "
        "silently gives a wrong, too-low answer. "
        "PICK THE EXACT MONEY FIELD the question means — check the parenthetical notes in "
        "the schema reference below; e.g. an invoice's `subtotal` is pre-VAT and is almost "
        "never what \"total\"/\"balance\"/\"total_amount\" means. When a question names a "
        "field literally (e.g. asks for \"total_amount\"), aggregate that exact field, not a "
        "different one. For status/category filters, use the EXACT database value (e.g. "
        "PARTIALLY_PAID) — query_records will error with the valid values if you guess wrong, "
        "so read the error rather than inventing an unrelated filter. "
        "Never invent ids or figures; if a query errors, read the error and adjust.",
        "\nSCHEMA REFERENCE (fields you may set; * = required to create; [rcud] = your "
        "read/create/update/delete rights):\n" + entities.build_schema_reference(user),
    ]
    if create_tables or update_tables or delete_tables:
        parts.append(
            "\nWRITE RULES: Prepare writes with propose_create"
            + (f" ({', '.join(create_tables)})" if create_tables else "")
            + ", propose_update"
            + (f" ({', '.join(update_tables)})" if update_tables else "")
            + " and propose_delete"
            + (f" ({', '.join(delete_tables)})" if delete_tables else "")
            + ". A proposal is NOT saved until the user confirms the card shown in the UI. "
            "After a propose tool succeeds, tell the user to review and confirm the card — NEVER say "
            "a record was saved, changed or deleted. One proposal per message. "
            "\nGUIDED ENTRY: To create a record, collect the required fields by asking ONE question at a "
            "time in the schema order (accept several answers at once when the user volunteers them). "
            "Prices, rates and amounts must always come from the user — never compute or guess them. "
            "Resolve references (customer/driver/vehicle/invoice) by name with query_records first; when "
            "several match, list the candidates and ask which one. "
            f"\nIf asked to change a table your tools don't cover, say plainly that the {role} role "
            "doesn't permit it and suggest asking an admin."
        )
    else:
        parts.append(
            f"\nYou are read-only for this user (role {role}): suggest what they should do in the app, "
            "but never claim to have changed anything."
        )

    if entities.can_send_email(user):
        parts.append(
            "\nEMAIL: You can send an email to a known Customer or Driver contact via "
            "propose_send_email. NEVER invent or accept an email address from the user — always "
            "resolve the recipient by name with query_records('customers'|'drivers', ...) first, "
            "then pass recipient_type + recipient_id. Before drafting a reminder, follow-up, or "
            "analysis email (e.g. about overdue invoices or payment history), first research the "
            "relevant context with query_records (invoices/payments/loads) and summarize what you "
            "found in analysis_summary so the user sees why you wrote what you wrote. Keep the email "
            "professional, concise (under ~200 words), and specific to the real data — never invent "
            "figures. The `body` must be ONLY the message content — do NOT include a greeting line "
            "(e.g. 'Dear X,' / 'Hi X,') or a sign-off/closing (e.g. 'Regards,' / 'Best regards,' / "
            "'Sincerely,'), since the email template already adds a greeting at the top and a signature "
            "at the bottom automatically — including your own would duplicate them. "
            "propose_send_email only drafts the email for confirmation — after calling it, "
            "tell the user to review and confirm the card; NEVER claim an email was sent."
        )
    return ''.join(parts)


def _openai_tool_loop(system: str, convo: list, company, user, conversation=None):
    """OpenAI function-calling loop over the copilot database tools.

    Returns (reply_text, proposal_or_None). The caller wraps this in try/except
    so any failure degrades to the rules reply.
    """
    from core.services.copilot_entities import build_tool_schemas
    from core.services.copilot_tools import TOOL_HANDLERS

    tools = build_tool_schemas(user)
    if not tools:
        return _llm_generate(system, convo), None

    client = OpenAI(api_key=_openai_key(), timeout=LLM_TIMEOUT_SECONDS, max_retries=LLM_MAX_RETRIES)
    messages = [{"role": "system", "content": system}, *convo]
    proposal_id = None
    for _ in range(6):  # cap the tool loop (query rounds + one propose)
        response = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            max_tokens=700,
            temperature=0,
            tools=tools,
            messages=messages,
        )
        msg = response.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return (msg.content or "").strip(), _load_proposal(proposal_id)
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
            handler = TOOL_HANDLERS.get(tc.function.name)
            if handler is None:
                result = {"error": f"unknown tool {tc.function.name}"}
            else:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except ValueError:
                    args = {}
                try:
                    result = handler(company, user, conversation, args)
                except Exception as exc:  # surface a tool error back to the model
                    logger.exception("copilot tool %s failed", tc.function.name)
                    result = {"error": str(exc)}
                if isinstance(result, dict) and result.get('proposal_id') and proposal_id is None:
                    proposal_id = result['proposal_id']
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)})
    if proposal_id:
        return ("I've prepared it — please review and confirm the card above.",
                _load_proposal(proposal_id))
    return "Tell me the remaining details and I'll prepare it for your confirmation.", None


def _load_proposal(proposal_id):
    if not proposal_id:
        return None
    from core.models import CopilotProposal
    return CopilotProposal.objects.filter(id=proposal_id).first()


def agent_respond(company, messages: list, *, query: str = None, user=None,
                  enable_tools: bool = False, conversation=None) -> dict:
    """Return {reply, source, ai_available, actions, proposed_action, proposal?, ...}. Never raises.

    messages: [{"role": "user"|"assistant", "content": str}, ...]
    query: the current user question to retrieve account records for (RAG). Falls back
        to the last user message when omitted.
    user: the acting user (required for database tools — permissions come from their role).
    enable_tools: when True (and the OpenAI provider is active), the agent can query the
        database and prepare confirm-first create/update/delete proposals.
    conversation: the CopilotConversation the turn belongs to (proposals attach to it).
    """
    ctx = build_agent_context(company, user=user)
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

    tools_on = bool(enable_tools and user is not None and _provider() == "openai")

    try:
        if tools_on:
            capability_clause = _capability_block(user)
        elif enable_tools and user is not None:
            _warn_tools_disabled_once()
            capability_clause = (
                " Database write tools are unavailable right now (the OpenAI provider is not "
                "configured) — you are read-only: suggest what the user should do, but never "
                "claim to have changed anything."
            )
        else:
            capability_clause = (
                " You are read-only: suggest what the user should do, but never claim to have "
                "changed anything."
            )

        # RAG retrieval surfaces invoice records — only for roles that may read
        # them. Gate purely on whether the (already role-stripped) snapshot kept
        # the invoices section; `user is None` must NOT imply full access.
        rag_block = ''
        if ctx.get('invoices') is not None:
            rag_block = _retrieved_block(company, rag_query)

        user_name = (getattr(user, 'first_name', '') or '').strip() if user is not None else ''
        name_clause = (
            f"\n\nThe user you are talking to is named {user_name}. Address them naturally by "
            "first name occasionally — e.g. in a greeting or when wrapping up — not in every reply."
            if user_name else ""
        )

        # Data minimisation: bank-account details are the most sensitive field in
        # the snapshot. Only send them to the LLM when the question is actually
        # about banking — never on every unrelated turn. (The rules fallback still
        # answers banking questions locally from the full ctx.)
        prompt_ctx = ctx
        if ctx.get('banking') and not any(
            w in (last_user or '').lower()
            for w in ('bank', 'banking', 'account number', 'branch', 'iban', 'swift', 'payment detail')
        ):
            prompt_ctx = {k: v for k, v in ctx.items() if k != 'banking'}

        system = (
            "You are the TruckWys copilot — an AI agent for a South African road-freight operator. "
            "TruckWys is a fleet finance/data/AI platform (quotes, bookings, invoicing, and a Capital "
            "fast-pay/factoring product). Answer concisely and practically, grounded ONLY in the JSON "
            "company snapshot, the retrieved records, and your database tool results — never invent "
            "figures. Use ZAR (R). When a number isn't available, say you don't have it rather than "
            "guessing. Quote amounts and dates EXACTLY as they appear — do not add VAT, interest, or "
            "any derived calculation unless the user explicitly asks. A PAID invoice is settled "
            "(balance 0) and is never overdue. Format tabular answers as markdown tables. "
            "Keep replies under ~120 words. "
            "SNAPSHOT LIMITS: some snapshot lists are truncated SAMPLES, not the full set — "
            "`contacts` is the first 100 customers by name (the real count is `customers_total`), "
            "`quotes.recent` the 25 newest, `invoices.top_overdue`/`top_customers_by_outstanding` "
            "the worst 5, `capital.top_eligible` a sample. NEVER state a sample's length as a total "
            + ("or claim a record doesn't exist just because it's absent from a sample — use "
               "query_records for complete counts, totals and lookups. "
               if tools_on else
               "or claim a record doesn't exist just because it's absent from a sample; say you're "
               "showing a sample and they can view the full list in the app. ")
            + capability_clause
            + name_clause
            + f"\n\nCompany snapshot:\n{json.dumps(prompt_ctx, default=str)}"
            + rag_block
        )
        convo = [
            {"role": m["role"], "content": str(m.get("content", ""))}
            for m in (messages or [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ][-30:]
        if not convo:
            convo = [{"role": "user", "content": "Give me a quick status of my business."}]

        proposal = None
        if tools_on:
            reply, proposal = _openai_tool_loop(system, convo, company, user, conversation)
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
        if proposal is not None:
            from core.services.copilot_tools import proposal_public
            result["proposal"] = proposal_public(proposal)
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
