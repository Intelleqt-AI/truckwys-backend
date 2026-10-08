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


def costing_inputs_from(company, rec):
    """compute() inputs a TMS record may carry: toll_cost (all loaded legs),
    toll_cost_one_way, tolls_confirmed_none, duration_minutes (one way),
    driver_cost, driver_nights, include_empty_return, border_cost and the
    truck: vehicle_type_id or vehicle_type (name), both only among the
    vehicle types this company can see."""
    from core.services.trip_costing import clean_costing_inputs
    ci = clean_costing_inputs(rec)
    vt_id = ci.pop('vehicle_type_id', None)
    raw_name = rec.get('vehicle_type')
    if raw_name is not None and not isinstance(raw_name, str):
        raise SyncError('vehicle_type must be a string (the vehicle type name)')
    name = (raw_name or '').strip()
    if vt_id or name:
        from core.services.quote_costing import resolve_vehicle
        vt, how = resolve_vehicle(company, vehicle_type_id=vt_id, name=name, suggest=False)
        if vt is not None:
            ci['vehicle_type_id'] = vt.id
    return ci


def trip_type_from(rec):
    t = str(rec.get('trip_type') or '').upper()
    return 'ROUND_TRIP' if t == 'ROUND_TRIP' else 'ONE_WAY'


MAX_EXTERNAL_ID = 100


def clean_external_id(value, field='external_id'):
    """A TMS id: a string or number, one line, at most 100 characters."""
    if value is None:
        return ''
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SyncError(f'{field} must be a string')
    import unicodedata
    ext = str(value).strip()
    if any(unicodedata.category(c) == 'Cc' for c in ext):
        # Tabs, line breaks, NUL (PostgreSQL refuses it) and other controls.
        raise SyncError(f'{field} must not contain control characters (tabs, line breaks, NUL)')
    if len(ext) > MAX_EXTERNAL_ID:
        raise SyncError(f'{field} is longer than {MAX_EXTERNAL_ID} characters')
    return ext


STATUS_RANK = {'PENDING': 0, 'ASSIGNED': 1, 'LOADING': 2, 'IN_TRANSIT': 3, 'DELIVERED': 4, 'INVOICED': 5}


def allowed_status_move(load, new):
    """(status to write | None, refusal | None). No moving back once a job
    is DELIVERED / INVOICED; INVOICED is set by TruckWys invoicing, not a TMS;
    CANCELLED after invoicing doesn't cancel the job: it flags the invoice."""
    cur = load.status
    if new == cur:
        return None, None
    if cur == 'CANCELLED':
        # Cancelled in TruckWys is final for a TMS (un-cancelling would raise
        # an invoice for a job nobody runs).
        return None, {'code': 'cancelled_in_truckwys',
                      'detail': 'The job is cancelled in TruckWys; status left as is.'}
    if cur == 'INVOICED' and new == 'DELIVERED':
        return None, None          # delivered and already invoiced: nothing to do
    if new == 'INVOICED':
        return None, {'code': 'invoiced_by_truckwys', 'detail': 'Invoicing sets INVOICED; status left as is.'}
    if new == 'CANCELLED':
        from core.models import Invoice
        if cur in ('DELIVERED', 'INVOICED') and Invoice.objects.filter(load=load).exclude(
                status='CANCELLED').exists():
            return None, {'code': 'cancelled_after_invoicing',
                          'detail': 'The job is invoiced; the invoice is flagged, the job stays as it is.'}
        return new, None
    if cur in ('DELIVERED', 'INVOICED') and STATUS_RANK.get(new, 99) < STATUS_RANK[cur]:
        return None, {'code': 'status_backwards', 'detail': f'{cur.title()} jobs never move back to {new.title()}.'}
    return new, None


