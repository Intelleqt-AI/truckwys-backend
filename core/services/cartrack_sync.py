"""Sync live telematics data from Cartrack into TruckWys models."""
import logging
from datetime import timedelta

from django.utils import timezone

from core.integrations.cartrack import CartrackClient

logger = logging.getLogger(__name__)

# poll_door_events runs every ~2 minutes; look back further than that so a
# slow/late poll never leaves a gap, then rely on last_door_event_at to skip
# events already applied.
DOOR_EVENT_LOOKBACK = timedelta(minutes=5)


def _vehicles_by_cartrack_registration(company):
    """Map upper-cased Cartrack registration -> Vehicle, for one company.

    Uses Vehicle.cartrack_registration when set, falling back to plate.
    """
    from core.models import Vehicle

    vehicles_by_reg = {}
    for vehicle in Vehicle.objects.filter(company=company):
        key = (vehicle.cartrack_registration or vehicle.plate or '').strip().upper()
        if key:
            vehicles_by_reg[key] = vehicle
    return vehicles_by_reg


def poll_vehicle_status(company) -> dict:
    """Fetch GET /vehicles/status for one company and update matching Vehicle rows
    with location, temperature and reported-driver fields.

    Matches Cartrack's `registration` against Vehicle.cartrack_registration,
    falling back to Vehicle.plate when that override isn't set. Returns a
    summary dict of how many vehicles were matched/unmatched, for logging
    and tests.
    """
    client = CartrackClient.for_company(company)
    statuses = client.get_vehicle_status()
    vehicles_by_reg = _vehicles_by_cartrack_registration(company)

    now = timezone.now()
    matched = 0
    unmatched = []
    for entry in statuses:
        registration = (entry.get('registration') or '').strip().upper()
        vehicle = vehicles_by_reg.get(registration)
        if not vehicle:
            if registration:
                unmatched.append(registration)
            continue

        vehicle.latitude = entry.get('latitude')
        vehicle.longitude = entry.get('longitude')
        vehicle.heading = entry.get('heading')
        vehicle.speed_kmh = entry.get('speed')
        vehicle.ignition_on = entry.get('ignition')
        vehicle.last_location_at = now
        vehicle.temp1 = entry.get('temp1')
        vehicle.temp2 = entry.get('temp2')
        vehicle.temp3 = entry.get('temp3')
        vehicle.temp4 = entry.get('temp4')
        # Informational only — never touches the authoritative `driver` FK,
        # which stays dispatcher-controlled (see Vehicle.cartrack_current_driver_ref).
        vehicle.cartrack_current_driver_ref = (
            entry.get('driver_id') or entry.get('driver_tag') or entry.get('driver') or ''
        )
        vehicle.save(update_fields=[
            'latitude', 'longitude', 'heading', 'speed_kmh', 'ignition_on', 'last_location_at',
            'temp1', 'temp2', 'temp3', 'temp4', 'cartrack_current_driver_ref',
        ])
        matched += 1

    company.cartrack_last_status_sync = now
    company.save(update_fields=['cartrack_last_status_sync'])

    if unmatched:
        logger.warning(
            'Cartrack poll for company %s: %d vehicle(s) in the Cartrack response had no '
            'matching TruckWys vehicle (checked cartrack_registration/plate): %s',
            company.id, len(unmatched), unmatched,
        )

    return {'matched': matched, 'unmatched': unmatched}


def poll_door_events(company) -> dict:
    """Fetch GET /topics/vehicles/door for one company and apply door open/close
    events to matching Vehicle rows, logging an ActivityEvent per event applied.

    Idempotent against re-processing the same event across overlapping poll
    windows: an event is only applied if its timestamp is newer than the
    vehicle's current last_door_event_at.

    Requires the account's DOOR topic to be granted by Cartrack — a 403
    propagates as CartrackAPIError, which callers should treat as
    "not enabled for this account" rather than a code bug.
    """
    from core.models import ActivityEvent
    from dateutil import parser as date_parser

    client = CartrackClient.for_company(company)
    now = timezone.now()
    events = client.get_door_events(now - DOOR_EVENT_LOOKBACK, now)
    vehicles_by_reg = _vehicles_by_cartrack_registration(company)

    applied = 0
    for entry in events:
        registration = (entry.get('registration') or '').strip().upper()
        vehicle = vehicles_by_reg.get(registration)
        if not vehicle:
            continue

        raw_ts = entry.get('event_ts') or entry.get('timestamp')
        if not raw_ts:
            continue
        event_at = date_parser.parse(raw_ts)
        if vehicle.last_door_event_at and event_at <= vehicle.last_door_event_at:
            continue  # already applied in a previous, overlapping poll

        is_open = entry.get('is_open')
        if is_open is None:
            is_open = str(entry.get('event_type', '')).upper() == 'DOOR_OPEN'

        vehicle.door_open = bool(is_open)
        vehicle.last_door_event_at = event_at
        vehicle.save(update_fields=['door_open', 'last_door_event_at'])

        ActivityEvent.objects.create(
            event_type='system',
            title=f"Door {'opened' if is_open else 'closed'}: {vehicle.plate}",
            description=f'Cartrack reported a door {"open" if is_open else "close"} event.',
            entity_id=vehicle.id,
            entity_type='vehicle',
            company=company,
            metadata=entry,
        )
        applied += 1

    return {'applied': applied}
