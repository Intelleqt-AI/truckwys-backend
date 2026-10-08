"""
Vehicle performance score computation tasks.

Scores are derived entirely from real operational data:
  - Load history (revenue, distance, trip days)
  - Vehicle maintenance/compliance dates
  - VehicleLog cost entries
  - Vehicle type fuel benchmark

Run triggers:
  1. Immediately when a load is marked DELIVERED or INVOICED (via signal)
  2. Immediately when a vehicle's maintenance/compliance dates are saved (via signal)
  3. Nightly at 02:00 for all vehicles (Celery Beat)
"""

import logging
from decimal import Decimal
from datetime import date

from celery import shared_task
from django.db.models import Sum

from core.services.task_run import track_task_run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transactional email helpers — called synchronously from views
# ---------------------------------------------------------------------------

def send_verification_email_task(email: str, code: str, first_name: str):
    try:
        from core.services.email_service import send_verification_email
        send_verification_email(email, code, first_name)
    except Exception as exc:
        logger.error('send_verification_email failed for %s: %s', email, exc)


def send_password_reset_email_task(email: str, first_name: str, reset_code: str):
    try:
        from core.services.email_service import send_password_reset_email
        if not send_password_reset_email(email, first_name, reset_code):
            logger.error('send_password_reset_email returned False for %s (delivery failed)', email)
    except Exception as exc:
        logger.error('send_password_reset_email failed for %s: %s', email, exc)


def send_invite_email_task(email: str, invited_by_name: str, company_name: str, invite_url: str, role: str):
    try:
        from core.services.email_service import send_invite_email
        send_invite_email(email, invited_by_name, company_name, invite_url, role)
    except Exception as exc:
        logger.error('send_invite_email failed for %s: %s', email, exc)


def send_login_alert_email_task(email: str, first_name: str, device: str, ip_address: str, when: str):
    try:
        from core.services.email_service import send_login_alert_email
        send_login_alert_email(email, first_name, device, ip_address, when)
    except Exception as exc:
        logger.error('send_login_alert_email failed for %s: %s', email, exc)


def send_login_otp_email_task(email: str, code: str, first_name: str) -> bool:
    """Send the 2FA sign-in code. Returns True on success so the caller can
    fail closed (refuse to issue a challenge) when delivery fails in production."""
    try:
        from core.services.email_service import send_login_otp_email
        return send_login_otp_email(email, code, first_name)
    except Exception as exc:
        logger.error('send_login_otp_email failed for %s: %s', email, exc)
        return False

def _fuel_price(company=None):
    """The company's diesel R/L in use (own or official zone price), else the
    official inland price in force; None when unknown — never a hard-coded
    figure (QUOTE-RULES §1). Callers then leave the fuel-based figures as they
    were."""
    from core.services.fuel_price import company_diesel_price, price_in_force
    if company is not None:
        p = company_diesel_price(company)
        if p is not None:
            return p
    rec = price_in_force('INLAND')
    return Decimal(str(rec['price'])) if rec else None


# ---------------------------------------------------------------------------
# Individual score helpers
# ---------------------------------------------------------------------------

def _maintenance_score(vehicle) -> int:
    """
    0-100. Penalises overdue/approaching service based on km travelled,
    expired registration, and vehicles with no maintenance data.
    Falls back to date-based logic when km fields are not set.
    """
    today = date.today()
    score = 100

    # --- km-based service scheduling (preferred) ---
    has_km_data = (
        vehicle.service_interval_km
        and vehicle.last_service_mileage is not None
        and vehicle.mileage is not None
    )
    if has_km_data:
        km_since = float(vehicle.mileage) - float(vehicle.last_service_mileage)
        interval = float(vehicle.service_interval_km)
        km_overdue = km_since - interval
        if km_overdue >= 0:
            score -= 40  # already past service interval
        elif km_since >= interval * 0.9:
            score -= 15  # within 10% of interval (imminent)
        elif km_since >= interval * 0.75:
            score -= 5   # within 25% of interval (approaching)
    else:
        # --- date-based fallback for vehicles without km service data ---
        if vehicle.last_maintenance_date:
            days_since = (today - vehicle.last_maintenance_date).days
            if days_since > 180:
                score -= 40
            elif days_since > 90:
                score -= 20
            elif days_since > 60:
                score -= 10
        else:
            score -= 30  # no maintenance data at all

        if vehicle.next_maintenance_due:
            days_until = (vehicle.next_maintenance_due - today).days
            if days_until < 0:
                score -= 30
            elif days_until < 14:
                score -= 15
            elif days_until < 30:
                score -= 5

    if vehicle.insurance_expiry:
        days_until = (vehicle.insurance_expiry - today).days
        if days_until < 0:
            score -= 20
        elif days_until < 30:
            score -= 10

    if vehicle.registration_expiry:
        days_until = (vehicle.registration_expiry - today).days
        if days_until < 0:
            score -= 20
        elif days_until < 30:
            score -= 10

    return max(0, min(100, score))


