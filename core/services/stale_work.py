"""Stale work (R6): the frontend's one rule (src/lib/staleWork.ts), for the
server-side lists that need it (Vehicles "Doing now", the tiles).

An open load (Pending, Assigned, Loading or In transit) is stale when it is
past its delivery date, or open for more than 30 days counted from its
pickup date, else from when it was created. Days are whole South African
calendar days, whatever the server's time zone.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

SA = ZoneInfo('Africa/Johannesburg')
OPEN_LOAD_STATUSES = ('PENDING', 'ASSIGNED', 'LOADING', 'IN_TRANSIT')
STALE_AFTER_DAYS = 30


def sa_today() -> date:
    return timezone.now().astimezone(SA).date()


def sa_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if timezone.is_aware(value) else timezone.make_aware(value)).astimezone(SA).date()
    return value


def is_open(status) -> bool:
    return str(status or '').upper() in OPEN_LOAD_STATUSES


def is_stale(status, delivery_date, pickup_date, created_at, today: date | None = None) -> bool:
    if not is_open(status):
        return False
    today = today or sa_today()
    due = sa_date(delivery_date)
    if due and (today - due).days > 0:
        return True
    start = sa_date(pickup_date) or sa_date(created_at)
    return bool(start and (today - start).days > STALE_AFTER_DAYS)
