"""Per-account semantic RAG for the Copilot.

Indexes each company's invoices (+ payment state) as text documents, embeds them
with OpenAI, and retrieves the top-K most relevant rows for a user's question.
Everything is scoped to a company FK — retrieval never crosses accounts.

Degrades gracefully: if no OPENAI_API_KEY is configured the embed calls return
[] and the caller falls back to the snapshot-only prompt (no crash).
"""
import hashlib
import json
import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _NUMPY = True
except ImportError:  # pragma: no cover
    _NUMPY = False

try:
    from openai import OpenAI
    _OPENAI = True
except ImportError:  # pragma: no cover
    _OPENAI = False


def _embedding_model() -> str:
    return getattr(settings, "EMBEDDING_MODEL", "text-embedding-3-small")


def _api_key() -> str:
    import os
    return os.environ.get("OPENAI_API_KEY") or getattr(settings, "OPENAI_API_KEY", "")


def rag_enabled() -> bool:
    return _OPENAI and _NUMPY and bool(_api_key())


def _money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def invoice_document(invoice) -> str:
    """Render one invoice + its payments to a compact, retrievable text document."""
    today = timezone.now().date()
    customer = invoice.customer.name if invoice.customer else "Unknown customer"
    due = invoice.due_date
    days_overdue = (today - due).days if (due and due < today and invoice.status != "PAID") else 0

    lines = [
        f"Invoice {invoice.invoice_number} for customer {customer}.",
        f"Status: {invoice.get_status_display()}. Payment terms: {invoice.get_payment_terms_display()}.",
        f"Total R{_money(invoice.total_amount):,.2f}, paid R{_money(invoice.paid_amount):,.2f}, "
        f"balance outstanding R{_money(invoice.balance):,.2f}.",
        f"Issued {invoice.issue_date.isoformat() if invoice.issue_date else '—'}, "
        f"due {due.isoformat() if due else '—'}.",
    ]
    if days_overdue > 0:
        lines.append(f"OVERDUE by {days_overdue} days.")
    if invoice.early_pay_eligible:
        lines.append("Eligible for fast-pay / early-payment advance.")

    payments = list(invoice.payments.all().order_by("-payment_date")[:10])
    if payments:
        pay_strs = [
            f"R{_money(p.amount):,.2f} via {p.get_payment_method_display()} on "
            f"{p.payment_date.isoformat() if p.payment_date else '—'}"
            for p in payments
        ]
        lines.append("Payments received: " + "; ".join(pay_strs) + ".")
    else:
        lines.append("No payments received yet.")

    if invoice.notes:
        lines.append(f"Notes: {invoice.notes[:300]}")

    return " ".join(lines)


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embed(texts):
    """Batch-embed a list of strings. Returns list[list[float]] (or [] if unavailable)."""
    if not texts or not rag_enabled():
        if texts and not rag_enabled():
            logger.warning("RAG embed skipped: OPENAI_API_KEY / openai / numpy not available")
        return []
    try:
        client = OpenAI(api_key=_api_key())
        resp = client.embeddings.create(model=_embedding_model(), input=list(texts))
        return [item.embedding for item in resp.data]
    except Exception as exc:  # pragma: no cover - network/credential failures
        logger.warning("RAG embedding call failed: %s", exc)
        return []


def index_company_invoices(company) -> int:
    """Upsert embeddings for all of a company's invoices. Skips unchanged rows
    (matched by source_hash). Returns the number of (re)embedded invoices."""
    if not rag_enabled():
        return 0
    from core.models import Invoice, InvoiceEmbedding

    invoices = list(Invoice.objects.filter(company=company).select_related("customer"))
    if not invoices:
        return 0

    existing = {
        e.invoice_id: e
        for e in InvoiceEmbedding.objects.filter(company=company)
    }

    pending, docs = [], []
    for inv in invoices:
        doc = invoice_document(inv)
        h = _source_hash(doc)
        cur = existing.get(inv.id)
        if cur and cur.source_hash == h:
            continue  # unchanged
        pending.append((inv, doc, h))
        docs.append(doc)

    if not docs:
        return 0

    vectors = _embed(docs)
    if len(vectors) != len(docs):
        return 0  # embed failed; leave existing rows intact

    for (inv, doc, h), vec in zip(pending, vectors):
        InvoiceEmbedding.objects.update_or_create(
            invoice=inv,
            defaults={
                "company": company,
                "content": doc,
                "embedding": json.dumps(vec),
                "source_hash": h,
            },
        )
    return len(pending)


def retrieve(company, query: str, k: int = 8):
    """Return the top-K invoice documents for `query`, scoped to `company`.

    Output: [{invoice_number, content, score}], highest score first.
    Empty list when RAG is unavailable or the company has no indexed invoices.
    """
    if not query or not rag_enabled():
        return []
    from core.models import InvoiceEmbedding

    rows = list(InvoiceEmbedding.objects.filter(company=company).select_related("invoice"))
    if not rows:
        return []

    qvecs = _embed([query])
    if not qvecs:
        return []

    q = np.asarray(qvecs[0], dtype=np.float32)
    q_norm = np.linalg.norm(q) or 1.0

    scored = []
    for row in rows:
        try:
            v = np.asarray(json.loads(row.embedding), dtype=np.float32)
        except (ValueError, TypeError):
            continue
        denom = (np.linalg.norm(v) or 1.0) * q_norm
        score = float(np.dot(q, v) / denom)
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "invoice_number": row.invoice.invoice_number if row.invoice else "—",
            "content": row.content,
            "score": round(score, 4),
        }
        for score, row in scored[:k]
    ]