def _uptime(vehicle, loads) -> tuple:
    """
    Returns (uptime_score: int 0-100, uptime_percentage: Decimal 0-100).
    Based on total trip days vs vehicle age in days.
    """
    completed = [l for l in loads if l.status in ('DELIVERED', 'INVOICED')]

    if not completed:
        return 0, Decimal('0.00')

    trip_days = 0
    for load in completed:
        if load.pickup_date and load.delivery_date:
            delta = (load.delivery_date.date() if hasattr(load.delivery_date, 'date') else load.delivery_date) \
                  - (load.pickup_date.date() if hasattr(load.pickup_date, 'date') else load.pickup_date)
            trip_days += max(1, delta.days + 1)
        else:
            trip_days += 1

    vehicle_age_days = max(30, (date.today() - vehicle.created_at.date()).days)
    raw_pct = min(100.0, (trip_days / vehicle_age_days) * 100)
    uptime_pct = Decimal(str(round(raw_pct, 2)))

    # Map percentage to score with a generous curve — heavy trucks aren't
    # running 100% of days; 40% utilisation is healthy for SA freight.
    pct = float(uptime_pct)
    if pct >= 60:
        score = 90 + min(10, int((pct - 60) / 4))
    elif pct >= 40:
        score = 70 + int((pct - 40) * 1.0)
    elif pct >= 20:
        score = 45 + int((pct - 20) * 1.25)
    elif pct >= 5:
        score = 20 + int((pct - 5) * 1.67)
    else:
        score = int(pct * 4)

    return min(100, max(0, score)), uptime_pct


def _fuel_efficiency_score(vehicle) -> int:
    """
    0-100. Compares vehicle's actual L/km to the vehicle-type benchmark.
    Lower consumption relative to benchmark = higher score.
    """
    actual = float(vehicle.fuel_consumption_per_km or 0)

    benchmark = 0.35  # default L/km for a heavy truck
    if vehicle.vehicle_type and vehicle.vehicle_type.fuel_consumption_l_per_100km:
        benchmark = float(vehicle.vehicle_type.fuel_consumption_l_per_100km) / 100

    if actual <= 0 or benchmark <= 0:
        return 50  # neutral — no data

    ratio = actual / benchmark
    if ratio <= 0.85:
        return 100
    elif ratio <= 0.95:
        return 88
    elif ratio <= 1.05:
        return 75
    elif ratio <= 1.15:
        return 60
    elif ratio <= 1.30:
        return 45
    elif ratio <= 1.50:
        return 30
    else:
        return 15


def _age_score(vehicle) -> int:
    """0-100. Newer vehicles score higher."""
    age = date.today().year - (vehicle.year or date.today().year)
    if age <= 2:   return 100
    elif age <= 4: return 88
    elif age <= 7: return 74
    elif age <= 10: return 58
    elif age <= 13: return 42
    elif age <= 16: return 28
    else:           return 15


def _economics(vehicle, loads) -> tuple:
    """
    Returns (cost_per_km: Decimal, margin_per_trip: Decimal).

    cost_per_km  = (fuel cost + recorded maintenance costs) / total km driven
    margin/trip  = avg(revenue - fuel_cost) per completed trip
    """
    from core.models import VehicleLog

    completed = [l for l in loads if l.status in ('DELIVERED', 'INVOICED')]
    fuel_per_km = vehicle.fuel_consumption_per_km or Decimal('0.35')
    fp = _fuel_price(getattr(vehicle, 'company', None))
    if fp is None:
        return None, None       # diesel price unknown: fuel-based economics not worked out

    # Total km from completed loads
    total_km = sum(Decimal(str(l.distance or 0)) for l in completed)

    # Maintenance / tyre / repair costs logged against this vehicle
    maint_total = VehicleLog.objects.filter(vehicle=vehicle).aggregate(
        total=Sum('cost')
    )['total'] or Decimal('0')

    if total_km > 0:
        fuel_cost_total = total_km * fuel_per_km * fp
        cost_per_km = (fuel_cost_total + Decimal(str(maint_total))) / total_km
    else:
        # No trips yet — show pure fuel cost per km
        cost_per_km = fuel_per_km * fp

    if completed:
        margins = []
        for load in completed:
            dist = Decimal(str(load.distance or 0))
            revenue = Decimal(str(load.total_amount or 0))
            trip_fuel = dist * fuel_per_km * fp
            margins.append(revenue - trip_fuel)
        margin_per_trip = sum(margins) / len(margins)
    else:
        margin_per_trip = Decimal('0')

    return round(cost_per_km, 2), round(margin_per_trip, 2)


