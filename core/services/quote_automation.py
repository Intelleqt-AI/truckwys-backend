"""Company settings for the quote follow-up features, and the pricing setup
status (were the pricing inputs consciously set, or still defaults?).

See core/models/quote_followups.py and docs/QUOTE-RULES.md ("Quote follow-ups").
"""
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
SAST = ZoneInfo('Africa/Johannesburg')

# Bounds for the editable settings: (min, max, plain message).
BOUNDS = {
    'fuel_surcharge_threshold_pct': (Decimal('1'), Decimal('25'),
                                     'Enter a fuel price change between 1% and 25%.'),
    'follow_up_after_days': (1, 30, 'Enter a number of days between 1 and 30.'),
    'expiry_nudge_days': (1, 14, 'Enter a number of days between 1 and 14.'),
}
BOOL_FIELDS = ('fuel_surcharge_enabled', 'fuel_alerts_enabled', 'follow_ups_enabled',
               'weekly_margin_email_enabled')


def get_settings(company):
    """The company's QuoteAutomationSettings, created on first use for a
    company that has none (a new company: fuel clause on, no prompt)."""
    from core.models import QuoteAutomationSettings
    if company is None:
        return None
    # Always read the row (never a cached reverse relation on a long-lived
    # company instance, e.g. request.user.company).
    obj = QuoteAutomationSettings.objects.filter(company_id=company.pk).first()
    if obj is not None:
        return obj
    try:
        with transaction.atomic():
            obj, _ = QuoteAutomationSettings.objects.get_or_create(company=company)
    except IntegrityError:
        obj = QuoteAutomationSettings.objects.get(company=company)
    return obj


def settings_out(s):
    return {
        'fuel_surcharge_enabled': s.fuel_surcharge_enabled,
        'fuel_surcharge_threshold_pct': float(s.fuel_surcharge_threshold_pct),
        'fuel_surcharge_prompt_pending': s.fuel_surcharge_prompt_pending,
        'fuel_surcharge_decided_at': _iso(s.fuel_surcharge_decided_at),
        'fuel_alerts_enabled': s.fuel_alerts_enabled,
        'follow_ups_enabled': s.follow_ups_enabled,
        'follow_up_after_days': s.follow_up_after_days,
        'expiry_nudge_days': s.expiry_nudge_days,
        'weekly_margin_email_enabled': s.weekly_margin_email_enabled,
    }


def update_settings(company, data):
    """Validate and apply a partial update. Returns (settings, errors{field: msg})."""
    s = get_settings(company)
    errors, changes = {}, {}
    for f in BOOL_FIELDS:
        if f in data:
            v = data[f]
            if not isinstance(v, bool):
                errors[f] = 'Choose on or off.'
            else:
                changes[f] = v
    for f, (lo, hi, msg) in BOUNDS.items():
        if f not in data:
            continue
        raw = data[f]
        try:
            if isinstance(raw, bool):
                raise ValueError
            v = Decimal(str(raw)) if isinstance(lo, Decimal) else int(str(raw))
            if isinstance(lo, Decimal):
                v = v.quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError, TypeError):
            errors[f] = msg
            continue
        current = getattr(s, f)
        if v == current:
            continue            # an unchanged stored value always saves
        if not (lo <= v <= hi):
            errors[f] = msg
            continue
        changes[f] = v
    if errors:
        return s, errors
    if 'fuel_surcharge_enabled' in data or data.get('fuel_surcharge_prompt_dismissed') is True:
        # Any decision on the clause answers the one-time prompt.
        changes['fuel_surcharge_prompt_pending'] = False
        changes['fuel_surcharge_decided_at'] = timezone.now()
    for k, v in changes.items():
        setattr(s, k, v)
    if changes:
        s.save()
    return s, {}


# ---------------------------------------------------------------------------
# Pricing setup (onboarding step)
# ---------------------------------------------------------------------------

PRICING_FIELDS = {
    # key: (Company field, label)
    'target_margin': ('margin_target_pct', 'Target margin'),
    'operating_cost': ('operating_cost_per_km', 'Operating cost per km'),
    'driver_allowance': ('driver_allowance_per_night', 'Driver allowance per night'),
    'fuel_mode': ('fuel_price_mode', 'Fuel price'),
}
COMPANY_FIELD_TO_KEY = {v[0]: k for k, v in PRICING_FIELDS.items()}
DEFAULT_TARGET_MARGIN = Decimal('10.00')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def infer_pricing_setup(company):
    """What an existing company has evidently set (used once by the
    migration): a non-default target margin, an operating cost, a driver
    allowance, or an own fuel price. LIVE fuel can't be told apart from the
    default, so it is not inferred."""
    out = {}
    now = timezone.now().isoformat()
    tm = getattr(company, 'margin_target_pct', None)
    if tm is not None and Decimal(str(tm)) != DEFAULT_TARGET_MARGIN:
        out['target_margin'] = {'how': 'inferred', 'at': now}
    if getattr(company, 'operating_cost_per_km', None) is not None:
        out['operating_cost'] = {'how': 'inferred', 'at': now}
    if getattr(company, 'driver_allowance_per_night', None) is not None:
        out['driver_allowance'] = {'how': 'inferred', 'at': now}
    if (getattr(company, 'fuel_price_mode', None) or 'LIVE') == 'OWN':
        out['fuel_mode'] = {'how': 'inferred', 'at': now}
    return out


