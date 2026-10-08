"""Stored, verified figures for the AI quote price check, and their review
workflow (propose -> approve / reject).

  * SANRAL toll tariffs live on TollPlaza (tariff_class_2..5, VAT inclusive
    as published) with their verification data (tariff_effective_from,
    tariff_source_url, tariff_source_name, tariff_verified_at). A changed
    tariff is proposed as a VerifiedRate(kind='toll_tariff') row; approving
    it writes the new tariff onto the plaza, so the route toll calculator
    and the price check both use it.
  * The driver allowance is VerifiedRate(kind='driver_allowance') rows. The
    approved row in force today is the stored figure; older approved rows
    are the history.

Nothing here talks to the network. The monthly refresh job
(core.services.verified_rate_refresh) is what proposes changes.
"""
import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

ALLOWANCE_LABELS = {
    'nbcrfli': 'NBCRFLI driver allowance',
    'nbcrfli_cross_border': 'NBCRFLI cross-border driver allowance',
    'sars_subsistence': 'SARS daily subsistence allowance (meals & incidentals)',
}
# Which approved allowance the price check uses when several are on record.
ALLOWANCE_PREFERENCE = ('nbcrfli', 'sars_subsistence')
# International trips (NBCRFLI clause 36B replaces 36A outside SA).
CROSS_BORDER_PREFERENCE = ('nbcrfli_cross_border',) + ALLOWANCE_PREFERENCE
SANRAL_CLASSES = (1, 2, 3, 4)


class ReviewError(Exception):
    """A proposal can't be approved / rejected (message is safe to show)."""


def toll_key(plaza_id: int, sanral_class: int) -> str:
    return f'toll:{plaza_id}:class{sanral_class}'


def _tariff_column(sanral_class: int) -> str:
    # TollPlaza columns are named one higher than the SANRAL class they hold
    # (see core.services.toll_calculator.TRUCK_TYPE_TO_CLASS).
    return f'tariff_class_{int(sanral_class) + 1}'


def _excl_vat(amount_incl_vat) -> Decimal:
    from core.services.toll_calculator import tariff_excl_vat
    return tariff_excl_vat(amount_incl_vat)


# ---------------------------------------------------------------------------
# Lookups for the per-quote check (DB only)
# ---------------------------------------------------------------------------

def stored_toll_tariffs(route_plazas, sanral_class) -> list:
    """The stored tariff for each plaza on a quote's route, for one SANRAL
    class. `route_plazas` is [{'plaza': name, 'route': 'N1' or None}] as the
    route calculation returned them. Plazas are matched on their exact name
    (case-insensitive), and on the route code when the quote has it.

    Returns one dict per route plaza: found / ambiguous, the tariff incl.
    and excl. VAT, and the plaza's verification data. Never raises."""
    from core.models import TollPlaza

    out = []
    column = _tariff_column(sanral_class) if sanral_class in SANRAL_CLASSES else None
    for rp in route_plazas:
        row = {'plaza': rp.get('plaza'), 'route': rp.get('route'), 'plaza_id': None, 'found': False,
               'ambiguous': False, 'tariff_incl_vat': None, 'tariff_excl_vat': None, 'effective_from': None,
               'verified_at': None, 'source_url': '', 'source_name': ''}
        out.append(row)
        if column is None or not rp.get('plaza'):
            continue
        try:
            qs = TollPlaza.objects.filter(is_active=True, name__iexact=rp['plaza'].strip())
            if rp.get('route'):
                qs = qs.filter(route=rp['route'])
            matches = list(qs[:2])
        except Exception as exc:
            logger.warning('AI price analysis: toll tariff lookup failed: %s', exc)
            continue
        if len(matches) != 1:
            row['ambiguous'] = len(matches) > 1
            continue
        plaza = matches[0]
        incl = getattr(plaza, column)
        row.update({
            'plaza_id': plaza.id, 'route': plaza.route, 'found': True,
            'tariff_incl_vat': float(incl), 'tariff_excl_vat': float(_excl_vat(incl)),
            'effective_from': plaza.tariff_effective_from, 'verified_at': plaza.tariff_verified_at,
            'source_url': plaza.tariff_source_url, 'source_name': plaza.tariff_source_name,
        })
    return out


def current_allowance_row(key: str, today: date):
    from core.models import VerifiedRate
    return (VerifiedRate.objects
            .filter(kind=VerifiedRate.KIND_DRIVER_ALLOWANCE, key=key, status=VerifiedRate.STATUS_APPROVED,
                    effective_from__lte=today)
            .order_by('-effective_from', '-approved_at', '-id')
            .first())


