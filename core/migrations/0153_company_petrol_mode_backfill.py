"""Petrol LIVE/OWN for every existing company (QUOTE-RULES.md §1, petrol).

Petrol and hybrid trucks used to be priced only on Company.fuel_price_petrol /
fuel_price_hybrid. Now petrol works like diesel: LIVE (official FIASA petrol
price for the zone and grade) or OWN (fuel_price_petrol). Hybrid trucks use
the petrol setting.

Classification of the existing own value (fuel_price_petrol, else
fuel_price_hybrid when only that is set):
  * empty -> LIVE;
  * equal (within half a cent) to an official petrol figure (FIASA / MANUAL
    rows, 95 or 93, inland or coastal) in force in the current or the previous
    first-Wednesday period -> LIVE: it is the official price echoed back by a
    settings form, not a price the fleet chose;
  * anything else -> OWN, set_at = the company's updated_at. A hybrid-only
    value is copied into fuel_price_petrol so hybrid trucks keep pricing on it.

bulk_update of the listed fields only (portable, updated_at untouched). The
own price itself is never cleared. Reverse sets every company back to LIVE /
95 with no set_at (a copied hybrid value stays in fuel_price_petrol).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import migrations
from django.utils import timezone

TOLERANCE = Decimal('0.005')
SAST = ZoneInfo('Africa/Johannesburg')
PETROL_COLUMNS = ('petrol_95', 'petrol_93', 'petrol_95_coastal', 'petrol_93_coastal')


def _first_wednesday(y, m):
    first = date(y, m, 1)
    d = first + timedelta(days=(2 - first.weekday()) % 7)
    return datetime(d.year, d.month, d.day, 0, 1, tzinfo=SAST)


def _previous_period_start(now):
    local = now.astimezone(SAST)
    start = _first_wednesday(local.year, local.month)
    if start > now:
        prev = local.date().replace(day=1) - timedelta(days=1)
        start = _first_wednesday(prev.year, prev.month)
    prev = start.astimezone(SAST).date().replace(day=1) - timedelta(days=1)
    return _first_wednesday(prev.year, prev.month)


def official_petrol_values(FuelPrice, now):
    since = _previous_period_start(now)
    known = set()
    for row in FuelPrice.objects.filter(source__in=('FIASA', 'MANUAL')).only(
            'date', 'effective_from', *PETROL_COLUMNS):
        eff = row.effective_from or datetime(row.date.year, row.date.month, row.date.day, tzinfo=SAST)
        if eff < since or eff > now:
            continue
        known.update(Decimal(getattr(row, c)) for c in PETROL_COLUMNS if getattr(row, c) is not None)
    return known


def forwards(apps, schema_editor):
    Company = apps.get_model('core', 'Company')
    FuelPrice = apps.get_model('core', 'FuelPrice')
    known = official_petrol_values(FuelPrice, timezone.now())

    def is_official(value):
        return any(abs(Decimal(value) - k) <= TOLERANCE for k in known)

    fields = ['fuel_price_petrol_mode', 'fuel_price_petrol_set_at', 'fuel_price_petrol']
    batch = []
    for c in Company.objects.only('id', 'fuel_price_petrol', 'fuel_price_hybrid', 'updated_at') \
            .order_by('id').iterator(chunk_size=500):
        own = c.fuel_price_petrol if c.fuel_price_petrol is not None and c.fuel_price_petrol > 0 else None
        from_hybrid = False
        if own is None and c.fuel_price_hybrid is not None and c.fuel_price_hybrid > 0:
            own, from_hybrid = c.fuel_price_hybrid, True
        if own is None or is_official(own):
            c.fuel_price_petrol_mode, c.fuel_price_petrol_set_at = 'LIVE', None
        else:
            c.fuel_price_petrol_mode, c.fuel_price_petrol_set_at = 'OWN', c.updated_at
            if from_hybrid:
                c.fuel_price_petrol = own
        batch.append(c)
        if len(batch) >= 500:
            Company.objects.bulk_update(batch, fields)
            batch = []
    if batch:
        Company.objects.bulk_update(batch, fields)


def backwards(apps, schema_editor):
    Company = apps.get_model('core', 'Company')
    Company.objects.update(fuel_price_petrol_mode='LIVE', fuel_price_petrol_set_at=None,
                           fuel_price_petrol_grade='95')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0152_petrol_official_price'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
