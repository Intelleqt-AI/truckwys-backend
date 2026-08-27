"""
CtrlFleet vehicle-roster sync.

CtrlFleet's External API identifies vehicles by their own `vehicleCode`, not
ours. This matches each vehicle CtrlFleet reports (by licence plate) to this
company's existing Vehicle records, so later position lookups can use
CtrlFleet's code directly instead of requiring a manual per-vehicle pairing
step from the customer.
"""
from typing import Any, Dict, List, Optional

from django.utils import timezone

from core.integrations.ctrlfleet import CtrlFleetClient


def _normalize_plate(value: Optional[str]) -> str:
    return (value or '').strip().upper().replace(' ', '')


def sync_ctrlfleet_vehicles(company, vehicles: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Match CtrlFleet's vehicle roster to this company's Vehicle records by
    licence plate, stamping CtrlFleet's vehicleCode onto each match.

    Pass `vehicles` to reuse a list already fetched (e.g. during Connect, where
    the same call also validates the key) instead of fetching it again.

    Returns {'matched': int, 'unmatched': [licenceNumber, ...], 'total': int}.
    """
    from core.models import Vehicle

    if vehicles is None:
        vehicles = CtrlFleetClient.for_company(company).list_vehicles()

    local_by_plate = {
        _normalize_plate(v.plate): v
        for v in Vehicle.objects.filter(company=company)
    }

    matched = 0
    unmatched = []
    for cf_vehicle in vehicles:
        licence_number = cf_vehicle.get('licenceNumber')
        vehicle_code = cf_vehicle.get('vehicleCode')
        if not licence_number or not vehicle_code:
            continue

        local_vehicle = local_by_plate.get(_normalize_plate(licence_number))
        if not local_vehicle:
            unmatched.append(licence_number)
            continue

        matched += 1
        if local_vehicle.ctrlfleet_vehicle_code != vehicle_code:
            local_vehicle.ctrlfleet_vehicle_code = vehicle_code
            local_vehicle.save(update_fields=['ctrlfleet_vehicle_code'])

    company.ctrlfleet_last_vehicle_sync = timezone.now()
    company.save(update_fields=['ctrlfleet_last_vehicle_sync'])

    return {'matched': matched, 'unmatched': unmatched, 'total': len(vehicles)}


def sync_ctrlfleet_positions(company, vehicle_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Poll CtrlFleet for the latest position of vehicles already linked (via
    ctrlfleet_vehicle_code, whether by automatic plate match or a manual link)
    to this company, and write it onto the same Vehicle.latitude/longitude/
    heading/speed_kmh/last_location_at fields Cartrack's own poll_vehicle_status
    populates — so the fleet list/map already displays this with no separate
    UI needed.

    Pass `vehicle_ids` to sync just those vehicles (e.g. the one truck on an
    order) instead of every linked vehicle in the company — cheaper and more
    relevant when only one truck's position is actually wanted right now.

    Returns {'checked': int, 'updated': int}.
    """
    from django.utils.dateparse import parse_datetime
    from core.models import Vehicle

    linked_vehicles_qs = (
        Vehicle.objects.filter(company=company)
        .exclude(ctrlfleet_vehicle_code__isnull=True)
        .exclude(ctrlfleet_vehicle_code='')
    )
    if vehicle_ids:
        linked_vehicles_qs = linked_vehicles_qs.filter(id__in=vehicle_ids)
    linked_vehicles = list(linked_vehicles_qs)
    if not linked_vehicles:
        return {'checked': 0, 'updated': 0}

    codes = [v.ctrlfleet_vehicle_code for v in linked_vehicles]
    positions = CtrlFleetClient.for_company(company).get_vehicle_positions(vehicle_codes=codes)
    by_code = {p.get('vehicleCode'): p for p in positions if p.get('vehicleCode')}

    updated = 0
    for vehicle in linked_vehicles:
        pos = by_code.get(vehicle.ctrlfleet_vehicle_code)
        if not pos:
            continue
        lat, lng = pos.get('latitude'), pos.get('longitude')
        if lat is None or lng is None:
            continue

        vehicle.latitude = lat
        vehicle.longitude = lng
        vehicle.heading = pos.get('direction')
        vehicle.speed_kmh = pos.get('speed')
        timestamp = pos.get('timestamp')
        vehicle.last_location_at = (parse_datetime(timestamp) if timestamp else None) or timezone.now()
        vehicle.save(update_fields=['latitude', 'longitude', 'heading', 'speed_kmh', 'last_location_at'])
        updated += 1

    return {'checked': len(linked_vehicles), 'updated': updated}