def current_allowance(today: date, cross_border: bool = False):
    """The approved driver allowance in force today (NBCRFLI first, then the
    SARS subsistence allowance), or None when an admin has approved none.
    {'rate_per_night', 'allowance_type', 'label', 'effective_from',
    'verified_at', 'source_url', 'source_name', 'id'}. Never raises."""
    try:
        for key in (CROSS_BORDER_PREFERENCE if cross_border else ALLOWANCE_PREFERENCE):
            row = current_allowance_row(key, today)
            if row is not None:
                return {'id': row.id, 'rate_per_night': float(row.value), 'allowance_type': key,
                        'label': ALLOWANCE_LABELS.get(key, row.label or key),
                        'effective_from': row.effective_from, 'verified_at': row.verified_at,
                        'source_url': row.source_url, 'source_name': row.source_name}
    except Exception as exc:
        logger.warning('AI price analysis: driver allowance lookup failed: %s', exc)
    return None


# ---------------------------------------------------------------------------
# Proposals and review
# ---------------------------------------------------------------------------

def propose(*, kind, key, value, effective_from, unit, label='', published_value=None, previous_value=None,
            source_url='', source_name='', verified_at=None, proposed_by='', toll_plaza=None,
            sanral_class=None, refresh_run=None, evidence=None):
    """Record a PENDING change. Returns (row, outcome):
      'created'             a new pending row (an older pending one for the
                            same key is marked superseded);
      'already_pending'     the same figure and date is already waiting for
                            review (its verified_at is refreshed);
      'previously_rejected' an admin already rejected this figure and date,
                            so it is not proposed again.
    Never applies anything."""
    from core.models import VerifiedRate

    value = Decimal(str(value)).quantize(Decimal('0.01'))
    same = VerifiedRate.objects.filter(kind=kind, key=key, value=value, effective_from=effective_from)
    with transaction.atomic():
        rejected = same.filter(status=VerifiedRate.STATUS_REJECTED).first()
        if rejected is not None:
            return rejected, 'previously_rejected'
        pending = same.filter(status=VerifiedRate.STATUS_PENDING).first()
        if pending is not None:
            pending.verified_at = verified_at or pending.verified_at
            pending.save(update_fields=['verified_at', 'updated_at'])
            return pending, 'already_pending'
        (VerifiedRate.objects.filter(kind=kind, key=key, status=VerifiedRate.STATUS_PENDING)
         .update(status=VerifiedRate.STATUS_SUPERSEDED, updated_at=timezone.now()))
        row = VerifiedRate.objects.create(
            kind=kind, key=key, label=label, value=value,
            published_value=Decimal(str(published_value)).quantize(Decimal('0.01')) if published_value is not None else None,
            previous_value=Decimal(str(previous_value)).quantize(Decimal('0.01')) if previous_value is not None else None,
            unit=unit, effective_from=effective_from, source_url=source_url or '', source_name=source_name or '',
            verified_at=verified_at, status=VerifiedRate.STATUS_PENDING, proposed_by=proposed_by,
            toll_plaza=toll_plaza, sanral_class=sanral_class, refresh_run=refresh_run, evidence=evidence or {},
        )
    return row, 'created'


def approve(rate_id: int, user, *, effective_from: date = None, note: str = '', today: date = None):
    """Apply a pending proposal. A toll tariff is written onto its plaza
    (VAT inclusive, as published) with its verification data; a driver
    allowance becomes the approved figure from its effective date (older
    approved rows stay as history). Raises ReviewError."""
    from core.models import TollPlaza, VerifiedRate

    today = today or timezone.localdate()
    with transaction.atomic():
        rate = VerifiedRate.objects.select_for_update().filter(id=rate_id).first()
        if rate is None:
            raise ReviewError('Proposal not found.')
        if rate.status != VerifiedRate.STATUS_PENDING:
            raise ReviewError(f'Only a pending proposal can be approved (this one is {rate.status}).')
        if effective_from is not None:
            rate.effective_from = effective_from
        if rate.kind == VerifiedRate.KIND_TOLL_TARIFF:
            if rate.effective_from > today:
                raise ReviewError(f'This tariff only takes effect on {rate.effective_from.isoformat()}; '
                                  'approve it on or after that day.')
            plaza = TollPlaza.objects.select_for_update().filter(id=rate.toll_plaza_id).first()
            if plaza is None or rate.sanral_class not in SANRAL_CLASSES:
                raise ReviewError('The toll plaza for this proposal no longer exists.')
            published = rate.published_value
            if published is None:
                raise ReviewError('This toll proposal has no published (VAT inclusive) tariff.')
            setattr(plaza, _tariff_column(rate.sanral_class), published)
            plaza.tariff_effective_from = rate.effective_from
            plaza.tariff_year = rate.effective_from.year
            plaza.tariff_source_url = rate.source_url
            plaza.tariff_source_name = rate.source_name
            plaza.tariff_verified_at = rate.verified_at or today
            plaza.save()
        rate.status = VerifiedRate.STATUS_APPROVED
        rate.approved_by = user if getattr(user, 'is_authenticated', False) else None
        rate.approved_at = timezone.now()
        rate.review_note = note or rate.review_note
        rate.save()
        # Any other proposal still waiting for this key is moot now.
        (VerifiedRate.objects.filter(kind=rate.kind, key=rate.key, status=VerifiedRate.STATUS_PENDING)
         .exclude(id=rate.id).update(status=VerifiedRate.STATUS_SUPERSEDED, updated_at=timezone.now()))
    logger.info('Verified rate %s approved: %s = R%s from %s', rate.id, rate.key, rate.value, rate.effective_from)
    return rate


