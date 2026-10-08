"""Inbound fleet / TMS trip sync — the one place that turns a TMS record into
a Load.

Tenancy (security, 2026-10): every lookup, create and customer here is scoped
to the company of the API key that made the call (see
core.views_integrations.resolve_fleet_key). A key for company A can never
read, update or attach to company B's loads, vehicles, drivers or customers.
"""
import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

logger = logging.getLogger(__name__)

LOAD_STATUSES = ('PENDING', 'ASSIGNED', 'LOADING', 'IN_TRANSIT', 'DELIVERED', 'INVOICED', 'CANCELLED')


class SyncError(Exception):
    def __init__(self, message, http_status=400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def _dec(value, default=None):
    if value in (None, ''):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SyncError(f'Not a number: {value!r}')


def parse_dt(value):
    """Best-effort parse of an ISO datetime/date string; None on failure.
    A naive value is read as SAST (the app's time zone)."""
    if not value:
        return None
    from django.utils.dateparse import parse_date, parse_datetime
    try:
        dt = parse_datetime(str(value))
        if dt is None:
            d = parse_date(str(value))
            if d is None:
                return None
            dt = datetime(d.year, d.month, d.day)
    except (ValueError, TypeError):
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def company_customer(company, rec):
    """The customer for an inbound TMS booking, ALWAYS within `company`:
    customer_id (this company's), else customer_email (matched or created in
    this company), else this company's "External TMS import" placeholder."""
    from core.models import Customer
    cid = rec.get('customer_id')
    if cid not in (None, ''):
        cust = Customer.objects.filter(pk=cid, company=company).first()
        if cust is None:
            raise SyncError('Customer not found', 404)
        return cust
    email = (rec.get('customer_email') or '').strip().lower()
    name = (rec.get('customer_name') or '').strip()
    base = {'phone': '', 'address': '', 'city': '', 'state': '', 'zip_code': ''}
    if email:
        cust = Customer.objects.filter(company=company, email__iexact=email).first()
        if cust is None:
            cust = Customer.objects.create(company=company, email=email, name=name or email, **base)
        return cust
    placeholder = f'tms-import+{company.id}@truckwys.local'
    cust = Customer.objects.filter(company=company, email=placeholder).first()
    if cust is None:
        cust = Customer.objects.create(company=company, email=placeholder, name='External TMS Import', **base)
    return cust


def company_vehicle(company, plate):
    from core.models import Vehicle
    plate = (plate or '').strip()
    if not plate:
        return None
    norm = plate.replace(' ', '').upper()
    for v in Vehicle.objects.filter(company=company).only('id', 'plate', 'cartrack_registration'):
        for cand in (v.plate, getattr(v, 'cartrack_registration', None)):
            if cand and cand.replace(' ', '').upper() == norm:
                return v
    return None


def company_driver(company, driver_id):
    from core.models import Driver
    if driver_id in (None, ''):
        return None
    try:
        return Driver.objects.filter(pk=int(driver_id), company=company).first()
    except (TypeError, ValueError):
        return None


def find_load(company, *, load_number=None, load_id=None):
    """A load of THIS company by id or number (None when not found)."""
    from core.models import Load
    qs = Load.objects.filter(company=company)
    if load_id not in (None, ''):
        try:
            return qs.filter(pk=int(load_id)).first()
        except (TypeError, ValueError):
            return None
    if load_number:
        return qs.filter(load_number=load_number).first()
    return None


def new_load_number():
    from core.models import Load
    num = f'LOAD-{timezone.now().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
    while Load.objects.filter(load_number=num).exists():
        num = f'LOAD-{timezone.now().strftime("%Y%m%d")}-{random.randint(1000, 9999)}'
    return num


def apply_fleet_trip(company, data):
    """POST integrations/fleet/sync/ (and each bulk item). Returns
    (load, created). Raises SyncError."""
    from core.models import Load
    action = data.get('action', 'status_update')
    load_number = data.get('load_number')
    if not load_number:
        raise SyncError('load_number is required')
    load = find_load(company, load_number=load_number)
    created = False
    if load is None:
        if action != 'create':
            raise SyncError(f'Load {load_number} not found', 404)
        if Load.objects.filter(load_number=load_number).exists():
            # The number is taken by another company: never reveal or touch it.
            raise SyncError(f'Load number {load_number} is not available; use another', 409)
        now = timezone.now()
        pickup = data.get('pickup_location', '') or ''
        delivery = data.get('delivery_location', '') or ''
        load = Load.objects.create(
            company=company,
            load_number=load_number,
            customer=company_customer(company, data),
            pickup_location=pickup,
            pickup_city=data.get('pickup_city') or pickup[:100],
            pickup_state=data.get('pickup_state', ''),
            pickup_zip=data.get('pickup_zip', ''),
            pickup_date=parse_dt(data.get('pickup_date')) or now,
            delivery_location=delivery,
            delivery_city=data.get('delivery_city') or delivery[:100],
            delivery_state=data.get('delivery_state', ''),
            delivery_zip=data.get('delivery_zip', ''),
            delivery_date=parse_dt(data.get('delivery_date')) or now,
            cargo_description=data.get('cargo_description', 'Freight'),
            weight=_dec(data.get('weight'), Decimal('0')),
            distance=_dec(data.get('distance'), Decimal('0')),
            rate=_dec(data.get('rate'), Decimal('0')),
            total_amount=_dec(data.get('total_amount'), Decimal('0')),
            status='PENDING',
            notes=data.get('notes', ''),
        )
        created = True

    if action in ('status_update', 'create'):
        st = data.get('status')
        if st:
            st = str(st).upper()
            if st not in LOAD_STATUSES:
                raise SyncError(f'Unknown status {st}')
            load.status = st
        if data.get('driver_id'):
            driver = company_driver(company, data['driver_id'])
            if driver is not None:
                load.driver = driver
        if data.get('vehicle_plate'):
            vehicle = company_vehicle(company, data['vehicle_plate'])
            if vehicle is not None:
                load.vehicle = vehicle
        if data.get('notes'):
            load.notes = data['notes']
    elif action == 'complete':
        load.status = 'DELIVERED'
        if data.get('notes'):
            load.notes = data['notes']
    else:
        raise SyncError(f'Unknown action {action}')
    load.save()
    return load, created


def _create_load_from_record(company, rec, ext_id):
    from core.models import Load
    origin = rec.get('origin') or ''
    dest = rec.get('destination') or ''
    rate = _dec(rec.get('rate') if rec.get('rate') not in (None, '') else rec.get('amount'), Decimal('0'))
    return Load.objects.create(
        company=company,
        load_number=new_load_number(),
        customer=company_customer(company, rec),
        pickup_location=origin, pickup_city=origin[:100], pickup_state='', pickup_zip='',
        pickup_date=parse_dt(rec.get('pickup_date')) or timezone.now(),
        delivery_location=dest, delivery_city=dest[:100], delivery_state='', delivery_zip='',
        delivery_date=parse_dt(rec.get('delivery_date')) or (timezone.now() + timedelta(days=2)),
        cargo_description=rec.get('cargo_description', ''),
        weight=_dec(rec.get('weight'), Decimal('0')),
        distance=_dec(rec.get('distance'), Decimal('0')),
        rate=rate,
        total_amount=rate,
        status='PENDING',
        notes=f'ext_id:{ext_id}' if ext_id else 'imported via API',
    )


def sync_trip_record(company, rec):
    """POST integrations/trips/sync/ record. Returns ('created'|'skipped', load)."""
    from core.models import Load
    ext_id = rec.get('external_id')
    missing = [f for f in ('origin', 'destination') if not rec.get(f)]
    if missing:
        raise SyncError(f'Missing fields: {missing}')
    if ext_id:
        dup = Load.objects.filter(company=company, notes__icontains=f'ext_id:{ext_id}').first()
        if dup is not None:
            return 'skipped', dup
    return 'created', _create_load_from_record(company, rec, ext_id)