def find_by_external_id(company, external_id):
    """A load of THIS company by its TMS id (the field, else the legacy
    'ext_id:' note written by trips/sync before external_id existed, which is
    then moved onto the field)."""
    from core.models import Load
    ext = str(external_id or '').strip()
    if not ext:
        return None
    load = Load.objects.filter(company=company, external_id=ext).first()
    if load is not None:
        return load
    # Legacy: the id only in an 'ext_id:' note line (a note may hold several:
    # the first was moved onto external_id, the others stay findable here).
    legacy = [l for l in Load.objects.filter(company=company, notes__icontains=f'ext_id:{ext}').order_by('pk')
              if any(line.strip() == f'ext_id:{ext}' for line in (l.notes or '').splitlines())]
    if legacy:
        load = legacy[0]
        if not load.external_id:
            Load.objects.filter(pk=load.pk).update(external_id=ext, external_source='tms_api')
            load.external_id, load.external_source = ext, 'tms_api'
        return load
    return None


# --- field mapping ----------------------------------------------------------

def _money(v):
    return None if v is None else _dec(v).quantize(Decimal('0.01'))


def _record_changes(rec, *, origin_keys=('pickup_location', 'origin'), dest_keys=('delivery_location', 'destination')):
    """{load_field: new value} for every field the record carries."""
    def first(*keys):
        for k in keys:
            if k in rec and rec[k] not in (None, ''):
                return rec[k]
        return None

    out = {}
    total = first('total_amount')
    rate = first('rate', 'amount')
    if total is not None:
        out['total_amount'] = _money(total)
        if rate is not None:
            out['rate'] = _money(rate)
    elif rate is not None:
        out['rate'] = out['total_amount'] = _money(rate)
    for key in ('distance', 'weight'):
        v = first(key)
        if v is not None:
            out[key] = _dec(v).quantize(Decimal('0.01'))
    pickup = first(*origin_keys)
    if pickup is not None:
        out['pickup_location'] = str(pickup)[:500]
        out['pickup_city'] = str(first('pickup_city') or pickup)[:100]
    elif first('pickup_city') is not None:
        out['pickup_city'] = str(first('pickup_city'))[:100]
    delivery = first(*dest_keys)
    if delivery is not None:
        out['delivery_location'] = str(delivery)[:500]
        out['delivery_city'] = str(first('delivery_city') or delivery)[:100]
    elif first('delivery_city') is not None:
        out['delivery_city'] = str(first('delivery_city'))[:100]
    for key in ('pickup_state', 'delivery_state', 'pickup_zip', 'delivery_zip'):
        v = first(key)
        if v is not None:
            out[key] = str(v)[:50 if key.endswith('state') else 20]
    for key in ('pickup_lat', 'pickup_lng', 'delivery_lat', 'delivery_lng'):
        v = first(key)
        if v is not None:
            out[key] = _dec(v).quantize(Decimal('0.0000001'))
    for key in ('pickup_date', 'delivery_date'):
        v = first(key)
        if v is not None:
            dt = parse_dt(v)
            if dt is None:
                raise SyncError(f'{key} is not a date')
            out[key] = dt
    if first('cargo_description') is not None:
        out['cargo_description'] = str(first('cargo_description'))
    if 'stops' in rec and isinstance(rec.get('stops'), list):
        out['stops'] = rec['stops']
    if isinstance(rec.get('route_geometry'), list) and rec['route_geometry']:
        out['route_geometry'] = rec['route_geometry'][:20000]
    if first('trip_type') is not None:
        out['trip_type'] = trip_type_from(rec)
    st = first('status')
    if st is not None:
        st = str(st).upper()
        if st not in LOAD_STATUSES:
            raise SyncError(f'Unknown status {st}')
        out['status'] = st
    return out


PRICING_FIELDS = {'distance', 'weight', 'trip_type', 'vehicle', 'costing_inputs', 'route_geometry', 'pickup_date',
                  'pickup_location', 'delivery_location', 'pickup_city', 'delivery_city', 'stops',
                  'pickup_lat', 'pickup_lng', 'delivery_lat', 'delivery_lng'}
