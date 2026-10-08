"""Sequential, gap-free, concurrency-safe document numbers per company.

Rules (docs/foundation/SPEC.md §Numbering):
  * Drafts get a provisional number (DRAFT-XXXXXXXX). Deleting a draft
    therefore never leaves a hole in the issued sequence.
  * The real number {prefix}{n:0{padding}} is allocated when the invoice is
    issued (DRAFT -> SENT), inside the issuing transaction, under a row lock
    on the company's DocumentSequence.
  * Existing (pre-foundation) numbers are kept untouched.
  * If a formatted number is already taken in that company (a legacy number
    that happens to match, or a sequence moved backwards by hand) it is
    skipped. Settings refuse to move next_number below the highest number
    already issued with the current prefix, so this is a safety net only.
"""
import re
import secrets

from django.db import IntegrityError, transaction

PROVISIONAL_PREFIX = 'DRAFT-'


def provisional_number() -> str:
    return f'{PROVISIONAL_PREFIX}{secrets.token_hex(4).upper()}'


def is_provisional_number(number) -> bool:
    return bool(number) and str(number).startswith(PROVISIONAL_PREFIX)


def _get_locked_sequence(company, doc_type):
    from core.models import DocumentSequence
    for _ in range(3):
        seq = DocumentSequence.objects.select_for_update().filter(company=company, doc_type=doc_type).first()
        if seq is not None:
            return seq
        try:
            with transaction.atomic():
                DocumentSequence.objects.create(
                    company=company, doc_type=doc_type,
                    prefix=DocumentSequence.DEFAULT_PREFIX[doc_type],
                )
        except IntegrityError:
            pass  # created concurrently; loop and lock it
    raise RuntimeError('Could not lock document sequence')


def _allocate(company, doc_type, exists):
    with transaction.atomic():
        seq = _get_locked_sequence(company, doc_type)
        n = seq.next_number
        number = seq.format(n)
        while exists(number):
            n += 1
            number = seq.format(n)
        seq.next_number = n + 1
        seq.save(update_fields=['next_number', 'updated_at'])
        return number


def allocate_invoice_number(company) -> str:
    from core.models import DocumentSequence, Invoice
    return _allocate(company, DocumentSequence.INVOICE,
                     lambda num: Invoice.objects.filter(company=company, invoice_number=num).exists())


def allocate_credit_note_number(company) -> str:
    from core.models import DocumentSequence, CreditNote
    return _allocate(company, DocumentSequence.CREDIT_NOTE,
                     lambda num: CreditNote.objects.filter(company=company, credit_note_number=num).exists())


def get_sequence(company, doc_type):
    from core.models import DocumentSequence
    seq, _ = DocumentSequence.objects.get_or_create(
        company=company, doc_type=doc_type,
        defaults={'prefix': DocumentSequence.DEFAULT_PREFIX[doc_type]},
    )
    return seq


def highest_issued_number(company, doc_type, prefix) -> int:
    """Highest n already used as {prefix}{digits} in this company."""
    from core.models import DocumentSequence, Invoice, CreditNote
    if doc_type == DocumentSequence.INVOICE:
        numbers = Invoice.objects.filter(company=company, invoice_number__startswith=prefix) \
            .values_list('invoice_number', flat=True)
    else:
        numbers = CreditNote.objects.filter(company=company, credit_note_number__startswith=prefix) \
            .values_list('credit_note_number', flat=True)
    pattern = re.compile(r'^' + re.escape(prefix) + r'(\d+)$')
    best = 0
    for num in numbers:
        m = pattern.match(num)
        if m:
            best = max(best, int(m.group(1)))
    return best
