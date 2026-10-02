"""Facility capacity ledger: the only code that moves Facility.outstanding/reserved.

Capacity model (capital-safety 2026-10, audit §6 #3):

    available = limit - outstanding - reserved

* REQUEST  -> the advance's amount is added to ``reserved`` (and recorded on
  ``AdvanceRequest.capacity_reserved``). Refused if it would over-commit.
* APPROVE  -> reservation kept. A legacy advance holding no reservation
  reserves now, so an approval can never promise money the facility lacks.
* DISBURSE -> the reservation moves to ``outstanding``.
* DENY / CANCEL -> the reservation is released.
* SETTLE   -> the amount leaves ``outstanding``.

Why it is written this way:
* Every change runs in ``transaction.atomic`` with ``select_for_update`` on the
  facility row and the advance row, so two requests cannot interleave a
  check-then-write on the same facility or double-transition one advance.
* Every capacity change is a single conditional ``UPDATE ... SET x = x + n
  WHERE limit >= outstanding + reserved + n`` (F() expressions), never a
  read-modify-write of a Python value. If the guard fails, zero rows update and
  we raise ``CapacityError``; nothing is written.
* The Facility CheckConstraints are the backstop if anything bypasses this.

Advance status changes are saved on the caller's own instance (not a re-read
copy) so view-set attributes the post_save signal reads (``_notify_handled``)
survive.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ('REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED')
UNDISBURSED_STATUSES = ('REQUESTED', 'SCORING', 'APPROVED')


class CapacityError(ValueError):
    """The facility cannot absorb this amount (or is not active)."""


def _amount(value) -> Decimal:
    amt = Decimal(str(value)).quantize(Decimal('0.01'))
    if amt <= 0:
        raise CapacityError('Amount must be positive')
    return amt


def _facility_model():
    from core.models import Facility
    return Facility


def _refresh_facility(obj) -> None:
    """Pull the new ledger figures into an in-memory Facility so a later
    .save() on it cannot write stale outstanding/reserved back."""
    if obj is not None and getattr(obj, 'pk', None):
        obj.refresh_from_db(fields=['outstanding', 'reserved', 'limit', 'status'])


# ---------------------------------------------------------------------------
# Primitive conditional updates (caller must hold the facility row lock)
# ---------------------------------------------------------------------------

def _add_reserved(facility_id: int, amt: Decimal) -> None:
    Facility = _facility_model()
    updated = Facility.objects.filter(
        pk=facility_id,
        status='ACTIVE',
        limit__gte=F('outstanding') + F('reserved') + amt,
    ).update(reserved=F('reserved') + amt, updated_at=timezone.now())
    if not updated:
        raise CapacityError(_capacity_message(facility_id, amt))


def _release_reserved(facility_id: int, amt: Decimal) -> None:
    Facility = _facility_model()
    updated = Facility.objects.filter(pk=facility_id, reserved__gte=amt).update(
        reserved=F('reserved') - amt, updated_at=timezone.now())
    if not updated:
        # Releasing more than is held means the ledger is already wrong; refuse
        # rather than clamp so the discrepancy surfaces.
        raise CapacityError(f'Facility {facility_id}: cannot release R{amt} of reservation')


def _move_reserved_to_outstanding(facility_id: int, held: Decimal, amt: Decimal) -> None:
    """reserved -= held, outstanding += amt, atomically, within the limit."""
    Facility = _facility_model()
    # A suspended/closed facility pays nothing out, reservation or not.
    qs = Facility.objects.filter(pk=facility_id, status='ACTIVE', reserved__gte=held)
    if amt > held:
        # Paying out more than was reserved (e.g. a legacy advance holding no
        # reservation) needs fresh headroom.
        qs = qs.filter(limit__gte=F('outstanding') + F('reserved') - held + amt)
    updated = qs.update(
        reserved=F('reserved') - held,
        outstanding=F('outstanding') + amt,
        updated_at=timezone.now(),
    )
    if not updated:
        raise CapacityError(_capacity_message(facility_id, amt - held))


def _release_outstanding(facility_id: int, amt: Decimal) -> None:
    Facility = _facility_model()
    updated = Facility.objects.filter(pk=facility_id, outstanding__gte=amt).update(
        outstanding=F('outstanding') - amt, updated_at=timezone.now())
    if not updated:
        raise CapacityError(f'Facility {facility_id}: cannot release R{amt} from outstanding')


def _capacity_message(facility_id: int, amt: Decimal) -> str:
    Facility = _facility_model()
    f = Facility.objects.filter(pk=facility_id).only(
        'limit', 'outstanding', 'reserved', 'status').first()
    if f is None:
        return 'Facility not found'
    if f.status != 'ACTIVE':
        return f'Facility is {f.get_status_display()}'
    return f'Insufficient available capacity (R{f.available} available, R{amt} needed)'


def _lock_facility(facility_id: int):
    return _facility_model().objects.select_for_update().get(pk=facility_id)


def _lock_advance(advance):
    from core.models import AdvanceRequest
    return AdvanceRequest.objects.select_for_update().get(pk=advance.pk)


# ---------------------------------------------------------------------------
# Facility-level helpers (used by Facility.reserve_amount/release_amount)
# ---------------------------------------------------------------------------

def add_outstanding(facility, amount) -> None:
    amt = _amount(amount)
    with transaction.atomic():
        _lock_facility(facility.pk)
        _move_reserved_to_outstanding(facility.pk, Decimal('0.00'), amt)
    _refresh_facility(facility)


def release_outstanding(facility, amount) -> None:
    amt = _amount(amount)
    with transaction.atomic():
        _lock_facility(facility.pk)
        _release_outstanding(facility.pk, amt)
    _refresh_facility(facility)


def reserve_capacity(facility, amount) -> None:
    """Reserve capacity on a facility (no advance bookkeeping)."""
    amt = _amount(amount)
    with transaction.atomic():
        _lock_facility(facility.pk)
        _add_reserved(facility.pk, amt)
    _refresh_facility(facility)


# ---------------------------------------------------------------------------
# Advance lifecycle
# ---------------------------------------------------------------------------

def _set_capacity_reserved(advance, value: Decimal) -> None:
    from core.models import AdvanceRequest
    AdvanceRequest.objects.filter(pk=advance.pk).update(capacity_reserved=value)
    advance.capacity_reserved = value


def _reserve_for(advance) -> None:
    """Make the advance hold a reservation for its full amount (lock held)."""
    held = advance.capacity_reserved or Decimal('0.00')
    need = _amount(advance.amount) - held
    if need > 0:
        _add_reserved(advance.facility_id, need)
        _set_capacity_reserved(advance, held + need)


def _release_for(advance) -> None:
    held = advance.capacity_reserved or Decimal('0.00')
    if held > 0:
        _release_reserved(advance.facility_id, held)
        _set_capacity_reserved(advance, Decimal('0.00'))


def open_advance(*, invoice, facility, amount, **fields):
    """Create a REQUESTED advance and reserve its capacity in one transaction.

    Returns ``(advance, created)``. If the invoice already has an active
    advance (pre-check, or the uniq_active_advance_per_invoice constraint
    firing under a race) that advance is returned with ``created=False``.
    Raises CapacityError when the facility cannot absorb the amount.
    """
    from core.models import AdvanceRequest

    amt = _amount(amount)
    try:
        with transaction.atomic():
            _lock_facility(facility.pk)
            existing = AdvanceRequest.objects.filter(
                invoice=invoice, status__in=ACTIVE_STATUSES).first()
            if existing:
                return existing, False
            fields.setdefault('requested_at', timezone.now())
            advance = AdvanceRequest.objects.create(
                invoice=invoice, facility=facility, amount=amt, status='REQUESTED', **fields)
            _reserve_for(advance)
    except (IntegrityError, ValidationError):
        # IntegrityError: a concurrent insert won the race and the partial
        # unique index fired. ValidationError: full_clean's constraint check
        # saw it first. Either way hand back the winner, not a 500.
        existing = AdvanceRequest.objects.filter(
            invoice=invoice, status__in=ACTIVE_STATUSES).first()
        if existing is None:
            raise
        return existing, False
    _refresh_facility(facility)
    return advance, True


def request_advance(advance) -> None:
    """ELIGIBLE -> REQUESTED, reserving capacity."""
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status != 'ELIGIBLE':
            raise ValueError(f"Cannot request advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _reserve_for(advance)
        advance.status = 'REQUESTED'
        advance.requested_at = timezone.now()
        advance.save()
    _refresh_facility(advance.facility)


def approve_advance(advance) -> None:
    """REQUESTED/SCORING -> APPROVED; keeps (or, for legacy rows, takes) a reservation."""
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status not in ('SCORING', 'REQUESTED'):
            raise ValueError(f"Cannot approve advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _reserve_for(advance)
        advance.status = 'APPROVED'
        advance.approved_at = timezone.now()
        advance.save()
    _refresh_facility(advance.facility)


def deny_advance(advance, reason: str) -> None:
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status not in ('SCORING', 'REQUESTED'):
            raise ValueError(f"Cannot deny advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _release_for(advance)
        advance.status = 'DENIED'
        advance.denial_reason = reason
        advance.save()
    _refresh_facility(advance.facility)


def cancel_advance(advance, note: str = '') -> None:
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status not in ('ELIGIBLE',) + UNDISBURSED_STATUSES:
            # A disbursed advance has real money out; it is closed by
            # settlement (or a future write-off), never by cancel.
            raise ValueError(f"Cannot cancel advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _release_for(advance)
        advance.status = 'CANCELLED'
        if note:
            advance.notes = f'{advance.notes}\n{note}'.strip() if advance.notes else note
        advance.save()
    _refresh_facility(advance.facility)


def disburse_advance(advance) -> None:
    """APPROVED -> DISBURSED: the reservation becomes outstanding."""
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status != 'APPROVED':
            raise ValueError(f"Cannot disburse advance in status {current.status}")
        held = current.capacity_reserved or Decimal('0.00')
        amt = _amount(advance.amount)
        try:
            _move_reserved_to_outstanding(advance.facility_id, held, amt)
        except CapacityError as exc:
            raise ValueError(f'Cannot disburse: {exc}') from exc
        _set_capacity_reserved(advance, Decimal('0.00'))
        advance.status = 'DISBURSED'
        advance.disbursed_at = timezone.now()
        advance.save()
    _refresh_facility(advance.facility)


def settle_advance(advance, *, payment_reference: str, settled_by=None, payment=None) -> None:
    """DISBURSED -> SETTLED against debtor-payment evidence.

    Authorisation (staff/system only) is the caller's job; this enforces the
    evidence: a non-empty reference, and if a Payment is given it must be on
    the advanced invoice.
    """
    reference = (payment_reference or '').strip()
    if not reference:
        raise ValueError('A payment reference is required to settle an advance')
    if payment is not None and payment.invoice_id != advance.invoice_id:
        raise ValueError('Payment does not belong to the advanced invoice')

    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status != 'DISBURSED':
            raise ValueError(f"Cannot settle advance in status {current.status}")
        try:
            _release_outstanding(advance.facility_id, _amount(advance.amount))
        except CapacityError as exc:
            raise ValueError(f'Cannot settle: {exc}') from exc
        advance.status = 'SETTLED'
        advance.settled_at = timezone.now()
        advance.settlement_reference = reference[:200]
        advance.settlement_payment = payment
        advance.settled_by = settled_by if getattr(settled_by, 'pk', None) else None
        advance.save()
    _refresh_facility(advance.facility)