AUDITED_FIELDS = ('total_amount', 'rate', 'distance', 'weight', 'pickup_location', 'pickup_city', 'pickup_date',
                  'delivery_location', 'delivery_city', 'delivery_date', 'status', 'vehicle', 'driver', 'stops',
                  'trip_type', 'cargo_description', 'costing_inputs', 'notes')


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    if hasattr(v, 'pk'):
        return v.pk
    return v


def apply_record(company, load, rec, *, source, user=None, origin_keys=None, dest_keys=None):
    """Update an existing load from a TMS record: rate / total, distance,
    weight, dates, locations, status, vehicle, driver, stops, trip type and
    costing inputs. Audited (ActivityEvent with old -> new per field),
    re-costed when a pricing input changed. Issued or draft invoices are
    NEVER changed: a new total that differs from the invoice is flagged on
    the load (invoice_mismatch). Returns the {field: [old, new]} changes."""
    kwargs = {}
    if origin_keys:
        kwargs['origin_keys'] = origin_keys
    if dest_keys:
        kwargs['dest_keys'] = dest_keys
    new = _record_changes(rec, **kwargs)
    if rec.get('vehicle_plate'):
        vehicle = company_vehicle(company, rec['vehicle_plate'])
        if vehicle is not None:
            new['vehicle'] = vehicle
    if rec.get('driver_id'):
        driver = company_driver(company, rec['driver_id'])
        if driver is not None:
            new['driver'] = driver
    if rec.get('notes'):
        new['notes'] = rec['notes']
    # Costing inputs are merged into the CURRENT row under a lock (only the
    # keys this record sent): a stale read never wipes what routing wrote.
    ci_new = costing_inputs_from(company, rec)
    from core.models import Load as _Load
    current_ci = _Load.objects.filter(pk=load.pk).values_list('costing_inputs', flat=True).first() or {}
    ci_changed = {k: v for k, v in ci_new.items() if current_ci.get(k) != v}

    refused = None
    if 'status' in new:
        st, refused = allowed_status_move(load, new['status'])
        if st is None:
            new.pop('status')
        if refused and refused['code'] == 'cancelled_after_invoicing':
            flag_cancelled_after_invoicing(load, source=source)
    load._status_refused = refused
    changes = {}
    for field, value in new.items():
        old = getattr(load, field)
        same = (old == value) if not hasattr(value, 'pk') else (getattr(old, 'pk', None) == value.pk)
        if not same:
            changes[field] = [_jsonable(old), _jsonable(value)]
            setattr(load, field, value)
    if ci_changed:
        from core.services.tms_routing import merge_costing_inputs
        merge_costing_inputs(load, ci_changed)
        changes['costing_inputs'] = [{k: current_ci.get(k) for k in ci_changed}, ci_changed]
    if not changes:
        return {}
    load._notify_actor_id = None
    fields = [f for f in changes if f != 'costing_inputs']
    if fields:
        load.save(update_fields=fields + ['updated_at'])

    from core.models import ActivityEvent
    ActivityEvent.objects.create(
        event_type='load', title=f'{source.upper()} update: {load.load_number}'[:200],
        description=', '.join(sorted(changes)), entity_id=load.pk, entity_type='Load', company=company,
        metadata={'source': source, 'external_id': load.external_id or None,
                  'changes': {k: changes[k] for k in AUDITED_FIELDS if k in changes}})
    recost = PRICING_FIELDS if load.costing_source != 'quote' else PRICING_FIELDS - {'vehicle'}
    if recost & set(changes) or rec.get('return_route_geometry') or rec.get('countries'):
        # A quote-costed job keeps its as-quoted figures (quoted_*); its
        # estimate moves to the new data (costing_source becomes computed).
        # Assigning a truck alone doesn't re-cost a quoted job (it was
        # priced on that truck type already).
        from core.services.trip_costing import cost_load
        cost_load(load, rec=rec)
        from core.services.tms_routing import queue_routing
        queue_routing(load)
    if 'total_amount' in changes:
        check_invoice_mismatch(load, source=source)
    return changes


