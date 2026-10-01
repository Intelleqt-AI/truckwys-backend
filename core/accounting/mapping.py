"""Per-connection mapping: revenue types and expense categories to account
codes, TruckWys tax codes to the org's tax rates, tracking categories.

Rules:
  * Never guess. Suggestions are offered to the UI, never applied; sync is
    blocked until every key is mapped (`missing()` is empty).
  * Validated against what the provider says exists right now (accounts and
    tax rates are read live and cached on the connection).
  * A tax rate must carry the rate of the TruckWys code it stands for
    (STANDARD 15%, every other code 0%), so a mis-mapping can't change VAT.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from core import revenue_types, tax_codes
from core.accounting.registry import get_adapter

EXPENSE_CATEGORY_CHOICES = [
    ('FUEL', 'Fuel'), ('TOLLS', 'Tolls'), ('MAINTENANCE', 'Maintenance'), ('DRIVER_COST', 'Driver cost'),
    ('SUBCONTRACTOR', 'Subcontractor'), ('INSURANCE', 'Insurance'), ('OVERHEAD', 'Overhead'), ('OTHER', 'Other'),
]
EXPENSE_CATEGORIES = [c for c, _ in EXPENSE_CATEGORY_CHOICES]
SECTIONS = ('revenue_types', 'expense_categories', 'tax_sales', 'tax_purchases')


class MappingError(ValueError):
    def __init__(self, errors: dict):
        super().__init__('; '.join(f'{k}: {v}' for k, v in errors.items()))
        self.errors = errors


def _settings(connection) -> dict:
    s = dict(connection.settings or {})
    for sec in SECTIONS:
        s.setdefault(sec, {})
    s.setdefault('tracking', {})
    return s


# ---------------------------------------------------------------- options

def refresh_options(connection) -> dict:
    """Read accounts, tax rates and tracking categories live from the provider."""
    adapter = get_adapter(connection)
    opts = {
        'accounts': [{'code': a.code, 'name': a.name, 'type': a.type, 'class': a.account_class,
                      'is_bank': a.is_bank} for a in adapter.get_accounts() if a.status == 'ACTIVE'],
        'tax_rates': [{'code': t.code, 'name': t.name, 'rate': str(t.rate), 'revenue': t.revenue,
                       'expenses': t.expenses} for t in adapter.get_tax_rates() if t.status == 'ACTIVE'],
        'tracking_categories': [{'id': c.id, 'name': c.name,
                                 'options': [{'id': o.id, 'name': o.name} for o in c.options]}
                                for c in adapter.get_tracking() if c.status == 'ACTIVE'],
        'fetched_at': timezone.now().isoformat(),
    }
    from django.db import transaction
    from core.models import AccountingConnection
    with transaction.atomic():   # merge into the stored settings, never overwrite a mapping saved meanwhile
        fresh = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
        s = dict(fresh.settings or {})
        s['options'] = opts
        fresh.settings = s
        fresh.save(update_fields=['settings', 'updated_at'])
    connection.settings = s
    return opts


def options(connection, *, refresh=False) -> dict:
    opts = (connection.settings or {}).get('options')
    if refresh or not opts:
        opts = refresh_options(connection)
    return opts


# ---------------------------------------------------------------- state

def expected_rate(code: str) -> Decimal:
    return tax_codes.rate_percent(code, date.today())


def missing(connection) -> list[str]:
    s = _settings(connection)
    out = []
    for key in revenue_types.REVENUE_TYPES:
        if not s['revenue_types'].get(key):
            out.append(f'revenue:{key}')
    for key in EXPENSE_CATEGORIES:
        if not s['expense_categories'].get(key):
            out.append(f'expense:{key}')
    for key in _tax_codes_needed(connection):
        if not s['tax_sales'].get(key):
            out.append(f'tax_sales:{key}')
        if not s['tax_purchases'].get(key):
            out.append(f'tax_purchases:{key}')
    return out


def _tax_codes_needed(connection):
    # A non-VAT-vendor company only ever uses NO_VAT on its sales; its
    # expenses can still be any code (it buys from VAT vendors) but it can't
    # claim input VAT, so purchases are still mapped in full.
    return tax_codes.TAX_CODES


def is_complete(connection) -> bool:
    return not missing(connection)


def suggestions(connection) -> dict:
    """Hints only. Applied by the user pressing Save, never automatically."""
    opts = (connection.settings or {}).get('options') or {}
    rates = opts.get('tax_rates') or []

    def pick(code, side):
        want = expected_rate(code)
        pool = [r for r in rates if Decimal(r['rate']) == want and r.get(side)]
        if code == tax_codes.STANDARD:
            cands = pool
        else:
            words = {'ZERO_RATED': ('zero',), 'EXEMPT': ('exempt',), 'NO_VAT': ('no vat', 'no tax', 'none')}[code]
            cands = [r for r in pool if any(w in r['name'].lower() or w == r['code'].lower() for w in words)]
        return cands[0]['code'] if len(cands) == 1 else None

    out = {'tax_sales': {}, 'tax_purchases': {}, 'revenue_types': {}, 'expense_categories': {}}
    for code in tax_codes.TAX_CODES:
        s, p = pick(code, 'revenue'), pick(code, 'expenses')
        if s:
            out['tax_sales'][code] = s
        if p:
            out['tax_purchases'][code] = p
    revenue_accounts = [a for a in opts.get('accounts') or [] if (a.get('class') or a.get('type')) in ('REVENUE', 'Income')
                        or a.get('type') in ('REVENUE', 'SALES', 'Income')]
    if len(revenue_accounts) == 1:
        out['revenue_types'] = {k: revenue_accounts[0]['code'] for k in revenue_types.REVENUE_TYPES}
    return out


def state(connection) -> dict:
    s = _settings(connection)
    opts = s.get('options') or {}
    tax_label = dict(tax_codes.TAX_CODE_CHOICES)
    return {
        'revenue_types': [{'key': k, 'label': label, 'account_code': s['revenue_types'].get(k)}
                          for k, label in revenue_types.REVENUE_TYPE_CHOICES],
        'expense_categories': [{'key': k, 'label': label, 'account_code': s['expense_categories'].get(k)}
                               for k, label in EXPENSE_CATEGORY_CHOICES],
        'tax_sales': [{'key': k, 'label': tax_label[k], 'rate': str(expected_rate(k)),
                       'tax_code': s['tax_sales'].get(k)} for k in tax_codes.TAX_CODES],
        'tax_purchases': [{'key': k, 'label': tax_label[k], 'rate': str(expected_rate(k)),
                           'tax_code': s['tax_purchases'].get(k)} for k in tax_codes.TAX_CODES],
        'receipts_account': s.get('receipts_account') or None,
        'tracking': {'vehicle_category_id': s['tracking'].get('vehicle_category_id') or None,
                     'branch_category_id': s['tracking'].get('branch_category_id') or None,
                     'branch_option': s['tracking'].get('branch_option') or ''},
        'options': {'accounts': opts.get('accounts') or [], 'tax_rates': opts.get('tax_rates') or [],
                    'tracking_categories': opts.get('tracking_categories') or [],
                    'fetched_at': opts.get('fetched_at')},
        'suggestions': suggestions(connection),
        'complete': is_complete(connection),
        'missing': missing(connection),
    }


# ---------------------------------------------------------------- update

def update(connection, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise MappingError({'mapping': 'Expected an object.'})
    opts = options(connection)
    accounts = {a['code']: a for a in opts.get('accounts') or []}
    rates = {r['code']: r for r in opts.get('tax_rates') or []}
    cats = {c['id']: c for c in opts.get('tracking_categories') or []}
    s = _settings(connection)
    errors = {}

    def accounts_section(name, keys):
        raw = payload.get(name)
        if raw is None:
            return
        if not isinstance(raw, dict):
            errors[name] = 'Expected an object of key -> account code.'
            return
        for key, code in raw.items():
            if key not in keys:
                errors[f'{name}.{key}'] = 'Unknown key.'
                continue
            if code in (None, ''):
                s[name].pop(key, None)
                continue
            if str(code) not in accounts:
                errors[f'{name}.{key}'] = f'Account {code} doesn\'t exist (or is archived) in {connection.get_provider_display()}.'
                continue
            if accounts[str(code)].get('is_bank'):
                errors[f'{name}.{key}'] = f'{code} is a bank account; choose an income or expense account.'
                continue
            s[name][key] = str(code)

    def tax_section(name, side):
        raw = payload.get(name)
        if raw is None:
            return
        if not isinstance(raw, dict):
            errors[name] = 'Expected an object of tax code -> provider tax rate.'
            return
        for key, code in raw.items():
            if key not in tax_codes.TAX_CODES:
                errors[f'{name}.{key}'] = 'Unknown TruckWys tax code.'
                continue
            if code in (None, ''):
                s[name].pop(key, None)
                continue
            rate = rates.get(str(code))
            if rate is None:
                errors[f'{name}.{key}'] = f'Tax rate {code} doesn\'t exist (or is inactive).'
                continue
            want = expected_rate(key)
            if Decimal(rate['rate']) != want:
                errors[f'{name}.{key}'] = (f'{rate["name"]} is {rate["rate"]}%; {key} needs a {want}% rate.')
                continue
            if not rate.get(side):
                errors[f'{name}.{key}'] = (f'{rate["name"]} can\'t be used on '
                                           f'{"sales" if side == "revenue" else "purchases"}.')
                continue
            s[name][key] = str(code)

    accounts_section('revenue_types', revenue_types.REVENUE_TYPES)
    accounts_section('expense_categories', EXPENSE_CATEGORIES)
    tax_section('tax_sales', 'revenue')
    tax_section('tax_purchases', 'expenses')

    if 'receipts_account' in payload:
        code = payload.get('receipts_account')
        if code in (None, ''):
            s.pop('receipts_account', None)
        elif str(code) not in accounts or not accounts[str(code)].get('is_bank'):
            errors['receipts_account'] = 'Choose a bank account (payments can be received into it).'
        else:
            s['receipts_account'] = str(code)

    if 'tracking' in payload:
        t = payload.get('tracking') or {}
        if not isinstance(t, dict):
            errors['tracking'] = 'Expected an object.'
        else:
            for fld in ('vehicle_category_id', 'branch_category_id'):
                if fld in t:
                    val = t.get(fld)
                    if val in (None, ''):
                        s['tracking'].pop(fld, None)
                    elif val not in cats:
                        errors[f'tracking.{fld}'] = 'That tracking category doesn\'t exist.'
                    else:
                        s['tracking'][fld] = val
            if 'branch_option' in t:
                opt = str(t.get('branch_option') or '').strip()
                cat_id = s['tracking'].get('branch_category_id')
                if opt and not cat_id:
                    errors['tracking.branch_option'] = 'Choose the branch tracking category first.'
                elif opt and cat_id and opt.lower() not in {o['name'].lower() for o in cats[cat_id]['options']}:
                    errors['tracking.branch_option'] = f'"{opt}" isn\'t an option of {cats[cat_id]["name"]}.'
                else:
                    s['tracking']['branch_option'] = opt
            vc, bc = s['tracking'].get('vehicle_category_id'), s['tracking'].get('branch_category_id')
            if vc and bc and vc == bc:
                errors['tracking'] = 'Vehicle and branch need two different tracking categories.'

    if errors:
        raise MappingError(errors)
    # Write only the mapping keys, merged into the stored settings under a
    # lock, so a cut-over date or option cache saved meanwhile survives.
    from django.db import transaction
    from core.models import AccountingConnection
    keys = SECTIONS + ('tracking', 'receipts_account')
    with transaction.atomic():
        fresh = AccountingConnection.objects.select_for_update().get(pk=connection.pk)
        merged = dict(fresh.settings or {})
        for k in keys:
            if k in s:
                merged[k] = s[k]
            else:
                merged.pop(k, None)
        fresh.settings = merged
        fresh.save(update_fields=['settings', 'updated_at'])
    connection.settings = merged
    from core.accounting.events import log_event
    log_event(connection, 'mapping', 'Mapping saved' + ('' if is_complete(connection) else
                                                         f' ({len(missing(connection))} still to map)'))
    # Documents waiting on a mapping can go now.
    if is_complete(connection):
        from core.accounting.sync import requeue_blocked
        requeue_blocked(connection)
    return state(connection)


# ---------------------------------------------------------------- lookups for documents

def account_for_revenue(connection, revenue_type):
    return _settings(connection)['revenue_types'].get(revenue_type)


def account_for_expense(connection, category):
    return _settings(connection)['expense_categories'].get(category)


def sales_tax(connection, code):
    return _settings(connection)['tax_sales'].get(code)


def purchase_tax(connection, code):
    return _settings(connection)['tax_purchases'].get(code)


def reverse_sales_tax(connection) -> dict:
    """provider tax code -> TruckWys code, only where unambiguous."""
    out, seen = {}, {}
    for code, ext in _settings(connection)['tax_sales'].items():
        seen.setdefault(ext, []).append(code)
    for ext, codes in seen.items():
        if len(codes) == 1:
            out[ext] = codes[0]
    return out


def tracking(connection) -> dict:
    return _settings(connection)['tracking']


def category_name(connection, category_id) -> str:
    for c in ((connection.settings or {}).get('options') or {}).get('tracking_categories') or []:
        if c['id'] == category_id:
            return c['name']
    return ''