# ---------------------------------------------------------------------------
# Main task
# ---------------------------------------------------------------------------

def compute_vehicle_scores(vehicle_id: int):
    """
    Compute and persist all performance scores for a single vehicle.
    Safe to call multiple times — always overwrites with fresh values.
    """
    from core.models import Vehicle, Load

    try:
        vehicle = Vehicle.objects.select_related('vehicle_type').get(pk=vehicle_id)
    except Vehicle.DoesNotExist:
        logger.warning('compute_vehicle_scores: vehicle %s not found', vehicle_id)
        return

    try:
        loads = list(Load.objects.filter(vehicle=vehicle))

        maint = _maintenance_score(vehicle)
        uptime_score, uptime_pct = _uptime(vehicle, loads)
        fuel = _fuel_efficiency_score(vehicle)
        age = _age_score(vehicle)
        cost_per_km, margin_per_trip = _economics(vehicle, loads)

        # Composite AI health score — weighted average
        ai_health = round(
            maint        * 0.35 +
            uptime_score * 0.25 +
            fuel         * 0.25 +
            age          * 0.15
        )

        updates = dict(maintenance_score=maint, uptime_score=uptime_score, uptime_percentage=uptime_pct,
                       fuel_efficiency_score=fuel, ai_health_score=ai_health)
        if cost_per_km is not None:
            updates.update(cost_per_km=cost_per_km, margin_per_trip=margin_per_trip)
        Vehicle.objects.filter(pk=vehicle_id).update(**updates)

        logger.info(
            'Vehicle %s scored: health=%d maint=%d uptime=%d(%s%%) fuel=%d age=%d cost/km=%s margin/trip=%s',
            vehicle.plate, ai_health, maint, uptime_score, uptime_pct, fuel, age, cost_per_km, margin_per_trip,
        )

    except Exception as exc:
        logger.exception('compute_vehicle_scores failed for vehicle %s', vehicle_id)


# ---------------------------------------------------------------------------
# Nightly batch task
# ---------------------------------------------------------------------------

def compute_all_vehicle_scores():
    """Recompute scores for every vehicle (run from management command or cron)."""
    from core.models import Vehicle

    ids = list(Vehicle.objects.values_list('pk', flat=True))
    for vid in ids:
        compute_vehicle_scores(vid)

    logger.info('compute_all_vehicle_scores: processed %d vehicles', len(ids))
    return len(ids)


# ---------------------------------------------------------------------------
# Driver score helpers
# ---------------------------------------------------------------------------

def _driver_on_time_rate(loads) -> float:
    delivered = [l for l in loads if l.status in ('DELIVERED', 'INVOICED')]
    if not delivered:
        return 0.0
    trackable = [l for l in delivered if l.actual_delivered_at]
    if not trackable:
        return 0.0
    on_time = 0
    for load in trackable:
        actual = load.actual_delivered_at.date() if hasattr(load.actual_delivered_at, 'date') else load.actual_delivered_at
        scheduled = load.delivery_date.date() if hasattr(load.delivery_date, 'date') else load.delivery_date
        if actual <= scheduled:
            on_time += 1
    return round((on_time / len(trackable)) * 100, 2)


def _driver_safety_score(driver) -> int:
    score = 100 - (driver.violation_count * 10) - (driver.accident_history * 20)
    return max(0, min(100, score))


def _driver_fuel_efficiency(loads) -> int:
    scores = [l.vehicle.fuel_efficiency_score for l in loads if l.vehicle and l.vehicle.fuel_efficiency_score]
    return round(sum(scores) / len(scores)) if scores else 0


def _driver_margin_per_trip(loads):
    from decimal import Decimal
    completed = [l for l in loads if l.status in ('DELIVERED', 'INVOICED')]
    if not completed:
        return Decimal('0')
    fp = _fuel_price(getattr(completed[0], 'company', None))
    if fp is None:
        return None     # diesel price unknown
    margins = []
    for load in completed:
        revenue = Decimal(str(load.total_amount or 0))
        dist = Decimal(str(load.distance or 0))
        fuel_per_km = (load.vehicle.fuel_consumption_per_km if load.vehicle and load.vehicle.fuel_consumption_per_km else Decimal('0.35'))
        margins.append(revenue - dist * fuel_per_km * fp)
    return round(sum(margins) / len(margins), 2)