def flag_cancelled_after_invoicing(load, *, source='tms'):
    from core.models import ActivityEvent, Invoice, Load
    inv = Invoice.objects.filter(load=load).exclude(status='CANCELLED').order_by('-id').first()
    flag = {'code': 'cancelled_after_invoicing', 'invoice_id': getattr(inv, 'pk', None),
            'invoice_number': getattr(inv, 'invoice_number', None), 'invoice_status': getattr(inv, 'status', None),
            'source': source, 'detected_at': timezone.now().isoformat(),
            'title': 'TMS cancelled an invoiced job',
            'detail': 'Check the invoice: void it or issue a credit note if the job really was cancelled.'}
    Load.objects.filter(pk=load.pk).update(invoice_mismatch=flag)
    load.invoice_mismatch = flag
    ActivityEvent.objects.create(event_type='load', title=f'TMS cancelled invoiced job {load.load_number}',
                                 entity_id=load.pk, entity_type='Load', company=load.company, metadata=flag)
    return flag


def check_invoice_mismatch(load, *, source='tms'):
    """Flag (never fix) a load whose invoice no longer matches its total."""
    from core.models import ActivityEvent, Invoice, Load
    inv = (Invoice.objects.filter(load=load).exclude(status__in=('VOID', 'CANCELLED'))
           .order_by('-id').first())
    if inv is None:
        if load.invoice_mismatch:
            Load.objects.filter(pk=load.pk).update(invoice_mismatch={})
            load.invoice_mismatch = {}
        return None
    if inv.status in Invoice.ISSUED_STATUSES:
        from core.services.report_figures import invoice_revenue_excl_vat
        invoiced = invoice_revenue_excl_vat(inv)       # net of credit notes
    else:
        invoiced = (inv.total_amount or Decimal('0')) - (inv.vat_amount or Decimal('0'))
    total = load.total_amount or Decimal('0')
    if abs(invoiced - total) < Decimal('0.01'):
        if load.invoice_mismatch:
            Load.objects.filter(pk=load.pk).update(invoice_mismatch={})
            load.invoice_mismatch = {}
        return None
    flag = {
        'code': 'invoice_differs_from_rate', 'invoice_id': inv.pk, 'invoice_number': inv.invoice_number,
        'invoice_status': inv.status, 'invoice_excl_vat': float(invoiced), 'load_total_excl_vat': float(total),
        'difference': float(total - invoiced), 'source': source, 'detected_at': timezone.now().isoformat(),
        'title': 'Invoice differs from the updated rate',
        'detail': 'The TMS changed the rate after invoicing; issue a credit note or a new invoice.',
    }
    Load.objects.filter(pk=load.pk).update(invoice_mismatch=flag)
    load.invoice_mismatch = flag
    ActivityEvent.objects.create(event_type='load', title=f'Invoice differs from updated rate: {load.load_number}',
                                 entity_id=load.pk, entity_type='Load', company=load.company, metadata=flag)
    return flag


# --- return links by external id -------------------------------------------

