"""Keep a truck's status in step with its loads.

A truck on a current Assigned, Loading or In-transit load is In use; once it
has none (delivered, cancelled, moved to another truck or deleted), it is
Available again. A load left open (past its dates, core.services.stale_work)
is not current work, so it never marks a truck In use: the Vehicles page
shows that truck as available, holding an order left open. Maintenance and
Out of service are the workshop's call and are never overridden. Called from
the Load signals (core.signals).
"""
from core.models import Load, Vehicle
from core.services.stale_work import is_stale, sa_today
from core.services.vehicle_list import ACTIVE_LOAD, TILES


def has_current_load(vehicle_id, today=None) -> bool:
    today = today or sa_today()
    rows = Load.objects.filter(vehicle_id=vehicle_id, status__in=ACTIVE_LOAD).values_list(
        'status', 'delivery_date', 'pickup_date', 'created_at')
    return any(not is_stale(*row, today) for row in rows)


def sync_vehicle_status(vehicle_id) -> None:
    if not vehicle_id:
        return
    status = Vehicle.objects.filter(pk=vehicle_id).values_list('status', flat=True).first()
    if status is None:
        return
    busy = has_current_load(vehicle_id)
    if busy and status in TILES['free']:
        Vehicle.objects.filter(pk=vehicle_id).update(status='IN_USE')
    elif not busy and status in TILES['job']:
        Vehicle.objects.filter(pk=vehicle_id).update(status='AVAILABLE')