def compute_driver_scores(driver_id: int):
    """Compute and persist all performance scores for a single driver."""
    from core.models import Driver, Load
    from decimal import Decimal
    from datetime import date

    try:
        driver = Driver.objects.get(pk=driver_id)
    except Driver.DoesNotExist:
        logger.warning('compute_driver_scores: driver %s not found', driver_id)
        return

    try:
        loads = list(Load.objects.filter(driver=driver).select_related('vehicle'))
        completed = [l for l in loads if l.status in ('DELIVERED', 'INVOICED')]

        total_revenue = sum(Decimal(str(l.total_amount or 0)) for l in completed)
        total_trips = len(completed)
        avg_rev = round(total_revenue / total_trips, 2) if total_trips else Decimal('0')
        total_dist = sum(Decimal(str(l.distance or 0)) for l in completed)

        today = date.today()
        month_start = today.replace(day=1)
        trips_this_month = sum(
            1 for l in completed
            if l.actual_delivered_at and l.actual_delivered_at.date() >= month_start
        )

        on_time = _driver_on_time_rate(loads)
        safety = _driver_safety_score(driver)
        fuel_eff = _driver_fuel_efficiency(loads)
        margin = _driver_margin_per_trip(loads)
        efficiency = round(on_time * 0.5 + safety * 0.3 + fuel_eff * 0.2)

        Driver.objects.filter(pk=driver_id).update(
            on_time_rate=Decimal(str(on_time)),
            safety_score=safety,
            efficiency_score=efficiency,
            total_distance=round(total_dist, 2),
            trips_this_month=trips_this_month,
            revenue_generated=round(total_revenue, 2),
            avg_revenue_per_trip=avg_rev,
            **({'margin_per_trip': margin} if margin is not None else {}),
        )

        logger.info(
            'Driver %s scored: efficiency=%d on_time=%.1f%% safety=%d trips=%d revenue=%.2f',
            driver_id, efficiency, on_time, safety, total_trips, total_revenue,
        )

    except Exception as exc:
        logger.exception('compute_driver_scores failed for driver %s', driver_id)


def compute_all_driver_scores():
    """Recompute scores for every driver (run from management command or cron)."""
    from core.models import Driver
    ids = list(Driver.objects.values_list('pk', flat=True))
    for did in ids:
        compute_driver_scores(did)
    logger.info('compute_all_driver_scores: processed %d drivers', len(ids))
    return len(ids)


# ---------------------------------------------------------------------------
# Fuel price refresh (Celery Beat — runs 3rd and 10th of every month)
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=3, default_retry_delay=21_600, name='core.tasks.refresh_fuel_price')
@track_task_run('refresh_fuel_price')
def refresh_fuel_price(self):
    """
    Fetch current SA diesel price from live sources (FIASA → AA SA → SAPIA → DMRE).
    Retries up to 3× with 6-hour gaps if the refresh fails.
    SA prices are announced on the first Wednesday of each month.

    A refresh "fails" when the stored row is a fallback placeholder OR when
    the live fetch failed and the previous good price was kept
    (``fetch_failed_at`` set — see fetch_fuel_prices' never-downgrade rule).
    Exactly one retry is scheduled per failed run: celery's Retry exception
    is re-raised untouched instead of being caught by the generic handler
    (which used to schedule a second retry for the same failure).
    """
    from celery.exceptions import Retry

    try:
        from core.services.fuel_price import fetch_fuel_prices
        from datetime import datetime as _dt
        from core.services.fuel_price import period_start
        fp = fetch_fuel_prices(force_update=True)
        if fp is None:
            logger.warning('refresh_fuel_price: no official price on record and the refresh failed — '
                           'will retry (attempt %d/3)', self.request.retries + 1)
            raise self.retry()
        eff = getattr(fp, 'effective_from', None)
        if isinstance(eff, _dt) and eff < period_start():
            # FIASA still shows last period's column (e.g. early on the
            # change Wednesday): not a success, try again later.
            logger.warning('refresh_fuel_price: official price in force is from %s, before this period — '
                           'will retry (attempt %d/3)', eff, self.request.retries + 1)
            raise self.retry()
        if fp.source in ('FALLBACK', 'FALLBACK_LATEST') or getattr(fp, 'fetch_failed_at', None):
            logger.warning(
                'refresh_fuel_price: live refresh failed (stored source=%s, kept=%s) — '
                'will retry (attempt %d/3)',
                fp.source, fp.source not in ('FALLBACK', 'FALLBACK_LATEST'),
                self.request.retries + 1,
            )
            raise self.retry()
        logger.info(
            'Fuel price refreshed: diesel_inland=R%.4f source=%s date=%s',
            fp.diesel_inland, fp.source, fp.date,
        )
        return {'diesel_inland': float(fp.diesel_inland), 'source': fp.source}
    except Retry:
        raise
    except self.MaxRetriesExceededError:
        logger.error('refresh_fuel_price: max retries exceeded — manual update required via Admin > Fuel Prices')
    except Exception as exc:
        logger.exception('refresh_fuel_price unexpected error: %s', exc)
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Win-probability model retraining (Celery Beat — nightly)
# ---------------------------------------------------------------------------

