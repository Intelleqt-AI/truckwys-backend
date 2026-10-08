"""Fast Pay scheduled jobs as plain functions (Celery wrappers live in core.tasks).

Each job loops over funders whose status is SANDBOX / ACTIVE / PAUSED and
catches errors per funder, so one broken book never stops the others. A
funder with no lines and no ledger rows is skipped, so every job is a fast
no-op until there is a book. Design: docs/capital-risk/03-design.md §6.3.

Run one synchronously with ``manage.py capital_run_jobs <job>``.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from django.db import transaction
from django.utils import timezone

from core.capital.policy import GRADE_RANK

logger = logging.getLogger(__name__)

JOB_FUNDER_STATUSES = ('SANDBOX', 'ACTIVE', 'PAUSED')
LIVE_ADVANCE_STATUSES = ('QUEUED', 'REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED')


def funders():
    from core.models import Funder
    return Funder.objects.filter(status__in=JOB_FUNDER_STATUSES).order_by('id')


def has_book(funder) -> bool:
    from core.models import CapitalLedgerEntry, Facility
    return (Facility.objects.filter(funder=funder).exists()
            or CapitalLedgerEntry.objects.filter(funder=funder).exists())


def _for_each_funder(job: str, fn, *, require_book: bool = True, atomic: bool = True) -> dict:
    """Run ``fn(funder)`` per funder. ``atomic``: one transaction per funder
    (rolled back on error); jobs whose steps keep their own savepoints pass False."""
    out = {'job': job, 'funders': 0, 'skipped': 0, 'errors': [], 'results': {}}
    for f in funders():
        if require_book and not has_book(f):
            out['skipped'] += 1
            continue
        out['funders'] += 1
        try:
            if atomic:
                with transaction.atomic():
                    out['results'][f.code] = fn(f)
            else:
                out['results'][f.code] = fn(f)
        except Exception as exc:  # isolate: one funder's failure never stops the rest
            logger.exception('capital job %s failed for funder %s', job, f.code)
            out['errors'].append({'funder': f.code, 'error': str(exc)[:500]})
    return out


# ---------------------------------------------------------------------------
# Queue (every 15 min)
# ---------------------------------------------------------------------------

def process_queues() -> dict:
    from core.capital import queue
    return _for_each_funder('process_queues', lambda f: queue.process_queue(f))


def process_queue_for(funder_id: int) -> dict:
    """One funder (called after capacity frees up)."""
    from core.capital import queue
    from core.models import Funder
    f = Funder.objects.filter(pk=funder_id, status__in=JOB_FUNDER_STATUSES).first()
    if f is None:
        return {'job': 'process_queue', 'funder_id': funder_id, 'skipped': True}
    return {'job': 'process_queue', 'funder': f.code, 'result': queue.process_queue(f)}


# ---------------------------------------------------------------------------
# Monitoring (hourly)
# ---------------------------------------------------------------------------

def _monitor_one(funder) -> dict:
    from core.capital import monitoring
    out = {}
    steps = (('book', monitoring.run_book_checks), ('early_warnings', monitoring.run_early_warnings),
             ('overdue', monitoring.run_overdue_checks), ('snapshot', lambda f: monitoring.snapshot(f).pk))
    errors = []
    for name, fn in steps:
        try:
            with transaction.atomic():  # one failing step keeps the others' alerts
                out[name] = fn(funder)
        except Exception as exc:
            logger.exception('capital monitor step %s failed for funder %s', name, funder.code)
            errors.append(f'{name}: {exc}')
    if errors:
        raise RuntimeError('; '.join(errors)[:500])
    return out


def monitor() -> dict:
    # Not one transaction per funder: each step commits on its own, so a
    # failing step never rolls back the alerts and snapshot of the others.
    return _for_each_funder('monitor', _monitor_one, atomic=False)


# ---------------------------------------------------------------------------
# Reconciliation (nightly)
# ---------------------------------------------------------------------------

def _reconcile_one(funder) -> dict:
    from core.capital import ledger, monitoring
    result = ledger.reconcile(funder)
    monitoring.check_reconciliation(funder, result)
    return {'ok': result['ok'], 'breaks': len(result['breaks']), 'checked': result['checked']}


def reconcile_all() -> dict:
    out = _for_each_funder('reconcile', _reconcile_one)
    out['ok'] = not out['errors'] and all(r['ok'] for r in out['results'].values())
    return out


# ---------------------------------------------------------------------------
# Rescoring (nightly)
# ---------------------------------------------------------------------------

def _grade_drop(old: str | None, new: str) -> int:
    if not old:
        return 0
    return GRADE_RANK.get(new, 4) - GRADE_RANK.get(old, 4)


def _is_warning(old: str | None, new: str) -> bool:
    return bool(old) and (_grade_drop(old, new) >= 2 or (new == 'E' and old != 'E'))


def _latest_grade(kind: str, **subject):
    from core.models import CapitalScore
    row = CapitalScore.objects.filter(kind=kind, **subject).order_by('-created_at', '-id').only('grade').first()
    return row.grade if row else None


def _rescore_one(funder, done: dict) -> dict:
    from core.capital import ledger, monitoring
    from core.capital.policy import policy_for_funder
    from core.capital.scoring import persist
    from core.capital.scoring.debtor import score_debtor
    from core.capital.scoring.transporter import score_transporter
    from core.models import AdvanceRequest, Company, DebtorIdentity, Facility

    policy = policy_for_funder(funder)
    stats = {'debtors': 0, 'transporters': 0, 'grade_changes': 0, 'warnings': 0, 'errors': 0}
    debtor_ids = {d for d, a in ledger.committed_by(funder, 'debtor').items() if d is not None and a > 0}
    debtor_ids |= set(AdvanceRequest.objects.filter(funder=funder, status__in=LIVE_ADVANCE_STATUSES,
                                                     debtor__isnull=False).values_list('debtor_id', flat=True))

    def warn(kind, subject_key, label, old, new, data):
        if not _is_warning(old, new):
            return
        k = monitoring.key(funder, 'downgrade', f'{subject_key}:{new}')
        _al, created = monitoring.raise_alert(
            funder, kind, 'RED' if new == 'E' else 'AMBER', k, f'{label} downgraded {old} -> {new}',
            'Nightly rescore moved the grade down by two or more steps (or to E). Review the limit.', data)
        stats['warnings'] += int(created)

    for debtor in DebtorIdentity.objects.filter(pk__in=debtor_ids):
        key = ('D', debtor.pk)
        if key not in done:
            old = _latest_grade('DEBTOR', debtor=debtor)
            try:
                with transaction.atomic():
                    new = persist(score_debtor(debtor, policy), debtor=debtor).grade
            except Exception:
                logger.exception('nightly rescore failed for debtor %s', debtor.pk)
                stats['errors'] += 1
                continue
            done[key] = (old, new)
            stats['debtors'] += 1
            stats['grade_changes'] += int(bool(old) and old != new)
        old, new = done[key]
        reg = debtor.registration_number or debtor.vat_number or f'#{debtor.pk}'
        warn('DEBTOR_WARNING', f'debtor:{debtor.pk}', f'Debtor {reg}', old, new,
             {'debtor_id': debtor.pk, 'old_grade': old, 'new_grade': new})

    company_ids = set(Facility.objects.filter(funder=funder, status='ACTIVE').values_list('company_id', flat=True))
    for co in Company.objects.filter(pk__in=company_ids):
        key = ('T', co.pk)
        if key not in done:
            old = _latest_grade('TRANSPORTER', company=co)
            try:
                with transaction.atomic():
                    new = persist(score_transporter(co, policy), company=co).grade
            except Exception:
                logger.exception('nightly rescore failed for company %s', co.pk)
                stats['errors'] += 1
                continue
            done[key] = (old, new)
            stats['transporters'] += 1
            stats['grade_changes'] += int(bool(old) and old != new)
        old, new = done[key]
        warn('TRANSPORTER_WARNING', f'transporter:{co.pk}', co.company_name, old, new,
             {'company_id': co.pk, 'old_grade': old, 'new_grade': new})
    return stats


def nightly_rescore() -> dict:
    """Rescore debtors with exposure / a live advance and transporters with a line.

    Scores are global (not per funder), so a subject shared by two funders is
    scored once; each funder still gets its own downgrade alert."""
    done: dict = {}
    return _for_each_funder('nightly_rescore', lambda f: _rescore_one(f, done))


# ---------------------------------------------------------------------------
# Data room (monthly)
# ---------------------------------------------------------------------------

PERIOD_RE = re.compile(r'^(\d{4})-(\d{2})$')


def previous_period(today: date | None = None) -> str:
    today = today or timezone.localdate()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f'{y:04d}-{m:02d}'


def monthly_data_room(period: str | None = None) -> dict:
    from core.capital import dataroom
    from core.models import DataRoomExport
    period = period or previous_period()

    def one(f):
        if DataRoomExport.objects.filter(funder=f, period=period).exists():
            return {'skipped': 'exists', 'period': period}
        x = dataroom.generate(f, period=period)
        return {'export_id': x.pk, 'period': period, 'content_hash': x.content_hash}

    return _for_each_funder('monthly_data_room', one)


JOBS = {
    'queue': process_queues,
    'monitor': monitor,
    'rescore': nightly_rescore,
    'reconcile': reconcile_all,
    'data-room': monthly_data_room,
}
