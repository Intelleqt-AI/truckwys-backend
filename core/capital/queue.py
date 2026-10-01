"""Queue release and part-fund top-ups (design §3.5).

* QUEUED advances hold no capacity. ``process_queue`` re-evaluates each one
  under the funder lock, in priority order (risk-adjusted margin per rand-day,
  then age), and promotes it to REQUESTED with a reservation when the book has
  room. A transporter takes at most ``queue_fair_share_pct`` of the headroom
  freed in one run while others are waiting.
* A queued item that now fails a hard rule (paid, disputed, debtor on hold...)
  is declined with the reason; one waiting more than ``queue_max_days``
  business days is cancelled.
* A part-funded advance still awaiting approval is topped up towards its
  ``topup_pending`` when its limits allow. Once approved or paid out, the
  pending top-up lapses (Phase 1 limitation: no second tranche).

``capacity_freed(funder)`` runs the queue after a commit that released
capacity (settle, decline, cancel, write-off, buy-back); the Celery beat job
runs it every 15 minutes as well.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
ZERO = Decimal('0.00')


def _business_days_between(start, end) -> int:
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def expire(funder, *, now=None) -> int:
    from core.capital.policy import policy_for_funder
    from core.models import AdvanceRequest
    from core.services import facility_ledger
    now = now or timezone.now()
    limit = policy_for_funder(funder).int('queue_max_days')
    n = 0
    for adv in AdvanceRequest.objects.filter(funder=funder, status='QUEUED', queued_at__isnull=False):
        if _business_days_between(timezone.localtime(adv.queued_at).date(), timezone.localtime(now).date()) > limit:
            facility_ledger.cancel_advance(adv, note=f'Queue expired after {limit} business days without capacity')
            _notify(adv, 'WARNING', 'Fast Pay request expired',
                    f'No funding capacity freed up within {limit} business days. You can request again.')
            n += 1
    return n


def process_queue(funder, *, actor=None, now=None) -> dict:
    """Promote queued advances and top up part-funded ones while capacity allows."""
    from core.capital import book as bookmod
    from core.capital import engine
    from core.models import AdvanceRequest, Funder
    from core.services import facility_ledger

    stats = {'expired': expire(funder, now=now), 'promoted': 0, 'declined': 0, 'still_queued': 0,
             'skipped_fair_share': 0, 'topped_up': 0, 'topped_up_amount': ZERO}
    queued_ids = list(AdvanceRequest.objects.filter(funder=funder, status='QUEUED')
                      .order_by('-queue_priority', 'queued_at', 'id').values_list('pk', flat=True))
    if queued_ids:
        waiting_companies = set(AdvanceRequest.objects.filter(pk__in=queued_ids)
                                .values_list('facility__company_id', flat=True))
        with transaction.atomic():
            locked = Funder.objects.select_for_update().get(pk=funder.pk)
            st = bookmod.load_state(locked)
            start_headroom = st.headroom
            fair_share = st.policy.dec('queue_fair_share_pct') * start_headroom
            allocated = defaultdict(lambda: ZERO)
            for adv_id in queued_ids:
                adv = AdvanceRequest.objects.select_related('invoice', 'facility').get(pk=adv_id)
                if adv.status != 'QUEUED':
                    continue
                ev = engine.evaluate(adv.invoice, state=st, ignore_advance=adv)
                if ev.decision == 'DECLINE':
                    assessment = engine.record(ev, purpose='QUEUE', actor=actor, actor_label='queue job')
                    why = '; '.join(r['text'] for r in ev.reasons if r['direction'] == '!') or 'No longer eligible'
                    facility_ledger.deny_advance(adv, f'No longer eligible: {why}'[:1000])
                    stats['declined'] += 1
                    _notify(adv, 'WARNING', 'Fast Pay request closed', why[:300])
                    continue
                if ev.decision == 'QUEUE':
                    stats['still_queued'] += 1
                    continue
                company_id = adv.facility.company_id
                # Fair share: once a transporter has had something this run, it
                # takes no more than its share while others are waiting.
                if (len(waiting_companies) > 1 and allocated[company_id] > 0
                        and allocated[company_id] + ev.fundable_amount > fair_share):
                    stats['skipped_fair_share'] += 1
                    continue
                assessment = engine.record(ev, purpose='QUEUE', actor=actor, actor_label='queue job')
                facility_ledger.promote_queued(
                    adv, amount=ev.fundable_amount, actor=actor, assessment=assessment,
                    fee_percent=ev.fee_pct, fee_amount=ev.fee_amount, net_amount=ev.net_payout,
                    holdback_amount=ev.holdback_amount, topup_pending=ev.queued_amount)
                allocated[company_id] += ev.fundable_amount
                stats['promoted'] += 1
                if ev.auto_approve:
                    facility_ledger.approve_advance(adv, actor_label='auto-approval (Mode B envelope)')
                _notify(adv, 'SUCCESS', 'Fast Pay capacity freed up',
                        f'{adv.invoice.invoice_number}: R{ev.fundable_amount:,.2f} is now with the finance '
                        'provider for approval.')
                st = bookmod.load_state(locked)  # the book just changed
    t = top_up_pending(funder, actor=actor)
    stats['topped_up'], stats['topped_up_amount'] = t['count'], t['amount']
    return stats


def top_up_pending(funder, *, actor=None) -> dict:
    from core.capital import book as bookmod
    from core.capital.scoring import current_debtor_score, current_transporter_score
    from core.models import AdvanceRequest, Funder
    from core.services import facility_ledger
    out = {'count': 0, 'amount': ZERO}
    ids = list(AdvanceRequest.objects.filter(funder=funder, status__in=('REQUESTED', 'SCORING'),
                                             topup_pending__gt=0).order_by('requested_at', 'id')
               .values_list('pk', flat=True))
    if not ids:
        return out
    with transaction.atomic():
        locked = Funder.objects.select_for_update().get(pk=funder.pk)
        for adv_id in ids:
            adv = AdvanceRequest.objects.select_related('facility', 'invoice', 'debtor').get(pk=adv_id)
            st = bookmod.load_state(locked)
            dscore = current_debtor_score(adv.debtor, st.policy) if adv.debtor else None
            tscore = current_transporter_score(adv.facility.company, st.policy)
            hr = bookmod.headroom(
                st, company_id=adv.facility.company_id, line_limit=adv.facility.limit,
                debtor_id=adv.debtor_id, debtor_grade=getattr(dscore, 'grade', None),
                debtor_cold_start=bool(getattr(dscore, 'cold_start', True)),
                transporter_grade=getattr(tscore, 'grade', None),
                sector=getattr(adv.debtor, 'sector', 'UNKNOWN'))
            hr.pop('advance_brake_pp', None)
            ticket = hr.pop('ticket_cap', None)
            extra, _scope = bookmod.binding(hr, adv.topup_pending)
            if ticket is not None:
                extra = min(extra, max(ZERO, ticket - adv.amount))
            if extra <= 0:
                continue
            vat_fraction = st.policy.dec('platform_fee_pct') / 100 * st.policy.dec('platform_fee_vat_rate')
            added = facility_ledger.top_up(adv, extra=extra, fee_percent=adv.fee_percent,
                                           vat_fraction=vat_fraction, actor=actor)
            if added > 0:
                out['count'] += 1
                out['amount'] += added
    return out


def capacity_freed(funder) -> None:
    """Run the queue for ``funder`` after the current transaction commits."""
    if funder is None:
        return
    funder_id = funder.pk if hasattr(funder, 'pk') else funder

    def run():
        try:
            from core.tasks import capital_process_queue
            capital_process_queue.delay(funder_id)
        except Exception:
            try:
                from core.models import Funder
                process_queue(Funder.objects.get(pk=funder_id))
            except Exception:
                logger.exception('queue run after capacity freed failed (funder %s)', funder_id)

    transaction.on_commit(run)


def queue_position(advance) -> int | None:
    from core.models import AdvanceRequest
    if advance.status != 'QUEUED':
        return None
    ahead = AdvanceRequest.objects.filter(funder_id=advance.funder_id, status='QUEUED').filter(
        models_q_ahead(advance)).count()
    return ahead + 1


def models_q_ahead(advance):
    from django.db.models import Q
    return (Q(queue_priority__gt=advance.queue_priority)
            | Q(queue_priority=advance.queue_priority, queued_at__lt=advance.queued_at)
            | Q(queue_priority=advance.queue_priority, queued_at=advance.queued_at, id__lt=advance.id))


def _notify(advance, ntype, title, message):
    try:
        from core.services.notify import notify_company
        notify_company(advance.facility.company_id, ntype, title, message,
                       link='/capital', event='advance.status')
    except Exception:
        pass