@shared_task(name='core.tasks.retrain_win_model')
@track_task_run('retrain_win_model')
def retrain_win_model():
    """Nightly retrain of the quote win-probability model from captured
    QuoteOutcome data. Idempotent: no-ops with a clear reason until both
    WIN_MODEL_MIN_ACCEPTED accepted AND WIN_MODEL_MIN_REJECTED rejected
    outcomes exist — an all-accepted dataset cannot train a classifier."""
    from core.services.quote_training import retrain_company_win_models, retrain_win_model as _retrain
    result = _retrain()
    if result.get('trained'):
        logger.info(
            'Win model retrained on %s outcomes (accuracy=%s, auc=%s)',
            result.get('samples'), result.get('accuracy'), result.get('auc'),
        )
    else:
        logger.info('Win model not retrained: %s', result.get('reason'))
    # Per-company tier (pricing analysis checks it before the user and global
    # tiers). Same nightly run, so no new Beat entry; additive result key.
    try:
        companies = retrain_company_win_models()
        logger.info('Company win models: %s considered, %s trained, %s skipped',
                    companies['considered'], companies['trained'], companies['skipped'])
        result['companies'] = {k: v for k, v in companies.items() if k != 'results'}
    except Exception as exc:  # never fail the global retrain over the company sweep
        logger.warning('Company win-model sweep failed: %s', exc)
    return result


# ---------------------------------------------------------------------------
# Verified rates for the AI quote price check (Celery Beat — monthly)
# ---------------------------------------------------------------------------

@shared_task(name='core.tasks.refresh_verified_rates')
@track_task_run('refresh_verified_rates')
def refresh_verified_rates(kinds=None, sanral_classes=None, triggered_by_id=None):
    """Look up the current SANRAL toll tariffs and driver allowance (OpenAI
    web search + source-page check) and write PENDING proposals for figures
    that changed. Never applies anything: a superuser approves proposals
    (/api/v1/admin/verified-rates/). Skips cleanly when switched off, without
    an OpenAI key, or over the platform daily budget."""
    from core.models import User
    from core.services.verified_rate_refresh import KINDS, run_refresh

    user = User.objects.filter(id=triggered_by_id).first() if triggered_by_id else None
    summary = run_refresh(kinds=tuple(kinds or KINDS), sanral_classes=sanral_classes, triggered_by=user)
    logger.info('refresh_verified_rates: %s', {k: v for k, v in summary.items() if k != 'unverified'})
    return summary


@shared_task(name='core.tasks.train_user_win_model')
def train_user_win_model(user_id):
    """Event-driven, debounced per-user win-model retrain — enqueued by
    core.services.ml_training_queue.schedule_user_retrain() right after an
    outcome is recorded for that user (called from record_quote_outcome, not
    a signal). Not in TRACKED_TASKS/track_task_run: this fires many times a
    day per active user, not once on a fixed schedule, so per-run staleness
    tracking doesn't apply — sweep_user_win_model_training below is the
    scheduled safety net that IS tracked.

    Clears its own debounce flag at the START, not the end, so an outcome
    landing mid-training re-opens a fresh debounce window instead of being
    silently dropped while the flag was still held.
    """
    from core.services.ml_training_queue import clear_queued_flag, mark_retrain_finished
    from core.services.quote_training import retrain_win_model_for_scope

    clear_queued_flag(user_id)
    try:
        result = retrain_win_model_for_scope('user', user_id=user_id)
        if result.get('trained'):
            logger.info(
                'User %s win model retrained on %s outcomes (algo=%s, auc=%s)',
                user_id, result.get('samples'), result.get('algorithm'), result.get('auc'),
            )
        else:
            logger.info('User %s win model not retrained: %s', user_id, result.get('reason'))
        return result
    finally:
        mark_retrain_finished(user_id)


@shared_task(name='core.tasks.sweep_user_win_model_training')
@track_task_run('sweep_user_win_model_training')
def sweep_user_win_model_training():
    """Nightly safety net for the per-user win model (Beat: 04:00 SAST, after
    the global retrain above). The event-driven path covers the normal case;
    this catches outcomes ever written outside record_quote_outcome(),
    lost/failed per-user tasks, or a user crossing the sample threshold via a
    data backfill. Only (re-)enqueues a user whose qualifying-outcome count
    grew meaningfully since their last MLModelVersion, or who qualifies but
    has no model yet — not a blind nightly refit of every user.
    """
    from django.db.models import Count
    from core.models import MLModelVersion
    from core.services.ml_training_queue import schedule_user_retrain
    from core.services.quote_training import _min_class_counts, closed_outcomes

    min_accepted, min_rejected = _min_class_counts('user')
    counts = (
        closed_outcomes().filter(created_by__isnull=False)
        .values('created_by_id').annotate(n=Count('id'))
    )
    scheduled = 0
    for row in counts:
        user_id, n = row['created_by_id'], row['n']
        # Cheap combined-total skip; the real accepted/rejected gate is
        # enforced inside retrain_win_model_for_scope (via schedule_user_retrain).
        if n < min_accepted + min_rejected:
            continue
        latest = MLModelVersion.objects.filter(scope='user', user_id=user_id).order_by('-created_at').first()
        if latest is None or n >= (latest.training_sample_count or 0) + 5:
            if schedule_user_retrain(user_id, delay_seconds=0):
                scheduled += 1
    logger.info('sweep_user_win_model_training: scheduled %s user retrain(s)', scheduled)
    return {'scheduled': scheduled}


