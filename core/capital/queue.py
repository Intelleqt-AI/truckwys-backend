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
             'skipped_fair_share': 0, 'topped_up': 0, 'topped_up_amount': ZERO, 'errors': 0, 'paused': False}
    funder.refresh_from_db()
    if not funder.accepts_new_advances:
        # A paused funder keeps its queue: nobody is declined for the pause.
        stats['paused'] = True
        return stats
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
                try:
                    with transaction.atomic():  # one item failing never undoes the others
                        outcome = _process_one(adv, st, engine, facility_ledger, actor, waiting_companies,
                                               allocated, fair_share)
                except (facility_ledger.CapacityError, ValueError):
                    logger.exception('queue item %s could not be processed', adv_id)
                    stats['errors'] += 1
                    continue
                stats[outcome] += 1
                if outcome == 'promoted':
                    st = bookmod.load_state(locked)  # the book just changed
    t = top_up_pending(funder, actor=actor)
    stats['topped_up'], stats['topped_up_amount'] = t['count'], t['amount']
    return stats


# Hard rules about the line or funder are not the transporter's invoice
# failing: the item waits instead of being declined.
_WAIT_RULES = {'line', 'funder'}


def _safe_why(ev) -> str:
    from core.capital.reasons import for_transporter
    texts = [r['text'] for r in for_transporter(ev.reasons) if r['direction'] == '!']
    return '; '.join(dict.fromkeys(texts)) or 'This invoice can no longer be funded'


def _process_one(adv, st, engine, facility_ledger, actor, waiting_companies, allocated, fair_share) -> str:
    ev = engine.evaluate(adv.invoice, state=st, ignore_advance=adv)
    failed = {r['rule'] for r in ev.eligibility if not r['passed']}
    if ev.decision == 'DECLINE' and failed and failed <= _WAIT_RULES:
        return 'still_queued'
    line = engine.line_for(adv.facility.company)
    if ev.decision != 'DECLINE' and (line is None or line.pk != adv.facility_id):
        engine.record(ev, purpose='QUEUE', actor=actor, actor_label='queue job')
        facility_ledger.deny_advance(adv, 'Your Fast Pay line changed. Please request again.')
        _notify(adv, 'WARNING', 'Fast Pay request closed', 'Your Fast Pay line changed. Please request again.')
        return 'declined'
    if ev.decision == 'DECLINE':
        engine.record(ev, purpose='QUEUE', actor=actor, actor_label='queue job')
        why = _safe_why(ev)  # transporter wording only: it is stored and shown to them
        facility_ledger.deny_advance(adv, f'No longer eligible: {why}'[:1000])
        _notify(adv, 'WARNING', 'Fast Pay request closed', why[:300])
        return 'declined'
    if ev.decision == 'QUEUE':
        return 'still_queued'
    company_id = adv.facility.company_id
    # Fair share: once a transporter has had something this run, it takes no
    # more than its share while others are waiting.
    if (len(waiting_companies) > 1 and allocated[company_id] > 0
            and allocated[company_id] + ev.fundable_amount > fair_share):
        return 'skipped_fair_share'
    assessment = engine.record(ev, purpose='QUEUE', actor=actor, actor_label='queue job')
    facility_ledger.promote_queued(
        adv, amount=ev.fundable_amount, actor=actor, assessment=assessment,
        fee_percent=ev.fee_pct, fee_amount=ev.fee_amount, net_amount=ev.net_payout,
        holdback_amount=ev.holdback_amount, topup_pending=ev.queued_amount)
    allocated[company_id] += ev.fundable_amount
    if ev.auto_approve:
        facility_ledger.approve_advance(adv, actor_label='auto-approval (Mode B envelope)')
    _notify(adv, 'SUCCESS', 'Fast Pay capacity freed up',
            f'{adv.invoice.invoice_number}: R{ev.fundable_amount:,.2f} is now with the finance '
            'provider for approval.')
    return 'promoted'


def top_up_pending(funder, *, actor=None) -> dict:
    """Grow part-funded advances still awaiting approval. Each one is
    re-evaluated first: if the invoice or parties no longer qualify, the
    pending top-up is dropped, and the extra never exceeds what the invoice
    itself still supports."""
    from core.capital import book as bookmod
    from core.capital import engine
    from core.models import AdvanceRequest, Funder
    from core.services import facility_ledger
    out = {'count': 0, 'amount': ZERO}
    if not funder.accepts_new_advances:
        return out
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
            ev = engine.evaluate(adv.invoice, state=st, ignore_advance=adv)
            if not ev.eligible or ev.decision == 'DECLINE':
                AdvanceRequest.objects.filter(pk=adv.pk).update(topup_pending=ZERO)
                continue
            # ev.headroom counts this advance's own reservation as used, so its
            # binding headroom is the room for the extra; the invoice caps it too.
            room, _scope = bookmod.binding(ev.headroom, adv.topup_pending)
            extra = min(room, max(ZERO, ev.eligible_amount - adv.amount))
            ticket = bookmod.headroom(
                st, company_id=adv.facility.company_id, line_limit=adv.facility.limit, debtor_id=adv.debtor_id,
                debtor_grade=getattr(ev.debtor_score, 'grade', None),
                debtor_cold_start=bool(getattr(ev.debtor_score, 'cold_start', True)),
                transporter_grade=getattr(ev.transporter_score, 'grade', None),
                sector=getattr(adv.debtor, 'sector', 'UNKNOWN')).get('ticket_cap')
            if ticket is not None:
                extra = min(extra, max(ZERO, ticket - adv.amount))
            if extra <= 0:
                continue
            vat_fraction = st.policy.dec('platform_fee_pct') / 100 * st.policy.dec('platform_fee_vat_rate')
            try:
                with transaction.atomic():
                    added = facility_ledger.top_up(adv, extra=extra, fee_percent=adv.fee_percent,
                                                   vat_fraction=vat_fraction, actor=actor)
            except (facility_ledger.CapacityError, ValueError):
                logger.exception('top-up of advance %s failed', adv_id)
                continue
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