def link_from_record(company, load, rec, *, source='tms'):
    """`return_of_external_id` / `return_of_load_number` on a record links
    this load as the return of that outbound (same company only). An
    outbound not synced yet is remembered and linked when it arrives.
    `return_of_external_id: null` (explicitly) unlinks. Returns
    {linked, pending, warnings, error}."""
    from core.models import Load
    from core.services.return_loads import LinkError, link_return, unlink_return
    keys = ('return_of_external_id', 'return_of_load_number')
    if not any(k in rec for k in keys):
        return None
    ext = clean_external_id(rec.get('return_of_external_id'), 'return_of_external_id')
    number = clean_external_id(rec.get('return_of_load_number'), 'return_of_load_number')
    if not ext and not number:
        if load.return_of_id:
            unlink_return(load, source=source)
        Load.objects.filter(pk=load.pk).update(return_of_external_ref='')
        return {'linked': False, 'pending': False, 'unlinked': True}
    outbound = find_by_external_id(company, ext) if ext else find_load(company, load_number=number)
    if outbound is None:
        ref = ext or f'number:{number}'          # <= 107 characters; the field holds 120
        Load.objects.filter(pk=load.pk).update(return_of_external_ref=ref)
        return {'linked': False, 'pending': True, 'waiting_for': ext or number}
    try:
        if load.return_of_id and load.return_of_id != outbound.pk:
            unlink_return(load, source=source)
            load.refresh_from_db()
        warnings = link_return(outbound, load, source='tms')
    except LinkError as e:
        return {'linked': False, 'pending': False, 'error': e.code, 'detail': e.message}
    Load.objects.filter(pk=load.pk).update(return_of_external_ref='')
    return {'linked': True, 'pending': False, 'outbound_id': outbound.pk, 'warnings': warnings}


def resolve_waiting_returns(company, outbound):
    """Link returns that named this load before it was synced."""
    from core.models import Load
    from core.services.return_loads import LinkError, link_return
    refs = [r for r in (outbound.external_id, f'number:{outbound.load_number}') if r]
    for ret in Load.objects.filter(company=company, return_of_external_ref__in=refs).exclude(pk=outbound.pk):
        try:
            link_return(outbound, ret, source='tms')
            Load.objects.filter(pk=ret.pk).update(return_of_external_ref='')
        except LinkError:
            continue


# --- endpoints ---------------------------------------------------------------

def _create_load(company, rec, *, load_number, external_id, source, origin_keys, dest_keys, default_days=0):
    from core.models import Load
    fields = _record_changes(rec, origin_keys=origin_keys, dest_keys=dest_keys)
    now = timezone.now()
    fields.setdefault('pickup_location', '')
    fields.setdefault('pickup_city', fields['pickup_location'][:100])
    fields.setdefault('delivery_location', '')
    fields.setdefault('delivery_city', fields['delivery_location'][:100])
    fields.setdefault('pickup_date', now)
    fields.setdefault('delivery_date', now + timedelta(days=default_days))
    fields.setdefault('cargo_description', 'Freight' if source == 'fleet_sync' else '')
    fields.setdefault('weight', Decimal('0'))
    fields.setdefault('distance', Decimal('0'))
    fields.setdefault('total_amount', Decimal('0'))
    fields.setdefault('rate', fields['total_amount'])
    fields['status'] = fields.get('status') if source == 'fleet_sync' and fields.get('status') else 'PENDING'
    for key in ('pickup_state', 'delivery_state', 'pickup_zip', 'delivery_zip'):
        fields.setdefault(key, '')
    load = Load.objects.create(
        company=company, load_number=load_number, customer=company_customer(company, rec),
        external_id=external_id or '', external_source=str(rec.get('source') or source)[:50] if external_id else '',
        notes=rec.get('notes') or ('' if source == 'fleet_sync' else 'imported via API'),
        costing_inputs=costing_inputs_from(company, rec),
        vehicle=company_vehicle(company, rec.get('vehicle_plate')),
        driver=company_driver(company, rec.get('driver_id')),
        **fields)
    from core.services.trip_costing import cost_load
    cost_load(load, rec=rec)
    # No road line and no toll figure: work the tolls out with TomTom after
    # commit (never in this request); the job shows "Working out tolls…".
    from core.services.tms_routing import queue_routing
    queue_routing(load)
    return load


FLEET_ORIGIN = ('pickup_location', 'origin')
FLEET_DEST = ('delivery_location', 'destination')
TRIP_ORIGIN = ('origin', 'pickup_location')
TRIP_DEST = ('destination', 'delivery_location')


