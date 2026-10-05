"""Server-side Orders and History lists (src/pages/LoadsList.tsx): the tab,
search, newest-first order and the tiles, so the pages load one page of
loads instead of every load.

- Orders:  Pending, Assigned, Loading, In transit
- History: Delivered, Invoiced, Cancelled; newest first by delivered date,
           else due date, else pickup
- Left open (stale, core.services.stale_work): past the delivery date, or
  picked up more than 30 days ago (SA calendar days). pickup_date is
  required, so "pickup, else created" is always pickup here.
- Amounts are order totals incl. VAT (core.services.quote_vat).
"""
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.db.models.functions import Coalesce

from core.services.quote_vat import sum_incl_vat
from core.services.stale_work import SA, sa_today

ORDER_STATUSES = ('PENDING', 'ASSIGNED', 'LOADING', 'IN_TRANSIT')
HISTORY_STATUSES = ('DELIVERED', 'INVOICED', 'CANCELLED')
ON_THE_MOVE = ('LOADING', 'IN_TRANSIT')
TABS = {'orders': ORDER_STATUSES, 'history': HISTORY_STATUSES}

SEARCH_Q = ('customer__name', 'load_number', 'pickup_location', 'delivery_location', 'pickup_city',
            'delivery_city', 'driver__user__first_name', 'driver__user__last_name', 'vehicle__plate',
            'vehicle__make', 'vehicle__model')


def search(qs, text):
    text = (text or '').strip()
    if not text:
        return qs
    q = Q()
    for field in SEARCH_Q:
        q |= Q(**{f'{field}__icontains': text})
    return qs.filter(q)


def _sa_midnight(day):
    return datetime.combine(day, time.min, tzinfo=SA)


def overdue_q(today=None) -> Q:
    """Past the delivery date (its SA day is before today)."""
    return Q(delivery_date__lt=_sa_midnight(today or sa_today()))


def stale_q(today=None) -> Q:
    today = today or sa_today()
    return Q(status__in=ORDER_STATUSES) & (
        overdue_q(today) | Q(pickup_date__lt=_sa_midnight(today - timedelta(days=30))))


def newest_first(qs):
    return qs.annotate(_when=Coalesce('actual_delivered_at', 'delivery_date', 'pickup_date', 'created_at')) \
        .order_by('-_when', '-id')


def orders_summary(loads) -> dict:
    """Tiles over every open order (the tab, before chip and search)."""
    today = sa_today()
    open_ = loads.filter(status__in=ORDER_STATUSES)
    need = open_.filter(vehicle__isnull=True).exclude(status__in=ON_THE_MOVE)
    transit = open_.filter(status='IN_TRANSIT')
    stale = open_.filter(stale_q(today))
    return {
        'open_count': open_.count(),
        'need_vehicle': need.count(),
        'need_vehicle_overdue': need.filter(overdue_q(today)).count(),
        'moving_no_vehicle': open_.filter(vehicle__isnull=True, status__in=ON_THE_MOVE).count(),
        'in_transit': transit.count(),
        'in_transit_overdue': transit.filter(overdue_q(today)).count(),
        'left_open': stale.count(),
        'open_total_incl_vat': float(sum_incl_vat(open_)),
        'any_loads': loads.exists(),
    }


def history_summary(loads) -> dict:
    hist = loads.filter(status__in=HISTORY_STATUSES)
    invoiced = hist.filter(status='INVOICED')
    completed = hist.exclude(status='CANCELLED')
    return {
        'history_count': hist.count(),
        'delivered_not_invoiced': hist.filter(status='DELIVERED').count(),
        'invoiced': invoiced.count(),
        'invoiced_total_incl_vat': float(sum_incl_vat(invoiced)),
        'completed': completed.count(),
        'completed_total_incl_vat': float(sum_incl_vat(completed)),
        'any_loads': loads.exists(),
    }
