"""Server-side figures for the Drivers page (tiles, chip counts, the open
order flag), so the page loads one page of drivers instead of every driver
plus every load. The page's rules (src/pages/Drivers.tsx), moved here.
"""
from datetime import timedelta

from django.db.models import Count, Q

from core.services.stale_work import OPEN_LOAD_STATUSES, sa_today

DELIVERED = ('DELIVERED', 'INVOICED', 'COMPLETED', 'PAID')
STATUSES = ('ACTIVE', 'INACTIVE', 'ON_LEAVE')
RENEW_SOON_DAYS = 90


def driver_name(first, last, username, pk):
    first, last = (first or '').strip(), (last or '').strip()
    if first and last:
        return f'{first} {last}'
    return first or username or f'Driver {pk}'


def open_load_numbers(company_loads) -> dict:
    """{driver_id: load_number} of one open order per driver."""
    out = {}
    for driver_id, number in (company_loads.filter(driver__isnull=False, status__in=OPEN_LOAD_STATUSES)
                              .order_by('id').values_list('driver_id', 'load_number')):
        out.setdefault(driver_id, number or '')
    return out


def summary(drivers, company_loads) -> dict:
    """Tiles over the searched drivers (status chip not applied)."""
    today = sa_today()
    counts = dict(drivers.values_list('status').annotate(n=Count('id')).values_list('status', 'n'))
    fields = ('id', 'user__first_name', 'user__last_name', 'user__username', 'license_expiry')
    name = lambda r: driver_name(r['user__first_name'], r['user__last_name'], r['user__username'], r['id'])
    expired = list(drivers.filter(license_expiry__lt=today).order_by('license_expiry', 'id').values(*fields))
    upcoming = drivers.filter(license_expiry__gte=today).order_by('license_expiry', 'id')
    nxt = upcoming.values(*fields).first()
    done = company_loads.filter(status__in=DELIVERED).aggregate(
        n=Count('id'), no_driver=Count('id', filter=Q(driver__isnull=True)))
    return {
        'status_counts': {'ALL': drivers.count(), **{s: counts.get(s, 0) for s in STATUSES}},
        'expired_count': len(expired),
        'expired_names': [name(r) for r in expired[:2]],
        'next_renewal': {'name': name(nxt), 'date': nxt['license_expiry'].isoformat()} if nxt else None,
        'renew_soon': upcoming.filter(license_expiry__lte=today + timedelta(days=RENEW_SOON_DAYS)).count(),
        'delivered_loads': done['n'] or 0,
        'no_driver_loads': done['no_driver'] or 0,
        'has_efficiency': drivers.filter(efficiency_score__gt=0).exists(),
        'has_revenue': drivers.filter(revenue_generated__gt=0).exists(),
    }
