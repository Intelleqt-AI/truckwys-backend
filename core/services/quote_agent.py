"""Quote creation for the conversational AI agent.

The Copilot calls create_quote() (via OpenAI function-calling) to turn a chat
into a real DRAFT Quote. Design decisions (per product):
  - Auto-create a DRAFT quote as soon as the AI has the details.
  - Auto-create the customer (minimal record) if it doesn't exist yet.
  - The price ALWAYS comes from the user — the agent never invents one; price_zar
    is a required argument and is stored verbatim as the quote total.

Everything is company-scoped: the quote and any auto-created customer belong to
the acting user's company.
"""
import logging
import random
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.utils import timezone

logger = logging.getLogger(__name__)

# Quote money/weight columns are DecimalField(max_digits=10, decimal_places=2):
# max 8 integer digits + 2 decimals = 99,999,999.99.
MAX_DECIMAL = Decimal("99999999.99")


# OpenAI function-calling schema for the create_quote tool.
CREATE_QUOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "create_quote",
        "description": (
            "Create a freight quote (status DRAFT) for the user's company. Call this ONLY once you have "
            "the customer name, pickup location, delivery location, cargo description, weight, AND a price "
            "that the USER has explicitly stated. Never invent, compute, or guess a price — if you don't "
            "have a user-given price, ask for it instead of calling this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Customer/company name. Auto-created if new."},
                "pickup_location": {"type": "string", "description": "Origin city/address."},
                "delivery_location": {"type": "string", "description": "Destination city/address."},
                "cargo_description": {"type": "string", "description": "Goods being moved, e.g. 'steel coils'."},
                "weight_kg": {"type": "number", "description": "Cargo weight in kilograms (1 ton = 1000 kg)."},
                "price_zar": {"type": "number", "description": "Total quote price in ZAR exactly as the user stated. Do not compute."},
                "vehicle_type": {"type": "string", "description": "e.g. Flatbed, Refrigerated, Tautliner, Tanker."},
                "distance_km": {"type": "number", "description": "Route distance in km, if known."},
                "notes": {"type": "string", "description": "Any extra notes from the conversation."},
            },
            "required": [
                "customer_name", "pickup_location", "delivery_location",
                "cargo_description", "weight_kg", "price_zar",
            ],
        },
    },
}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "customer").lower()).strip("-")
    return s or "customer"


def _to_decimal(v, default=None):
    """Parse to a 2-decimal-place Decimal (deterministic across SQLite/Postgres)."""
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _gen_quote_number() -> str:
    """QT-YYYYMMDD-XXXX, unique (mirrors QuoteViewSet.perform_create)."""
    from core.models import Quote
    ts = timezone.now().strftime("%Y%m%d")
    for _ in range(25):
        num = f"QT-{ts}-{random.randint(1000, 9999)}"
        if not Quote.objects.filter(quote_number=num).exists():
            return num
    return f"QT-{ts}-{uuid.uuid4().hex[:6]}"


def _resolve_customer(company, name: str):
    """Find an existing customer by name within the company, else auto-create a minimal one.
    Returns (customer, was_created)."""
    from core.models import Customer
    name = (name or "").strip()
    if name:
        # Prefer an exact (case-insensitive) match so we never silently bind a quote
        # to a different, longer-named customer; fall back to a substring match.
        existing = (
            Customer.objects.filter(company=company, name__iexact=name).order_by("id").first()
            or Customer.objects.filter(company=company, name__icontains=name).order_by("id").first()
        )
        if existing:
            return existing, False
    email = f"{_slug(name)}.{uuid.uuid4().hex[:8]}@quote.local"
    customer = Customer.objects.create(
        company=company, name=name or "New Customer", email=email,
        phone="", address="", city="", state="", zip_code="",
    )
    return customer, True


def create_quote(company, user, *, customer_name, pickup_location, delivery_location,
                 cargo_description, weight_kg, price_zar, vehicle_type=None,
                 distance_km=None, notes=None, **_ignored) -> dict:
    """Create a DRAFT Quote from chat-supplied details. Company-scoped.

    Raises ValueError for an invalid/missing price (the only hard requirement the
    agent can get wrong, since price must come from the user)."""
    from core.models import Quote

    if not company:
        raise ValueError("No company on this account.")

    price = _to_decimal(price_zar)
    if price is None or price <= 0:
        raise ValueError("A valid price (from the user) is required to create the quote.")
    if price > MAX_DECIMAL:
        raise ValueError("Price exceeds the maximum the system can store (R99,999,999.99).")

    weight = _to_decimal(weight_kg, Decimal("0")) or Decimal("0")
    if weight < 0:
        weight = Decimal("0")
    if weight > MAX_DECIMAL:
        raise ValueError("Weight is larger than the system can store.")

    customer, customer_created = _resolve_customer(company, customer_name)

    fields = dict(
        company=company,
        customer=customer,
        created_by=user,
        quote_number=_gen_quote_number(),
        pickup_location=(pickup_location or "").strip()[:500],
        delivery_location=(delivery_location or "").strip()[:500],
        cargo_description=(cargo_description or "").strip(),
        weight=weight,
        vehicle_type=(vehicle_type or "").strip()[:50],
        base_rate=price,
        total_amount=price,
        valid_until=date.today() + timedelta(days=30),
        status="DRAFT",
        notes=(notes or "").strip()[:2000],
    )
    dist = _to_decimal(distance_km)
    if dist is not None and 0 < dist <= MAX_DECIMAL:
        fields["distance"] = dist

    quote = Quote.objects.create(**fields)
    logger.info("AI created quote %s for company %s (customer_created=%s)",
                quote.quote_number, getattr(company, "id", "?"), customer_created)
    return {
        "quote_id": quote.id,
        "quote_number": quote.quote_number,
        "total_amount": float(quote.total_amount),
        "customer_name": customer.name,
        "customer_created": customer_created,
    }
