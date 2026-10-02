"""Normalising and validating business identifiers (CIPC, VAT, legal name),
and linking tenant Customers to the global DebtorIdentity."""
import re
import unicodedata

# Legal-form suffixes stripped from the name key, longest first so
# "(pty) ltd" goes before "ltd".
_SUFFIXES = [
    'proprietary limited', 'pty limited', 'pty ltd', 'pty', 'limited', 'ltd',
    'close corporation', 'cc', 'incorporated', 'inc', 'soc ltd', 'soc', 'npc', 'rf',
    'holdings', 'group',
]
_SUFFIX_RE = re.compile(r'(?:\s+(?:' + '|'.join(re.escape(s) for s in _SUFFIXES) + r'))+$')


def legal_name_key(name) -> str:
    """'ABC Logistics (Pty) Ltd.' -> 'abc logistics'. Accents folded,
    punctuation dropped, '&' read as 'and', legal suffixes removed."""
    if not name:
        return ''
    s = unicodedata.normalize('NFKD', str(name)).encode('ascii', 'ignore').decode('ascii')
    s = s.lower().replace('&', ' and ')
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    stripped = _SUFFIX_RE.sub('', s).strip()
    return (stripped or s)[:200]


_CIPC_RE = re.compile(r'^(?:CK)?\s*(\d{4})\D*(\d{6})\D*(\d{2})$', re.IGNORECASE)


def normalise_registration_number(value, country='ZA') -> str:
    """CIPC enterprise number to 'YYYY/NNNNNN/NN'. Accepts '2015/123456/07',
    '2015 123456 07', '201512345607', 'CK2015/123456/23' (old close
    corporation prefix). Raises ValueError if it can't be a CIPC number.
    Non-ZA numbers are trimmed and upper-cased only."""
    if value is None:
        return ''
    raw = str(value).strip().upper()
    if not raw:
        return ''
    if (country or 'ZA').upper() != 'ZA':
        return re.sub(r'\s+', ' ', raw)[:20]
    m = _CIPC_RE.match(raw)
    if not m:
        raise ValueError('Registration number must be a CIPC number like 2015/123456/07')
    year, num, kind = m.groups()
    if not (1900 <= int(year) <= 2100):
        raise ValueError('Registration number year looks wrong (expected YYYY/NNNNNN/NN)')
    return f'{year}/{num}/{kind}'


def normalise_vat_number(value, country='ZA') -> str:
    """SA VAT number: 10 digits starting with 4. Spaces/dashes ignored."""
    if value is None:
        return ''
    raw = str(value).strip()
    if not raw:
        return ''
    if (country or 'ZA').upper() != 'ZA':
        return re.sub(r'\s+', '', raw).upper()[:20]
    digits = re.sub(r'[\s\-]', '', raw)
    if not re.fullmatch(r'4\d{9}', digits):
        raise ValueError('A South African VAT number is 10 digits starting with 4')
    return digits


def link_debtor_identity(customer):
    """Point a Customer at the global DebtorIdentity for its registration /
    VAT number, creating it if needed. A customer with neither stays
    unlinked (a name alone is not an identity). Returns the identity or None.

    Registration number wins over VAT number when they point at two
    different identities (a group's companies can share a VAT number under
    group registration, never a CIPC number)."""
    from django.db import IntegrityError, transaction
    from core.models import DebtorIdentity

    reg = customer.registration_number or None
    vat = customer.vat_number or None
    if not reg and not vat:
        return None
    country = customer.country or 'ZA'

    identity = None
    if reg:
        identity = DebtorIdentity.objects.filter(registration_number=reg).first()
    if identity is None and vat:
        identity = DebtorIdentity.objects.filter(vat_number=vat).first()
        if identity is not None and reg and identity.registration_number and identity.registration_number != reg:
            identity = None
    if identity is None:
        try:
            with transaction.atomic():
                identity = DebtorIdentity.objects.create(
                    registration_number=reg,
                    vat_number=vat if not (vat and DebtorIdentity.objects.filter(vat_number=vat).exists()) else None,
                    legal_name_key=legal_name_key(customer.company_name or customer.name),
                    country=country,
                )
        except IntegrityError:
            identity = (DebtorIdentity.objects.filter(registration_number=reg).first() if reg else None) \
                or (DebtorIdentity.objects.filter(vat_number=vat).first() if vat else None)
    else:
        changed = []
        if reg and not identity.registration_number:
            identity.registration_number = reg
            changed.append('registration_number')
        if vat and not identity.vat_number and not DebtorIdentity.objects.filter(vat_number=vat).exists():
            identity.vat_number = vat
            changed.append('vat_number')
        if changed:
            try:
                with transaction.atomic():
                    identity.save(update_fields=changed + ['updated_at'])
            except IntegrityError:
                pass
    return identity