def record_pricing_changes(company_id, changed_keys, how='changed'):
    """Mark pricing inputs as consciously set (a person saved a new value)."""
    from core.models import QuoteAutomationSettings
    if not changed_keys:
        return
    try:
        with transaction.atomic():
            s, _ = QuoteAutomationSettings.objects.select_for_update().get_or_create(company_id=company_id)
            setup = dict(s.pricing_setup or {})
            now = timezone.now().isoformat()
            for k in changed_keys:
                setup[k] = {'how': how, 'at': now}
            s.pricing_setup = setup
            s.save(update_fields=['pricing_setup', 'updated_at'])
    except Exception:
        logger.exception('pricing setup record failed for company %s', company_id)


def _fmt_rand(v, dp=2):
    from core.services.quote_costing import fmt_rand
    return fmt_rand(float(v), dp)


def _fmt_pct(v):
    from core.services.quote_costing import fmt_num
    v = float(v)
    return f'{fmt_num(v, 0 if v == int(v) else 1)}%'


def pricing_setup_status(company, today=None):
    """{needs_setup, dismissed_at, items: [...]} — see the client spec."""
    from core.services.quote_costing import driver_rate
    s = get_settings(company)
    setup = s.pricing_setup or {}
    today = today or timezone.now().astimezone(SAST).date()
    items = []

    tm = company.margin_target_pct
    items.append({
        'key': 'target_margin', 'label': 'Target margin',
        'value': float(tm) if tm is not None else None,
        'display': _fmt_pct(tm if tm is not None else DEFAULT_TARGET_MARGIN),
        'default_text': 'We use 10% until you set your own target.',
    })

    oc = company.operating_cost_per_km
    items.append({
        'key': 'operating_cost', 'label': 'Operating cost per km',
        'value': float(oc) if oc is not None else None,
        'display': f'{_fmt_rand(oc)}/km' if oc is not None else 'Standard estimate per truck class',
        'default_text': ('Until you enter your own figure we use your recorded expenses, else a standard '
                         'estimate for each truck class (excl. fuel and tolls).'),
    })

    rate, source = driver_rate(company, today)
    da = company.driver_allowance_per_night
    if source == 'approved_allowance':
        from core.services.quote_ai_pricing import stored_allowance
        allowance = stored_allowance(today) or {}
        eff = allowance.get('effective_from')
        when = f' from {eff.day} {_MONTHS[eff.month - 1]} {eff.year}' if hasattr(eff, 'month') else ''
        label = ('NBCRFLI allowance' if allowance.get('allowance_type') == 'nbcrfli'
                 else (allowance.get('label') or 'allowance'))
        default_text = f'We use the approved {label} of {_fmt_rand(rate)} a night{when}.'
    elif source == 'company_setting':
        default_text = 'You pay your own allowance per night away.'
    else:
        default_text = 'No approved allowance is on record yet. Enter what you pay your drivers per night away.'
    items.append({
        'key': 'driver_allowance', 'label': 'Driver allowance per night',
        'value': float(da) if da is not None else None,
        'rate_in_use': rate, 'rate_source': source,
        'display': f'{_fmt_rand(rate)} a night' if rate is not None else 'Not set',
        'default_text': default_text,
    })

    mode = (company.fuel_price_mode or 'LIVE').upper()
    zone = 'coastal' if (company.fuel_zone or '').upper() == 'COASTAL' else 'inland'
    items.append({
        'key': 'fuel_mode', 'label': 'Fuel price',
        'value': mode,
        'display': f'Official {zone} price' if mode == 'LIVE' else 'Your own price',
        'default_text': f'We use the official {zone} diesel price, updated on the first Wednesday of each month.',
    })

    for it in items:
        rec = setup.get(it['key']) or {}
        it['set'] = bool(rec)
        it['how'] = rec.get('how')
        it['set_at'] = rec.get('at')
    unset = [it['key'] for it in items if not it['set']]
    return {
        'needs_setup': bool(unset) and s.pricing_setup_dismissed_at is None,
        'unset': unset,
        'dismissed_at': _iso(s.pricing_setup_dismissed_at),
        'items': items,
    }


def confirm_pricing_setup(company, keys):
    """The user accepted the shown values (defaults included) for `keys`."""
    bad = [k for k in keys if k not in PRICING_FIELDS]
    if bad:
        return {'keys': f'Unknown item: {", ".join(bad)}.'}
    s = get_settings(company)
    setup = dict(s.pricing_setup or {})
    now = timezone.now().isoformat()
    for k in keys:
        if not setup.get(k):
            setup[k] = {'how': 'confirmed', 'at': now}
    s.pricing_setup = setup
    s.save(update_fields=['pricing_setup', 'updated_at'])
    return {}


def dismiss_pricing_setup(company):
    s = get_settings(company)
    s.pricing_setup_dismissed_at = timezone.now()
    s.save(update_fields=['pricing_setup_dismissed_at', 'updated_at'])


def _iso(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.astimezone(SAST).isoformat()
    return str(dt)
