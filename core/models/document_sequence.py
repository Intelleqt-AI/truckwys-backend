"""Per-company document number sequences (invoices, credit notes)."""
from django.db import models


class DocumentSequence(models.Model):
    """The next number to issue for one document type of one company.

    Allocation (core.services.numbering) locks this row with
    select_for_update inside the transaction that issues the document, so
    two concurrent issues can't take the same number, and a rolled-back
    issue rolls the counter back too: issued numbers are gap-free.
    """
    INVOICE = 'INVOICE'
    CREDIT_NOTE = 'CREDIT_NOTE'
    DOC_TYPE_CHOICES = [(INVOICE, 'Invoice'), (CREDIT_NOTE, 'Credit note')]
    DEFAULT_PREFIX = {INVOICE: 'INV-', CREDIT_NOTE: 'CN-'}

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='document_sequences')
    doc_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES)
    prefix = models.CharField(max_length=20, default='INV-')
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=5)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'document_sequences'
        constraints = [
            models.UniqueConstraint(fields=['company', 'doc_type'], name='uniq_document_sequence'),
        ]

    def format(self, number: int) -> str:
        return f'{self.prefix}{number:0{self.padding}d}'

    def __str__(self):
        return f'{self.company_id} {self.doc_type} next={self.format(self.next_number)}'