def reject(rate_id: int, user, *, note: str = ''):
    from core.models import VerifiedRate

    with transaction.atomic():
        rate = VerifiedRate.objects.select_for_update().filter(id=rate_id).first()
        if rate is None:
            raise ReviewError('Proposal not found.')
        if rate.status != VerifiedRate.STATUS_PENDING:
            raise ReviewError(f'Only a pending proposal can be rejected (this one is {rate.status}).')
        rate.status = VerifiedRate.STATUS_REJECTED
        rate.rejected_by = user if getattr(user, 'is_authenticated', False) else None
        rate.rejected_at = timezone.now()
        rate.review_note = note or rate.review_note
        rate.save()
    return rate


# ---------------------------------------------------------------------------
# Admin listing
# ---------------------------------------------------------------------------

def _money(v):
    return float(v) if v is not None else None


def current_value(rate, today: date = None):
    """The figure in use now for this row's key (same basis as rate.value:
    tolls excl. VAT), or None."""
    from core.models import VerifiedRate

    today = today or timezone.localdate()
    if rate.kind == VerifiedRate.KIND_TOLL_TARIFF:
        plaza = rate.toll_plaza
        if plaza is None or rate.sanral_class not in SANRAL_CLASSES:
            return None
        return float(_excl_vat(getattr(plaza, _tariff_column(rate.sanral_class))))
    row = current_allowance_row(rate.key, today)
    return _money(row.value) if row is not None else None


def serialize(rate, today: date = None) -> dict:
    return {
        'id': rate.id,
        'kind': rate.kind,
        'key': rate.key,
        'label': rate.label,
        'status': rate.status,
        'unit': rate.unit,
        'vat_basis': 'excl_vat' if rate.kind == 'toll_tariff' else None,
        'current_value': current_value(rate, today),
        'proposed_value': _money(rate.value),
        'published_value': _money(rate.published_value),
        'previous_value': _money(rate.previous_value),
        'effective_from': rate.effective_from.isoformat() if rate.effective_from else None,
        'source_url': rate.source_url,
        'source_name': rate.source_name,
        'verified_at': rate.verified_at.isoformat() if rate.verified_at else None,
        'found_at': rate.created_at.isoformat() if rate.created_at else None,
        'proposed_by': rate.proposed_by,
        'toll_plaza_id': rate.toll_plaza_id,
        'sanral_class': rate.sanral_class,
        'approved_by': getattr(rate.approved_by, 'username', None),
        'approved_at': rate.approved_at.isoformat() if rate.approved_at else None,
        'rejected_by': getattr(rate.rejected_by, 'username', None),
        'rejected_at': rate.rejected_at.isoformat() if rate.rejected_at else None,
        'review_note': rate.review_note,
    }


def toll_verification_summary() -> dict:
    """How much of the active SANRAL tariff table is verified, and how old
    the oldest verification is."""
    from django.db.models import Count, Min, Q
    from core.models import TollPlaza

    agg = TollPlaza.objects.filter(is_active=True).aggregate(
        plazas=Count('id'), verified=Count('id', filter=Q(tariff_verified_at__isnull=False)),
        oldest_verified_at=Min('tariff_verified_at'), oldest_effective_from=Min('tariff_effective_from'))
    return {
        'active_plazas': agg['plazas'] or 0,
        'verified_plazas': agg['verified'] or 0,
        'oldest_verified_at': agg['oldest_verified_at'].isoformat() if agg['oldest_verified_at'] else None,
        'oldest_effective_from': agg['oldest_effective_from'].isoformat() if agg['oldest_effective_from'] else None,
    }