@shared_task(name='core.tasks.reindex_copilot_rag')
def reindex_copilot_rag():
    """Refresh the Copilot RAG invoice embeddings for every company off the chat
    request path. Incremental: index_company_invoices skips unchanged invoices
    (source_hash), so this is cheap between real changes. No-ops when RAG is
    disabled (OPENAI_API_KEY / openai / numpy missing)."""
    from core.models import Company
    from core.services import rag
    if not rag.rag_enabled():
        logger.info('Copilot RAG reindex skipped: RAG not enabled.')
        return {'indexed': 0, 'enabled': False}
    total = 0
    for company in Company.objects.all():
        try:
            total += rag.index_company_invoices(company)
        except Exception:
            logger.exception('Copilot RAG reindex failed for company %s', company.id)
    logger.info('Copilot RAG reindex complete: %s invoice embeddings written.', total)
    return {'indexed': total, 'enabled': True}


@shared_task(name='core.tasks.poll_cartrack_vehicle_status')
def poll_cartrack_vehicle_status():
    """Poll GET /vehicles/status from Cartrack for every company that has
    credentials configured, updating each Vehicle's live location fields.
    One company's failure never blocks the others."""
    from core.models import Company
    from core.services.cartrack_sync import poll_vehicle_status

    companies_polled = 0
    total_matched = 0
    for company in Company.objects.exclude(cartrack_username__isnull=True).exclude(cartrack_username=''):
        try:
            result = poll_vehicle_status(company)
            companies_polled += 1
            total_matched += result['matched']
        except Exception:
            logger.exception('Cartrack vehicle-status poll failed for company %s', company.id)
    return {'companies_polled': companies_polled, 'vehicles_matched': total_matched}


@shared_task(name='core.tasks.poll_ctrlfleet_positions')
def poll_ctrlfleet_positions():
    """Poll POST /vehicles/positions from CtrlFleet for every company that has
    a key configured, updating each linked Vehicle's live location fields.
    One company's failure never blocks the others."""
    from core.models import Company
    from core.services.ctrlfleet_sync import sync_ctrlfleet_positions

    companies_polled = 0
    total_updated = 0
    for company in Company.objects.exclude(ctrlfleet_api_key__isnull=True).exclude(ctrlfleet_api_key=''):
        try:
            result = sync_ctrlfleet_positions(company)
            companies_polled += 1
            total_updated += result['updated']
        except Exception:
            logger.exception('CtrlFleet position poll failed for company %s', company.id)
    return {'companies_polled': companies_polled, 'vehicles_updated': total_updated}


@shared_task(name='core.tasks.refresh_fleet_fuel_actuals')
def refresh_fleet_fuel_actuals(company_id=None):
    """Weekly: re-measure fuel use per truck / vehicle type from the fleet
    tracker for every company with a CONNECTED Cartrack account (or just
    `company_id`, from "Refresh now"). Disconnected or CtrlFleet-only
    companies get a 'skipped' run. One company's failure never blocks the
    others. Pricing only ever reads the stored rows."""
    from core.models import Company
    from core.services.fleet_fuel_actuals import refresh_company

    qs = Company.objects.all()
    if company_id is not None:
        qs = qs.filter(id=company_id)
    else:
        qs = (qs.exclude(cartrack_username__isnull=True).exclude(cartrack_username='')
              .exclude(cartrack_connected_at__isnull=True))
    done = {}
    for company in qs:
        try:
            done[company.id] = refresh_company(company).status
        except Exception:
            logger.exception('Fleet fuel refresh failed for company %s', company.id)
            done[company.id] = 'failed'
        finally:
            if company_id is not None:
                from django.core.cache import cache
                cache.delete(f'fleet-fuel-refresh:{company.id}')
    return done


@shared_task(name='core.tasks.poll_cartrack_door_events')
def poll_cartrack_door_events():
    """Poll GET /topics/vehicles/door from Cartrack for every company that has
    credentials configured. A company without the DOOR topic granted (403)
    fails independently of vehicle-status polling and of other companies."""
    from core.models import Company
    from core.services.cartrack_sync import poll_door_events

    companies_polled = 0
    total_applied = 0
    for company in Company.objects.exclude(cartrack_username__isnull=True).exclude(cartrack_username=''):
        try:
            result = poll_door_events(company)
            companies_polled += 1
            total_applied += result['applied']
        except Exception:
            logger.exception('Cartrack door-event poll failed for company %s', company.id)
    return {'companies_polled': companies_polled, 'events_applied': total_applied}


