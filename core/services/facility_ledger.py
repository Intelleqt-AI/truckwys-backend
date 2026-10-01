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

Fast Pay book (0139):
* Every capacity change also posts an append-only ``CapitalLedgerEntry``
  (core.capital.ledger) in the same transaction, so ledger-derived balances
  and the cached facility figures move together; ``reconcile()`` proves it.
* Lock order is always funder -> facility -> advance. ``_lock_facility``
  takes the funder row lock first, so every reservation under one funder is
  serialised and the funder's pot can be checked here (``_check_pot``);
  debtor / pair / sector caps are checked by core.capital.engine under the
  same funder lock before it calls ``open_advance``.
* QUEUED advances hold no capacity. ``promote_queued`` reserves for them,
  ``top_up`` grows an undisbursed advance, ``write_off`` / ``buy_back``
  close a disbursed one.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ('QUEUED', 'REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED')
UNDISBURSED_STATUSES = ('QUEUED', 'REQUESTED', 'SCORING', 'APPROVED')


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
    """Lock the facility's funder (if any), then the facility. Funder first so
    the order matches core.capital.engine, which locks the funder before it
    reads the book and opens an advance."""
    Facility = _facility_model()
    funder_id = Facility.objects.filter(pk=facility_id).values_list('funder_id', flat=True).first()
    if funder_id:
        from core.models import Funder
        list(Funder.objects.select_for_update().filter(pk=funder_id).values_list('pk', flat=True))
    return Facility.objects.select_for_update().get(pk=facility_id)


def _check_pot(facility_id: int, amt: Decimal) -> None:
    """Refuse a reservation that would take the funder's book past its pot.
    Caller holds the funder lock (via _lock_facility)."""
    Facility = _facility_model()
    fac = Facility.objects.select_related('funder').only('funder').get(pk=facility_id)
    funder = fac.funder
    if funder is None:
        return
    if not funder.accepts_new_advances:
        raise CapacityError(f'Funder is {funder.get_status_display()}')
    from core.capital.ledger import balances
    committed = balances(funder=funder)['committed']
    if committed + amt > funder.pot_limit:
        raise CapacityError(
            f'Funder pot would be exceeded (R{funder.pot_limit - committed} available, R{amt} needed)')


def _lock_advance(advance):
    from core.models import AdvanceRequest
    return AdvanceRequest.objects.select_for_update().get(pk=advance.pk)


# ---------------------------------------------------------------------------
# Facility-level helpers (used by Facility.reserve_amount/release_amount)
# ---------------------------------------------------------------------------

def add_outstanding(facility, amount, actor=None) -> None:
    amt = _amount(amount)
    with transaction.atomic():
        _lock_facility(facility.pk)
        _check_pot(facility.pk, amt)
        _move_reserved_to_outstanding(facility.pk, Decimal('0.00'), amt)
        _post('ADJUSTMENT', facility_id=facility.pk, actor=actor, amount=amt, outstanding_delta=amt,
              memo='Facility outstanding added directly (no advance)')
    _refresh_facility(facility)


def release_outstanding(facility, amount, actor=None) -> None:
    amt = _amount(amount)
    with transaction.atomic():
        _lock_facility(facility.pk)
        _release_outstanding(facility.pk, amt)
        _post('ADJUSTMENT', facility_id=facility.pk, actor=actor, amount=amt, outstanding_delta=-amt,
              memo='Facility outstanding released directly (no advance)')
    _refresh_facility(facility)


def reserve_capacity(facility, amount, actor=None) -> None:
    """Reserve capacity on a facility (no advance bookkeeping)."""
    amt = _amount(amount)
    with transaction.atomic():
        _lock_facility(facility.pk)
        _check_pot(facility.pk, amt)
        _add_reserved(facility.pk, amt)
        _post('ADJUSTMENT', facility_id=facility.pk, actor=actor, amount=amt, reserved_delta=amt,
              memo='Facility capacity reserved directly (no advance)')
    _refresh_facility(facility)


# ---------------------------------------------------------------------------
# Advance lifecycle
# ---------------------------------------------------------------------------

def _set_capacity_reserved(advance, value: Decimal) -> None:
    from core.models import AdvanceRequest
    AdvanceRequest.objects.filter(pk=advance.pk).update(capacity_reserved=value)
    advance.capacity_reserved = value


def _post(entry_type, advance=None, *, facility_id=None, actor=None, **kw):
    from core.capital import ledger
    facility = None
    if advance is None and facility_id is not None:
        facility = _facility_model().objects.select_related('funder', 'company').get(pk=facility_id)
    return ledger.post(entry_type, advance=advance, facility=facility, actor=actor, **kw)