def apply_fleet_trip(company, data, *, user=None):
    """POST integrations/fleet/sync/ (and each bulk item). Returns
    (load, created). Raises SyncError. Finds the load by external_id (when
    sent) else load_number, both within the key's company; 'create' makes it
    when missing, any action updates the fields the record carries."""
    from core.models import Load
    action = data.get('action', 'status_update')
    if action not in ('status_update', 'create', 'complete', 'update'):
        raise SyncError(f'Unknown action {action}')
    load_number = clean_external_id(data.get('load_number'), 'load_number') or None
    ext = clean_external_id(data.get('external_id'))
    if not load_number and not ext:
        raise SyncError('load_number or external_id is required')
    load = find_by_external_id(company, ext) if ext else None
    by_number = find_load(company, load_number=load_number) if load_number else None
    if load is not None and by_number is not None and load.pk != by_number.pk:
        raise SyncError(f'external_id {ext} belongs to another load than {load_number}', 409)
    if load is None and by_number is not None and ext and by_number.external_id \
            and by_number.external_id != ext:
        raise SyncError(f'Load {load_number} already has external_id {by_number.external_id}', 409)
    if load is None:
        load = by_number
    created = False
    if load is None:
        if action != 'create':
            raise SyncError(f'Load {load_number or ext} not found', 404)
        if load_number and Load.objects.filter(load_number=load_number).exists():
            # The number is taken by another company: never reveal or touch it.
            raise SyncError(f'Load number {load_number} is not available; use another', 409)
        load = _create_load(company, data, load_number=load_number or new_load_number(), external_id=ext,
                            source='fleet_sync', origin_keys=FLEET_ORIGIN, dest_keys=FLEET_DEST)
        created = True
        changes = {}
    else:
        if ext and not load.external_id:
            Load.objects.filter(pk=load.pk).update(external_id=ext, external_source='fleet_sync')
            load.external_id = ext
        rec = dict(data)
        if action == 'complete' and load.status != 'INVOICED':
            rec['status'] = 'DELIVERED'
        changes = apply_record(company, load, rec, source='fleet_sync', user=user,
                               origin_keys=FLEET_ORIGIN, dest_keys=FLEET_DEST)
    link = link_from_record(company, load, data)
    if load.external_id or load.load_number:
        resolve_waiting_returns(company, load)
    load._sync_changes = changes
    load._sync_link = link
    load.refresh_from_db()
    return load, created


def sync_trip_record(company, rec):
    """POST integrations/trips/sync/ record: upsert by external_id. Returns
    (outcome, load, detail) with outcome 'created' | 'updated' | 'unchanged'."""
    ext = clean_external_id(rec.get('external_id'))
    if not ext:
        # Without an id a re-sent record would create a duplicate job.
        raise SyncError('external_id is required')
    load = find_by_external_id(company, ext)
    if load is None:
        missing = [f for f in ('origin', 'destination') if not rec.get(f)]
        if missing:
            raise SyncError(f'Missing fields: {missing}')
        load = _create_load(company, rec, load_number=new_load_number(), external_id=ext, source='trips_sync',
                            origin_keys=TRIP_ORIGIN, dest_keys=TRIP_DEST, default_days=2)
        outcome, changes = 'created', {}
    else:
        changes = apply_record(company, load, rec, source='trips_sync', origin_keys=TRIP_ORIGIN, dest_keys=TRIP_DEST)
        outcome = 'updated' if changes else 'unchanged'
    link = link_from_record(company, load, rec)
    resolve_waiting_returns(company, load)
    load.refresh_from_db()
    detail = {'load_id': load.pk, 'external_id': load.external_id or None, 'outcome': outcome,
              'changed': sorted(changes)}
    if link is not None:
        detail['return_link'] = link
    if load.invoice_mismatch:
        detail['invoice_mismatch'] = load.invoice_mismatch
    refused = getattr(load, '_status_refused', None)
    if refused:
        detail['status_refused'] = refused
    return outcome, load, detail
