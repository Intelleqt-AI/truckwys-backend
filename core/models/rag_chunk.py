from django.db import models


class InvoiceEmbedding(models.Model):
    """A per-account RAG chunk: one invoice rendered to text + its embedding vector.

    Scoped to a company so retrieval is strictly account-isolated. The vector is
    stored as a JSON-encoded list[float] (works on SQLite/Postgres alike); cosine
    similarity is computed in-process with numpy. `source_hash` lets the indexer
    skip invoices whose underlying data hasn't changed.
    """
    company = models.ForeignKey(
        "Company", on_delete=models.CASCADE, related_name="invoice_embeddings"
    )
    invoice = models.OneToOneField(
        "Invoice", on_delete=models.CASCADE, related_name="rag_embedding"
    )
    content = models.TextField(help_text="Natural-language document that was embedded")
    embedding = models.TextField(help_text="JSON-encoded list[float] embedding vector")
    source_hash = models.CharField(max_length=64, db_index=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rag_invoice_embeddings"
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return f"Embedding(invoice={self.invoice_id}, company={self.company_id})"