def _reserve_for(advance, actor=None) -> None:
    """Make the advance hold a reservation for its full amount (lock held)."""
    held = advance.capacity_reserved or Decimal('0.00')
    need = _amount(advance.amount) - held
    if need > 0:
        _check_pot(advance.facility_id, need)
        _add_reserved(advance.facility_id, need)
        _set_capacity_reserved(advance, held + need)
        _post('RESERVE', advance, actor=actor, amount=need, reserved_delta=need,
              memo='Capacity reserved for the advance')


def _release_for(advance, actor=None, memo='') -> None:
    held = advance.capacity_reserved or Decimal('0.00')
    if held > 0:
        _release_reserved(advance.facility_id, held)
        _set_capacity_reserved(advance, Decimal('0.00'))
        _post('CANCEL_RESERVE', advance, actor=actor, amount=held, reserved_delta=-held,
              memo=memo or 'Reservation released')


def open_advance(*, invoice, facility, amount, actor=None, status='REQUESTED', **fields):
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
            if status == 'QUEUED':
                fields.setdefault('queued_at', timezone.now())
            else:
                fields.setdefault('requested_at', timezone.now())
            fields.setdefault('funder_id', facility.funder_id)
            if 'debtor' not in fields and 'debtor_id' not in fields:
                fields['debtor_id'] = getattr(getattr(invoice, 'customer', None), 'debtor_identity_id', None)
            advance = AdvanceRequest.objects.create(
                invoice=invoice, facility=facility, amount=amt, status=status, **fields)
            if status != 'QUEUED':  # a queued advance holds no capacity until promoted
                _reserve_for(advance, actor=actor)
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


