"""Company banking details for "How to pay" blocks on invoices.

One source of truth for every surface that tells a customer how to pay a
company's invoice: the invoice PDF, the invoice emails and the public invoice
page payload. Each of those shows the bank block ONLY when the company has
filled in enough to actually pay (bank name + account number); otherwise it
keeps the old "contact <company> for banking details" wording.

POPIA: these are the company's OWN business bank details, shown only on that
company's own invoices to that invoice's customer. Nothing here is ever read
for another tenant.
"""
from html import escape

ACCOUNT_TYPE_LABELS = {
    'CHEQUE': 'Cheque / current',
    'SAVINGS': 'Savings',
    'TRANSMISSION': 'Transmission',
}

DEFAULT_REFERENCE_TEXT = 'Please use the invoice number as your payment reference.'


def _clean(value):
    return (value or '').strip() if isinstance(value, str) else (str(value).strip() if value else '')


def company_bank_details(company):
    """Return a dict of the company's bank details, or None when not set.

    "Set" means both a bank name and an account number are present — anything
    less isn't payable, so callers fall back to the legacy wording.
    """
    if company is None:
        return None
    bank_name = _clean(getattr(company, 'bank_name', None))
    account_number = _clean(getattr(company, 'bank_account_number', None))
    if not bank_name or not account_number:
        return None
    account_type = _clean(getattr(company, 'bank_account_type', None))
    return {
        'bank_name': bank_name,
        'account_holder': _clean(getattr(company, 'bank_account_holder', None))
                          or _clean(getattr(company, 'company_name', None)),
        'account_number': account_number,
        'branch_code': _clean(getattr(company, 'bank_branch_code', None)),
        'account_type': account_type,
        'account_type_label': ACCOUNT_TYPE_LABELS.get(account_type, ''),
        'payment_reference_hint': _clean(getattr(company, 'payment_reference_hint', None)),
    }


def bank_detail_rows(details):
    """Ordered (label, value) rows for display, skipping empty values."""
    if not details:
        return []
    rows = [
        ('Bank', details['bank_name']),
        ('Account holder', details['account_holder']),
        ('Account number', details['account_number']),
        ('Branch code', details['branch_code']),
        ('Account type', details['account_type_label']),
    ]
    return [(label, value) for label, value in rows if value]


def reference_text(details, invoice_number):
    """The payment-reference sentence: the company's own hint if set, else the default."""
    hint = details.get('payment_reference_hint') if details else ''
    if hint:
        return hint
    return f'Please use the invoice number {invoice_number} as your payment reference.'


def bank_details_html(details, row_separator='<br>'):
    """HTML-escaped "Label: value" lines for the email templates."""
    return row_separator.join(
        f'{escape(label)}: <strong>{escape(value)}</strong>'
        for label, value in bank_detail_rows(details)
    )


def public_payment_details(company):
    """Payload for the public invoice page (None when not set)."""
    details = company_bank_details(company)
    if not details:
        return None
    return {
        'bank_name': details['bank_name'],
        'account_holder': details['account_holder'],
        'account_number': details['account_number'],
        'branch_code': details['branch_code'],
        'account_type': details['account_type'],
        'account_type_label': details['account_type_label'],
        'payment_reference_hint': details['payment_reference_hint'],
    }