# ---------------------------------------------------------------------------
# Notification sweeps (Celery Beat — daily; weekly digest Mondays)
# See core/services/notification_sweeps.py; same logic is runnable manually
# via the matching management commands.
# ---------------------------------------------------------------------------

@shared_task(name='core.tasks.retry_delivery_fee_charges')
@track_task_run('retry_delivery_fee_charges')
def retry_delivery_fee_charges():
    """Daily retry of failed 0.25% delivery take-rate charges. A company whose
    charge fails moves into grace_period here; the actual suspension once
    DELIVERY_FEE_GRACE_DAYS runs out belongs to check_grace_period_expirations."""
    from core.services.delivery_fee_billing import retry_failed_delivery_fee_charges
    summary = retry_failed_delivery_fee_charges()
    if summary['entered_grace']:
        logger.warning(
            'Delivery fee retry: %s company(ies) entered grace period', summary['entered_grace']
        )
    if summary['dead_authorization']:
        logger.error(
            'Delivery fee retry: cleared %s permanently-invalid card authorization(s)',
            summary['dead_authorization'],
        )
    return summary


@shared_task(name='core.tasks.run_monthly_subscription_billing')
@track_task_run('run_monthly_subscription_billing')
def run_monthly_subscription_billing():
    """Daily sweep: charge the flat monthly fee for every company whose
    next_billing_date has arrived."""
    from core.services.subscription_billing import run_monthly_subscription_billing as _run
    return _run()


@shared_task(name='core.tasks.check_grace_period_expirations')
@track_task_run('check_grace_period_expirations')
def check_grace_period_expirations():
    """Daily sweep: suspend any company whose grace period has expired with
    no successful charge (TruckWys_Fee_Billing_Spec.pdf §4)."""
    from core.services.subscription_billing import check_grace_period_expirations as _check
    summary = _check()
    if summary['suspended']:
        logger.warning('Grace-period check: %s company(ies) suspended', summary['suspended'])
    return summary


@shared_task(name='core.tasks.check_pending_cancellations')
@track_task_run('check_pending_cancellations')
def check_pending_cancellations():
    """Daily sweep: finalise any company that cancelled while still
    active/grace_period once the period they already paid for has ended."""
    from core.services.subscription_billing import check_pending_cancellations as _check
    summary = _check()
    if summary['cancelled']:
        logger.warning('Pending-cancellation check: %s company(ies) cancelled', summary['cancelled'])
    return summary


@shared_task(name='core.tasks.sweep_overdue_invoices')
def sweep_overdue_invoices():
    from core.services.notification_sweeps import sweep_overdue_invoices as run
    return run()


@shared_task(name='core.tasks.sweep_maintenance_due')
def sweep_maintenance_due():
    from core.services.notification_sweeps import sweep_maintenance_due as run
    return run()


@shared_task(name='core.tasks.sweep_expired_quotes')
def sweep_expired_quotes():
    from core.services.notification_sweeps import sweep_expired_quotes as run
    return run()


@shared_task(name='core.tasks.send_weekly_summaries')
def send_weekly_summaries():
    from core.services.notification_sweeps import send_weekly_summaries as run
    return run()


@shared_task(name='core.tasks.sweep_driver_documents')
def sweep_driver_documents():
    from core.services.notification_sweeps import sweep_driver_documents as run
    return run()


@shared_task(name='core.tasks.sweep_vehicle_documents')
def sweep_vehicle_documents():
    from core.services.notification_sweeps import sweep_vehicle_documents as run
    return run()


@shared_task(name='core.tasks.sweep_intelligence_recommendations')
def sweep_intelligence_recommendations():
    from core.services.notification_sweeps import sweep_intelligence_recommendations as run
    return run()


@shared_task(name='core.tasks.sweep_stale_activity_logs')
@track_task_run('sweep_stale_activity_logs')
def sweep_stale_activity_logs():
    """Daily trim of UserActivityLog past ACTIVITY_LOG_RETENTION_DAYS."""
    from core.services.activity_retention import sweep_stale_activity_logs as run
    return run()


# ---------------------------------------------------------------------------
# Demo company reset (Celery Beat — nightly, before the sweeps above)
# ---------------------------------------------------------------------------