def request_advance(advance, actor=None) -> None:
    """ELIGIBLE -> REQUESTED, reserving capacity."""
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status != 'ELIGIBLE':
            raise ValueError(f"Cannot request advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _reserve_for(advance, actor=actor)
        advance.status = 'REQUESTED'
        advance.requested_at = timezone.now()
        advance.save()
    _refresh_facility(advance.facility)


def approve_advance(advance, actor=None, actor_label: str = '') -> None:
    """REQUESTED/SCORING -> APPROVED; keeps (or, for legacy rows, takes) a reservation."""
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status not in ('SCORING', 'REQUESTED'):
            raise ValueError(f"Cannot approve advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _reserve_for(advance, actor=actor)
        advance.status = 'APPROVED'
        advance.approved_at = timezone.now()
        if actor is not None and getattr(actor, 'pk', None) and type(actor).__name__ != 'LenderUser':
            advance.approved_by = actor
        advance.approver_label = (actor_label or getattr(actor, 'username', '') or '')[:120]
        advance.save()
    _refresh_facility(advance.facility)


def deny_advance(advance, reason: str, actor=None) -> None:
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status not in ('SCORING', 'REQUESTED', 'QUEUED'):
            raise ValueError(f"Cannot deny advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _release_for(advance, actor=actor, memo=f'Declined: {reason}'[:500])
        advance.topup_pending = Decimal('0.00')
        advance.status = 'DENIED'
        advance.denial_reason = reason
        advance.save()
    _refresh_facility(advance.facility)


def cancel_advance(advance, note: str = '', actor=None) -> None:
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status not in ('ELIGIBLE',) + UNDISBURSED_STATUSES:
            # A disbursed advance has real money out; it is closed by
            # settlement (or a future write-off), never by cancel.
            raise ValueError(f"Cannot cancel advance in status {current.status}")
        advance.capacity_reserved = current.capacity_reserved
        _release_for(advance, actor=actor, memo=f'Cancelled. {note}'.strip()[:500])
        advance.topup_pending = Decimal('0.00')
        advance.status = 'CANCELLED'
        if note:
            advance.notes = f'{advance.notes}\n{note}'.strip() if advance.notes else note
        advance.save()
    _refresh_facility(advance.facility)


def disburse_advance(advance, actor=None, reference: str = '') -> None:
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
        _post('DISBURSE', advance, actor=actor, amount=amt, reserved_delta=-held, outstanding_delta=amt,
              reference=reference, memo='Advance paid out')
        if advance.fee_amount and advance.fee_amount > 0:
            _post('FEE', advance, actor=actor, amount=advance.fee_amount, reference=reference,
                  memo=f'Fee {advance.fee_percent}% deducted from the payout (excl. VAT)')
        # Anything still waiting to be topped up lapses once money has moved.
        advance.topup_pending = Decimal('0.00')
        advance.status = 'DISBURSED'
        advance.disbursed_at = timezone.now()
        if actor is not None and getattr(actor, 'pk', None) and type(actor).__name__ != 'LenderUser':
            advance.disbursed_by = actor
        if reference:
            advance.disbursement_reference = reference[:200]
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
        amt = _amount(advance.amount)
        try:
            _release_outstanding(advance.facility_id, amt)
        except CapacityError as exc:
            raise ValueError(f'Cannot settle: {exc}') from exc
        _post('COLLECTION', advance, actor=settled_by, amount=amt, outstanding_delta=-amt,
              reference=reference, memo='Debtor payment received; advance recovered')
        inv = advance.invoice
        holdback = (Decimal(str(inv.total_amount or 0)) - Decimal(str(getattr(inv, 'credited_amount', 0) or 0))
                    - amt).quantize(Decimal('0.01'))
        if holdback > 0:
            _post('RELEASE_HOLDBACK', advance, actor=settled_by, amount=holdback, reference=reference,
                  memo='Holdback owed to the transporter (invoice collected less the advance)')
        advance.status = 'SETTLED'
        advance.settled_at = timezone.now()
        advance.settlement_reference = reference[:200]
        advance.settlement_payment = payment
        advance.settled_by = settled_by if getattr(settled_by, 'pk', None) else None
        advance.save()
    _refresh_facility(advance.facility)


# ---------------------------------------------------------------------------
# Fast Pay book additions (0139)
# ---------------------------------------------------------------------------

def promote_queued(advance, *, amount, actor=None, **fields) -> None:
    """QUEUED -> REQUESTED with a reservation of ``amount`` (capacity freed).

    The engine has already checked every limit under the funder lock; this
    re-checks line and pot capacity and records the reservation.
    """
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status != 'QUEUED':
            raise ValueError(f'Cannot promote advance in status {current.status}')
        advance.capacity_reserved = current.capacity_reserved
        advance.amount = _amount(amount)
        for k, v in fields.items():
            setattr(advance, k, v)
        advance.status = 'REQUESTED'
        advance.requested_at = timezone.now()
        advance.save()
        _reserve_for(advance, actor=actor)
    _refresh_facility(advance.facility)


def top_up(advance, *, extra, fee_percent, vat_fraction=Decimal('0'), actor=None) -> Decimal:
    """Grow an undisbursed (REQUESTED/SCORING) advance by up to ``extra`` of its
    pending top-up. Returns the amount added. Caller checked the limits."""
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status not in ('REQUESTED', 'SCORING'):
            return Decimal('0.00')
        add = min(_amount(extra), current.topup_pending or Decimal('0.00'))
        if add <= 0:
            return Decimal('0.00')
        advance.capacity_reserved = current.capacity_reserved
        advance.amount = current.amount + add
        advance.topup_pending = (current.topup_pending - add).quantize(Decimal('0.01'))
        advance.calculate_fee(Decimal(str(fee_percent)))
        # VAT on the platform-fee part is deducted from the payout too.
        advance.net_amount = (advance.net_amount
                              - (advance.amount * Decimal(str(vat_fraction))).quantize(Decimal('0.01')))
        advance.holdback_amount = max(Decimal('0.00'), (advance.holdback_amount or Decimal('0.00')) - add)
        advance.save()
        _reserve_for(advance, actor=actor)
    _refresh_facility(advance.facility)
    return add


def _close_disbursed(advance, *, entry_type, new_status, reason, actor=None, reference='') -> None:
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('A reason is required')
    with transaction.atomic():
        _lock_facility(advance.facility_id)
        current = _lock_advance(advance)
        if current.status != 'DISBURSED':
            raise ValueError(f'Cannot close advance in status {current.status}')
        amt = _amount(advance.amount)
        try:
            _release_outstanding(advance.facility_id, amt)
        except CapacityError as exc:
            raise ValueError(f'Cannot close: {exc}') from exc
        _post(entry_type, advance, actor=actor, amount=amt, outstanding_delta=-amt,
              reference=reference, memo=reason)
        advance.status = new_status
        advance.settled_at = timezone.now()
        advance.notes = f'{advance.notes}\n{new_status}: {reason}'.strip()
        advance.save()
    _refresh_facility(advance.facility)


def write_off(advance, *, reason: str, actor=None) -> None:
    """DISBURSED -> WRITTEN_OFF: the loss is recognised and exposure released."""
    _close_disbursed(advance, entry_type='WRITE_OFF', new_status='WRITTEN_OFF', reason=reason, actor=actor)


def buy_back(advance, *, reason: str, reference: str, actor=None) -> None:
    """DISBURSED -> BOUGHT_BACK: the transporter repaid the advance (recourse)."""
    if not (reference or '').strip():
        raise ValueError('A payment reference is required for a buy-back')
    _close_disbursed(advance, entry_type='BUYBACK', new_status='BOUGHT_BACK', reason=reason,
                     actor=actor, reference=reference)
