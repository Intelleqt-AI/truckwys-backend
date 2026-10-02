"""POD verification tiers, duplicate detection and fraud checks (docs/capital-risk/03-design.md §5).

Verification tiers (``verification_tier``):

* V0  no load, or no POD file and no signature.
* V1  a POD exists but is not an in-app camera capture with full metadata
      (``pod_source != CAMERA`` or missing captured_at / lat / lng / sha256), or a
      camera capture more than 1 km from the load's delivery point (V2-FAR).
* V2  CAMERA + captured_at + lat/lng + sha256, within 1 km of the delivery
      point when the load has delivery coordinates (otherwise
      ``details['geofence'] = 'not_checked'``).
* V3  V2 plus an independent telematics stop at the consignee. TruckWys keeps
      no vehicle position history yet, so ``_telematics_stop_match`` returns
      None (no data) and **V3 is unreachable today**. When a position history
      exists, implement it there; nothing else changes.

Fraud checks (``fraud_checks``) return a 0-1 score from Phase 1 rules plus a
duplicate flag; duplicates (POD file reuse, or the same load / debtor +
amount + delivery date across the network) force the score to at least 0.9.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from core.capital.reasons import reason

D = Decimal
ZERO = D('0')
GEOFENCE_KM = D('1.0')
SA_TZ = ZoneInfo('Africa/Johannesburg')
EXCLUDED_STATUSES = ('DRAFT', 'CANCELLED')

W_SAME_AMOUNT = D('0.15')
W_ROUND = D('0.05')
W_NIGHT = D('0.10')
W_CAPACITY = D('0.10')
DUPLICATE_FLOOR = D('0.9')


# ---------------------------------------------------------------------------
# Verification tiers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lng1, lat2, lng2) -> Decimal:
    """Great-circle distance in km (to 0.001). Geometry needs trig, so floats are
    used here only; the result is returned as a Decimal. Not money."""
    r = 6371.0088
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lng2) - float(lng1))
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * r * math.asin(min(1.0, math.sqrt(h)))
    return D(repr(km)).quantize(D('0.001'), rounding=ROUND_HALF_UP)


def _telematics_stop_match(load) -> bool | None:
    """Was there a telematics stop at the consignee within +-2 h of the POD capture?

    None = no data. TruckWys stores only each vehicle's latest position
    (Vehicle.latitude/longitude), not a history, so this cannot be answered
    yet and V3 is unreachable. Implement against a position-history table.
    """
    return None


def _has_pod(load) -> tuple[bool, bool]:
    has_file = bool(getattr(load, 'pod_document', None))
    has_sig = bool((getattr(load, 'pod_signature', '') or '').strip())
    return has_file, has_sig


def verification_tier(load, policy=None) -> dict:
    """{tier: 'V0'..'V3', reasons: [reason dicts], details: {...}}."""
    if load is None:
        return {'tier': 'V0', 'reasons': [reason('E-POD-V0')], 'details': {'load': None}}
    has_file, has_sig = _has_pod(load)
    details = {
        'load': load.pk, 'has_file': has_file, 'has_signature': has_sig,
        'source': load.pod_source or '', 'captured_at': load.pod_captured_at.isoformat() if load.pod_captured_at else None,
        'has_location': load.pod_latitude is not None and load.pod_longitude is not None,
        'has_sha256': bool(load.pod_file_sha256), 'geofence': 'not_checked', 'distance_km': None,
        'telematics': 'no_data',
    }
    if not has_file and not has_sig:
        return {'tier': 'V0', 'reasons': [reason('E-POD-V0')], 'details': details}

    full_capture = (load.pod_source == 'CAMERA' and load.pod_captured_at is not None
                    and details['has_location'] and details['has_sha256'] and has_file)
    if not full_capture:
        missing = [label for ok, label in (
            (load.pod_source == 'CAMERA', 'camera capture'), (load.pod_captured_at is not None, 'capture time'),
            (details['has_location'], 'GPS location'), (details['has_sha256'], 'file hash'), (has_file, 'file'),
        ) if not ok]
        details['missing'] = missing
        return {'tier': 'V1', 'reasons': [reason('E-POD-V1')], 'details': details}

    if load.delivery_lat is not None and load.delivery_lng is not None:
        km = haversine_km(load.pod_latitude, load.pod_longitude, load.delivery_lat, load.delivery_lng)
        details['distance_km'] = str(km)
        if km > GEOFENCE_KM:
            details['geofence'] = 'outside'
            return {'tier': 'V1', 'details': details,
                    'reasons': [reason('V2-FAR', km=str(km.quantize(D('0.1'), rounding=ROUND_HALF_UP)))]}
        details['geofence'] = 'inside'

    match = _telematics_stop_match(load)
    if match is True:
        details['telematics'] = 'match'
        return {'tier': 'V3', 'reasons': [reason('V3')], 'details': details}
    if match is False:
        details['telematics'] = 'no_match'
    return {'tier': 'V2', 'reasons': [reason('V2')], 'details': details}


# ---------------------------------------------------------------------------
# Duplicates and fraud
# ---------------------------------------------------------------------------

def _network_issued():
    from core.models import Invoice
    return Invoice.objects.exclude(status__in=EXCLUDED_STATUSES)


def _same_debtor_q(invoice) -> Q:
    ident = getattr(invoice.customer, 'debtor_identity_id', None)
    if ident:
        return Q(customer__debtor_identity_id=ident)
    return Q(customer_id=invoice.customer_id)


def _within_pct(a: Decimal, b: Decimal, pct: Decimal = D('0.01')) -> bool:
    a, b = D(a or 0), D(b or 0)
    base = max(abs(a), abs(b))
    return abs(a - b) <= base * pct


def _delivery_day(load):
    if load is None:
        return None
    dt = load.actual_delivered_at or load.delivery_date
    if dt is None:
        return None
    return timezone.localtime(dt, SA_TZ).date() if timezone.is_aware(dt) else dt.date()


def _as_of_dt(as_of):
    if as_of is None:
        return timezone.now()
    if isinstance(as_of, datetime):
        return as_of if timezone.is_aware(as_of) else timezone.make_aware(as_of)
    return timezone.make_aware(datetime.combine(as_of, time.max))


def _route_mismatch(load) -> bool | None:
    """Telematics position vs planned route (F-ROUTE). No position history yet: None."""
    return None


def fraud_checks(invoice, policy=None, *, as_of=None) -> dict:
    """{score: Decimal 0-1, flags: [reason dicts], duplicate: bool, duplicate_detail: str}."""
    from core.models import Load, Vehicle

    flags = []
    score = ZERO
    duplicate = False
    details = []
    load = invoice.load if invoice.load_id else None
    total = D(invoice.total_amount or 0)

    # POD file reused on another load (any tenant)
    if load is not None and (load.pod_file_sha256 or '').strip():
        other = (Load.objects.filter(pod_file_sha256=load.pod_file_sha256).exclude(pk=load.pk)
                 .order_by('id').values_list('load_number', flat=True).first())
        if other:
            duplicate = True
            flags.append(reason('F-POD-REUSE', other=other))
            details.append(f'POD file also attached to load {other}')

    # Network duplicate: same load, or same debtor + amount within 1% + same delivery date
    others = _network_issued().exclude(pk=invoice.pk)
    dup_numbers = []
    if load is not None:
        dup_numbers += list(others.filter(load_id=load.pk).order_by('id').values_list('invoice_number', flat=True))
    day = _delivery_day(load)
    same_debtor = list(others.filter(_same_debtor_q(invoice))
                       .select_related('load').only('id', 'invoice_number', 'total_amount', 'issue_date', 'company_id',
                                                'load__vehicle_id', 'load__actual_delivered_at', 'load__delivery_date'))
    dup_ids = set()
    soft_same_day = []
    if day is not None:
        for o in same_debtor:
            if o.load_id and o.load_id != (load.pk if load else None) and _within_pct(o.total_amount, total) \
                    and _delivery_day(o.load) == day:
                # Design key: (debtor, amount, delivery date, vehicle). Another
                # transporter billing the same delivery, or the same truck twice,
                # is a duplicate. Two of this transporter's trucks on the same
                # lane and day is normal freight: a soft flag only.
                other_tenant = o.company_id != invoice.company_id
                same_vehicle = bool(load and load.vehicle_id and o.load.vehicle_id == load.vehicle_id)
                if other_tenant or same_vehicle:
                    dup_numbers.append(o.invoice_number)
                    dup_ids.add(o.pk)
                else:
                    soft_same_day.append(o.pk)
    if load is not None:
        dup_ids |= set(others.filter(load_id=load.pk).values_list('id', flat=True))
    if dup_numbers:
        duplicate = True
        seen = list(dict.fromkeys(dup_numbers))
        detail = 'invoice ' + ', '.join(seen) + ' for the same delivery'
        details.append(detail)
        flags.append(reason('E-DUPLICATE', detail=detail))

    # Same amount (+-1%) to the same debtor within 7 days, not already a duplicate
    same_amount = [o for o in same_debtor if o.pk not in dup_ids and (o.pk in soft_same_day or (
        _within_pct(o.total_amount, total) and abs((o.issue_date - invoice.issue_date).days) <= 7))]
    if same_amount:
        score += W_SAME_AMOUNT
        flags.append(reason('F-SAME-AMOUNT', n=len(same_amount)))

    # Round amount
    if total >= D('10000') and total % D('1000') == 0:
        score += W_ROUND
        flags.append(reason('F-ROUND'))

    # POD captured at night (22:00-04:59 SAST)
    if load is not None and load.pod_captured_at is not None:
        local = timezone.localtime(load.pod_captured_at, SA_TZ) if timezone.is_aware(load.pod_captured_at) \
            else load.pod_captured_at
        if local.hour >= 22 or local.hour < 5:
            score += W_NIGHT
            flags.append(reason('F-NIGHT-POD', hour=f'{local.hour:02d}'))

    # Physical capacity: loads delivered in 30 days vs 3 x vehicles x 30
    company = invoice.company
    if company is not None:
        vehicles = Vehicle.objects.filter(company=company).count()
        if vehicles:
            now = _as_of_dt(as_of)
            start = now - timedelta(days=30)
            delivered = (Load.objects.filter(company=company, status__in=('DELIVERED', 'INVOICED', 'COMPLETED'))
                         .filter(Q(actual_delivered_at__gt=start, actual_delivered_at__lte=now)
                                 | Q(actual_delivered_at__isnull=True, delivery_date__gt=start,
                                     delivery_date__lte=now))
                         .count())
            if delivered > 3 * vehicles * 30:
                score += W_CAPACITY
                flags.append(reason('F-CAPACITY'))

    # Telematics vs route: hook only (no position history yet)
    if _route_mismatch(load) is True:  # pragma: no cover - unreachable until telematics history exists
        flags.append(reason('F-ROUTE'))

    score = min(D('1'), score)
    if duplicate:
        score = max(score, DUPLICATE_FLOOR)
    return {'score': score.quantize(D('0.001')), 'flags': flags, 'duplicate': duplicate,
            'duplicate_detail': '; '.join(details)}
