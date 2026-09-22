"""Turn a block of pasted spreadsheet text into rows this system can store.

Excel, Google Sheets and most fleet systems put TAB-SEPARATED text on the
clipboard, so the parsing is genuinely trivial — the work is everything after:
deciding which column is which, coercing "R32" and "30 days" into values the
models accept, and reporting honestly which rows will not import and why.

Header matching is deterministic rather than a model call: it is instant, free,
and gives the same answer twice. An LLM pass is worth adding only for headers
none of these synonyms catch.
"""
from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation


def parse_pasted(text: str) -> list[list[str]]:
    """Pasted text -> a grid. Tab-separated first (what a spreadsheet gives);
    falls back to comma for someone pasting raw CSV."""
    rows: list[list[str]] = []
    for line in (text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not line.strip():
            continue
        cells = line.split('\t') if '\t' in line else _split_csv(line)
        rows.append([c.strip().strip('"').strip() for c in cells])
    return rows


def _split_csv(line: str) -> list[str]:
    try:
        return next(csv.reader(io.StringIO(line)))
    except Exception:
        return line.split(',')


def _norm(header: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (header or '').lower())


CUSTOMER_COLUMNS: dict[str, list[str]] = {
    'name': ['customername', 'customer', 'client', 'clientname', 'name', 'account', 'business'],
    'company_name': ['company', 'companyname', 'tradingname', 'entity'],
    'contact_person': ['contactperson', 'contact', 'contactname', 'person', 'attention', 'attn'],
    'phone': ['phone', 'phonenumber', 'tel', 'telephone', 'mobile', 'cell', 'cellphone', 'contactnumber'],
    'email': ['email', 'emailaddress', 'mail'],
    'address': ['address', 'streetaddress', 'physicaladdress', 'location', 'addressline1'],
    'city': ['city', 'town', 'suburb'],
    'payment_terms': ['paymentterms', 'terms', 'payment', 'creditterms', 'days'],
    'credit_limit': ['creditlimit', 'limit', 'credit'],
}

VEHICLE_COLUMNS: dict[str, list[str]] = {
    'plate': ['registration', 'reg', 'regno', 'registrationnumber', 'plate', 'platenumber',
              'licenceplate', 'licenseplate', 'fleetno', 'fleetnumber'],
    'type': ['vehicletype', 'type', 'category', 'class', 'configuration', 'bodytype'],
    'make': ['make', 'manufacturer', 'brand'],
    'model': ['model', 'variant'],
    'gvm': ['gvm', 'gvmt', 'gvmkg', 'gvmtonnes', 'grossvehiclemass', 'grossmass', 'gross'],
    'capacity': ['capacity', 'capacityt', 'payload', 'payloadt', 'tonnes', 'tons', 'loadcapacity', 'maxload'],
    'fuel_type': ['fuel', 'fueltype'],
    'fuel_consumption_l_per_100km': ['l100km', 'lper100km', 'consumption', 'fuelconsumption',
                                     'litresper100km', 'economy'],
    'base_rate': ['baserate', 'rate', 'ratekm', 'baseratekm', 'rateperkm', 'ratepkm', 'costperkm'],
    'year': ['year', 'modelyear', 'yearmodel'],
    'vin': ['vin', 'chassis', 'chassisnumber', 'vinnumber'],
}


def map_columns(headers: list[str], schema: dict[str, list[str]]) -> dict[int, str]:
    """{column index -> field name} for the headers we recognise.

    First match wins, so a sheet carrying both "Company" and "Customer Name"
    does not map two columns onto one field.
    """
    taken: set[str] = set()
    mapping: dict[int, str] = {}
    for idx, header in enumerate(headers):
        key = _norm(header)
        if not key:
            continue
        for field, synonyms in schema.items():
            if field in taken:
                continue
            if key in {_norm(s) for s in synonyms}:
                mapping[idx] = field
                taken.add(field)
                break
    return mapping


def looks_like_header(row: list[str], schema: dict[str, list[str]]) -> bool:
    """Two or more recognised columns reads as a header. People paste without
    one often enough that guessing wrong in either direction is costly."""
    return len(map_columns(row, schema)) >= 2


def to_decimal(raw) -> Decimal | None:
    """'R32', '32,50', '1 234.5' -> Decimal. None when it is not a number."""
    if raw is None:
        return None
    cleaned = re.sub(r'[^\d.,-]', '', str(raw)).strip()
    if not cleaned:
        return None
    # "1.234,56" (European) vs "1,234.56" — whichever separator is last is the
    # decimal point.
    if ',' in cleaned and '.' in cleaned:
        if cleaned.rfind(',') > cleaned.rfind('.'):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        decimals = len(cleaned.split(',')[-1])
        cleaned = cleaned.replace(',', '.' if cleaned.count(',') == 1 and decimals <= 2 else '')
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def to_payment_terms(raw) -> str | None:
    """'30 days', 'net 60', '45' -> the stored term. None when there is no
    number to read, so the caller can flag the row rather than silently assume
    30 days and have the customer chased early."""
    if not raw:
        return None
    digits = re.search(r'(\d+)', str(raw))
    if not digits:
        return None
    days = int(digits.group(1))
    if not 0 < days <= 365:
        return None
    return f'NET{days}'


FUEL_TYPES = {'diesel': 'Diesel', 'petrol': 'Petrol', 'electric': 'Electric',
              'ev': 'Electric', 'hybrid': 'Hybrid'}


def to_fuel_type(raw) -> str:
    text = (raw or '').strip().lower()
    for key, value in FUEL_TYPES.items():
        if key in text:
            return value
    return 'Diesel'


# ---------------------------------------------------------------------------
# Validation — what will import, what will not, and why
# ---------------------------------------------------------------------------
#
# Deliberately returns every row with a verdict rather than raising on the
# first problem: the point of the preview is "45 ready, 2 need attention", and
# a fleet should never have to fix one row, resubmit, and discover the next.

def _cell(row: list[str], mapping: dict[int, str], field: str) -> str:
    for idx, name in mapping.items():
        if name == field and idx < len(row):
            return row[idx].strip()
    return ''


def validate_customers(rows: list[list[str]], mapping: dict[int, str], company) -> dict:
    from core.models import Customer

    existing = set(
        Customer.objects.filter(company=company).values_list('email', flat=True)
    )
    seen_in_paste: set[str] = set()
    out: list[dict] = []

    for i, row in enumerate(rows):
        data = {
            'name': _cell(row, mapping, 'name') or _cell(row, mapping, 'company_name'),
            'company_name': _cell(row, mapping, 'company_name'),
            'email': _cell(row, mapping, 'email').lower(),
            'phone': _cell(row, mapping, 'phone'),
            'address': _cell(row, mapping, 'address'),
            'city': _cell(row, mapping, 'city'),
        }
        # The customer is the business; the contact is the person there. Getting
        # these the wrong way round put "John Smith" in the customer list where
        # "ABC Construction" belonged.
        data['contact_person'] = _cell(row, mapping, 'contact_person')

        terms_raw = _cell(row, mapping, 'payment_terms')
        terms = to_payment_terms(terms_raw)
        if terms:
            data['payment_terms_default'] = terms
        limit = to_decimal(_cell(row, mapping, 'credit_limit'))
        if limit is not None:
            data['credit_limit'] = limit

        problems: list[str] = []
        if not data['name']:
            problems.append('No customer name')
        if not data['email']:
            problems.append('No email address')
        elif '@' not in data['email']:
            problems.append(f"'{data['email']}' is not an email address")
        elif data['email'] in existing:
            problems.append('Already one of your customers')
        elif data['email'] in seen_in_paste:
            problems.append('Appears twice in this list')
        if not data['phone']:
            problems.append('No phone number')
        if terms_raw and not terms:
            problems.append(f"Couldn't read payment terms '{terms_raw}' — 30, 60 or 90 days")

        if data['email'] and not problems:
            seen_in_paste.add(data['email'])
        out.append({'row': i + 1, 'data': data, 'problems': problems, 'ready': not problems})

    return {
        'rows': out,
        'total': len(out),
        'ready': sum(1 for r in out if r['ready']),
        'needs_attention': sum(1 for r in out if not r['ready']),
    }


def validate_vehicles(rows: list[list[str]], mapping: dict[int, str], company) -> dict:
    from core.models import Vehicle
    from core.services.vehicle_types import visible_vehicle_types_queryset

    existing_plates = {
        (p or '').upper().replace(' ', '')
        for p in Vehicle.objects.filter(company=company).values_list('plate', flat=True)
    }
    known_types = {
        (vt.name or '').lower(): vt.name
        for vt in visible_vehicle_types_queryset(company)
    }
    seen: set[str] = set()
    out: list[dict] = []

    for i, row in enumerate(rows):
        plate = _cell(row, mapping, 'plate')
        type_raw = _cell(row, mapping, 'type')
        data = {
            'plate': plate,
            'type': known_types.get(type_raw.lower(), type_raw),
            'make': _cell(row, mapping, 'make'),
            'model': _cell(row, mapping, 'model'),
            'fuel_type': to_fuel_type(_cell(row, mapping, 'fuel_type')),
        }
        for field in ('capacity', 'gvm', 'fuel_consumption_l_per_100km', 'base_rate'):
            value = to_decimal(_cell(row, mapping, field))
            if value is not None:
                data[field] = value
        year = to_decimal(_cell(row, mapping, 'year'))
        if year is not None:
            data['year'] = int(year)
        vin = _cell(row, mapping, 'vin')
        if vin:
            data['vin'] = vin

        problems: list[str] = []
        notes: list[str] = []
        key = plate.upper().replace(' ', '')
        if not plate:
            problems.append('No registration number')
        elif key in existing_plates:
            problems.append('Already in your fleet')
        elif key in seen:
            problems.append('Appears twice in this list')
        if not data['type']:
            problems.append('No vehicle type')
        elif type_raw.lower() not in known_types:
            # Not a blocker: the importer creates the type. But say so, because
            # a typo would otherwise quietly become a new vehicle type.
            notes.append(f"'{type_raw}' is a new vehicle type — it will be created")
        if data.get('capacity') is None:
            problems.append('No capacity — needed to price a load')

        if key and not problems:
            seen.add(key)
        out.append({'row': i + 1, 'data': data, 'problems': problems,
                    'notes': notes, 'ready': not problems})

    return {
        'rows': out,
        'total': len(out),
        'ready': sum(1 for r in out if r['ready']),
        'needs_attention': sum(1 for r in out if not r['ready']),
    }