@shared_task(name='core.tasks.reset_demo_company_task')
@track_task_run('reset_demo_company_task')
def reset_demo_company_task():
    """Runs frequently (see config/settings.py's CELERY_BEAT_SCHEDULE) but
    only actually wipes-and-reseeds the shared public demo company's
    fleet/quote/order data once it's been idle for an hour since the last
    real activity — see core.services.demo_seed.reset_demo_company_if_idle().
    A visit-free stretch is a no-op: nothing to reset. Never touches the
    Company row or the demo login (demo@truckwys.com), only the data
    around it."""
    try:
        from core.services.demo_seed import reset_demo_company_if_idle
        summary = reset_demo_company_if_idle()
        if summary is None:
            return {'reset': False}
        logger.info(
            'Demo company reset: company_id=%s vehicles=%s drivers=%s customers=%s quotes=%s loads=%s',
            summary['company'].pk, summary['vehicles'], summary['drivers'],
            summary['customers'], summary['quotes'], summary['loads'],
        )
        return {'reset': True, 'company_id': summary['company'].pk}
    except Exception:
        logger.exception('reset_demo_company_task failed')


# ---------------------------------------------------------------------------
# Scheduler dead-man's-switch
# ---------------------------------------------------------------------------

@shared_task(name='core.tasks.alert_stale_scheduled_tasks')
def alert_stale_scheduled_tasks():
    """Email the superusers when a tracked scheduled task has gone quiet or is
    failing every run.

    This exists because both of those failures are invisible otherwise. Beat
    froze twice in Sept 2026 with the container reporting healthy and nothing
    in the log, and retry_delivery_fee_charges raised KeyError on every single
    run for days — in both cases the only symptom was work silently not
    happening, spotted days later by hand.

    Deliberately NOT wrapped in @track_task_run: it would be watching itself,
    and a row saying "the watchdog ran" adds nothing. It is also not the
    primary defence — this task rides on the same beat that it is monitoring,
    so a fully dead beat takes the alert with it. The beat watchdog in
    docker-entrypoint.sh is what catches that; this catches the narrower case
    of one task being broken while beat is fine.
    """
    from django.contrib.auth import get_user_model
    from core.services.task_run import stale_tracked_tasks

    problems = stale_tracked_tasks()
    if not problems:
        return {'stale': 0}

    lines = '\n'.join(f'- {name}: {reason}' for name, reason in problems)
    logger.error('Scheduled task health check found %s problem(s):\n%s', len(problems), lines)

    title = f'{len(problems)} scheduled task(s) need attention'
    message = (
        'The following Celery beat tasks have not run recently, or their last run failed:\n\n'
        f'{lines}\n\n'
        'Check the Job Health panel in the admin dashboard, then the beat container '
        '(docker compose -f docker-compose.prod.yml logs --tail=100 beat).'
    )

    recipients = get_user_model().objects.filter(
        is_superuser=True, is_active=True
    ).exclude(email='')
    sent = 0
    for user in recipients:
        try:
            from core.services.email_service import send_notification_email
            if send_notification_email(user, title, message, link='/admin-dashboard'):
                sent += 1
        except Exception:
            # An alert that raises is worse than one that logs — the ERROR
            # line above is already on record either way.
            logger.exception('alert_stale_scheduled_tasks: could not email %s', user.pk)
    return {'stale': len(problems), 'notified': sent, 'tasks': [n for n, _ in problems]}


# ---------------------------------------------------------------------------
# Fast Pay (Capital) book automation — job bodies in core.capital.jobs
# ---------------------------------------------------------------------------

@shared_task(name='core.tasks.capital_process_queue')
def capital_process_queue(funder_id=None):
    """Release queued advances / top up part-funded ones. With ``funder_id``
    (fired by core.capital.queue.capacity_freed after a commit) only that
    funder runs and no TaskRunLog row is written (it fires often); the beat
    run (no argument) covers every funder and is tracked."""
    from core.capital import jobs
    if funder_id is not None:
        return jobs.process_queue_for(int(funder_id))
    return track_task_run('capital_process_queue')(jobs.process_queues)()


@shared_task(name='core.tasks.capital_monitor')
@track_task_run('capital_monitor')
def capital_monitor():
    from core.capital import jobs
    return jobs.monitor()


@shared_task(name='core.tasks.capital_nightly_rescore')
@track_task_run('capital_nightly_rescore')
def capital_nightly_rescore():
    from core.capital import jobs
    return jobs.nightly_rescore()


@shared_task(name='core.tasks.capital_reconcile')
@track_task_run('capital_reconcile')
def capital_reconcile():
    from core.capital import jobs
    return jobs.reconcile_all()


@shared_task(name='core.tasks.capital_monthly_data_room')
@track_task_run('capital_monthly_data_room')
def capital_monthly_data_room(period=None):
    from core.capital import jobs
    return jobs.monthly_data_room(period)


# Accounting integrations (Xero, QuickBooks Online): registered here so
# autodiscovery and the beat schedule find them.
from core.accounting.tasks import (  # noqa: E402,F401
    poll_all_payments, poll_connection, process_webhooks, push_link, reconcile_all,
    reconcile_connection, retry_due, run_backfill,
)
