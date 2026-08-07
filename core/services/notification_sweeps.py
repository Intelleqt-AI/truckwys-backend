"""Scheduled notification sweeps.

Shared by the management commands (manual/cron runs) and the Celery beat
tasks in core/tasks.py. Each function is idempotent per day and returns a
small summary dict for logging.
"""
import logging
from datetime import date, timedelta

from django.core.cache import cache
from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

MAINTENANCE_WINDOW_DAYS = 7   # alert when due within this many days
MAINTENANCE_REALERT_DAYS = 7  # re-alert a vehicle at most this often
DOCUMENT_WINDOW_DAYS = 30     # alert when a compliance document expires within this many days
DOCUMENT_REALERT_DAYS = 7     # re-alert a driver/vehicle at most this often


def sweep_overdue_invoices():
    """Flip invoices past due_date to OVERDUE. The save() fires the existing
    invoice.overdue signal (bell + gated email/push). Previously this only
    happened when an overdue invoice was incidentally re-saved."""
    from core.models import Invoice
    flipped = 0
    candidates = Invoice.objects.filter(
        status__in=('SENT', 'VIEWED', 'PARTIALLY_PAID'),
        due_date__lt=date.today(),
        balance__gt=0,
    )
    for invoice in candidates.iterator():
        try:
            invoice.save()  # Invoice.save() auto-flips to OVERDUE
            flipped += 1
        except Exception as exc:
            logger.warning('overdue sweep: invoice %s failed: %s', invoice.pk, exc)
    return {'flipped': flipped}


def sweep_maintenance_due():
    """Notify per vehicle whose next_maintenance_due falls within the window,
    at most once per MAINTENANCE_REALERT_DAYS (event: maintenance.due)."""
    from core.models import Vehicle
    from core.services.notify import notify_company
    today = date.today()
    horizon = today + timedelta(days=MAINTENANCE_WINDOW_DAYS)
    realert_before = today - timedelta(days=MAINTENANCE_REALERT_DAYS)
    notified = 0
    candidates = Vehicle.objects.filter(
        next_maintenance_due__isnull=False,
        next_maintenance_due__lte=horizon,
        company__isnull=False,
    ).filter(
        Q(last_maintenance_alert_at__isnull=True) |
        Q(last_maintenance_alert_at__lte=realert_before)
    )
    for vehicle in candidates.iterator():
        due = vehicle.next_maintenance_due
        overdue = due < today
        detail = (f"{vehicle.make} {vehicle.model} ({vehicle.plate}) — maintenance "
                  + (f"overdue since {due}" if overdue else f"due {due}"))
        try:
            notify_company(
                vehicle.company_id, 'ALERT' if overdue else 'WARNING',
                'Vehicle maintenance due', detail,
                link=f'/fleet/vehicles/{vehicle.id}', event='maintenance.due',
            )
            Vehicle.objects.filter(pk=vehicle.pk).update(last_maintenance_alert_at=today)
            notified += 1
        except Exception as exc:
            logger.warning('maintenance sweep: vehicle %s failed: %s', vehicle.pk, exc)
    return {'notified': notified}


def sweep_driver_documents():
    """Notify per driver whose license or medical card expires within the
    window, at most once per DOCUMENT_REALERT_DAYS (event: driver.document_expiring)."""
    from core.models import Driver
    from core.services.notify import notify_company
    today = date.today()
    horizon = today + timedelta(days=DOCUMENT_WINDOW_DAYS)
    realert_before = today - timedelta(days=DOCUMENT_REALERT_DAYS)
    notified = 0
    for field, alert_field, label in (
        ('license_expiry', 'license_alert_at', 'license'),
        ('medical_card_expiry', 'medical_card_alert_at', 'medical card'),
    ):
        candidates = Driver.objects.filter(
            **{f'{field}__isnull': False, f'{field}__lte': horizon},
            company__isnull=False,
        ).filter(Q(**{f'{alert_field}__isnull': True}) | Q(**{f'{alert_field}__lte': realert_before}))
        for driver in candidates.select_related('user').iterator():
            expiry = getattr(driver, field)
            overdue = expiry < today
            name = driver.user.get_full_name() or driver.user.username
            detail = (f"{name}'s {label} " + (f"expired {expiry}" if overdue else f"expires {expiry}"))
            try:
                notify_company(
                    driver.company_id, 'ALERT' if overdue else 'WARNING',
                    f'Driver {label} expiring', detail,
                    link=f'/fleet/drivers/{driver.id}', event='driver.document_expiring',
                )
                Driver.objects.filter(pk=driver.pk).update(**{alert_field: today})
                notified += 1
            except Exception as exc:
                logger.warning('driver document sweep: driver %s failed: %s', driver.pk, exc)
    return {'notified': notified}


def sweep_vehicle_documents():
    """Notify per vehicle whose insurance or registration expires within the
    window, at most once per DOCUMENT_REALERT_DAYS (event: vehicle.document_expiring)."""
    from core.models import Vehicle
    from core.services.notify import notify_company
    today = date.today()
    horizon = today + timedelta(days=DOCUMENT_WINDOW_DAYS)
    realert_before = today - timedelta(days=DOCUMENT_REALERT_DAYS)
    notified = 0
    for field, alert_field, label in (
        ('insurance_expiry', 'insurance_alert_at', 'insurance'),
        ('registration_expiry', 'registration_alert_at', 'registration'),
    ):
        candidates = Vehicle.objects.filter(
            **{f'{field}__isnull': False, f'{field}__lte': horizon},
            company__isnull=False,
        ).filter(Q(**{f'{alert_field}__isnull': True}) | Q(**{f'{alert_field}__lte': realert_before}))
        for vehicle in candidates.iterator():
            expiry = getattr(vehicle, field)
            overdue = expiry < today
            detail = (f"{vehicle.make} {vehicle.model} ({vehicle.plate}) — {label} "
                      + (f"expired {expiry}" if overdue else f"expires {expiry}"))
            try:
                notify_company(
                    vehicle.company_id, 'ALERT' if overdue else 'WARNING',
                    f'Vehicle {label} expiring', detail,
                    link=f'/fleet/vehicles/{vehicle.id}', event='vehicle.document_expiring',
                )
                Vehicle.objects.filter(pk=vehicle.pk).update(**{alert_field: today})
                notified += 1
            except Exception as exc:
                logger.warning('vehicle document sweep: vehicle %s failed: %s', vehicle.pk, exc)
    return {'notified': notified}


def sweep_expired_quotes():
    """Mark SENT quotes past valid_until as EXPIRED. The status transition
    fires the quote.expired notify via the Quote post_save signal."""
    from core.models import Quote
    expired = 0
    candidates = Quote.objects.filter(status='SENT', valid_until__lt=date.today())
    for quote in candidates.iterator():
        try:
            quote.status = 'EXPIRED'
            quote.save(update_fields=['status', 'updated_at'])
            expired += 1
        except Exception as exc:
            logger.warning('quote expiry sweep: quote %s failed: %s', quote.pk, exc)
    return {'expired': expired}


def send_weekly_summaries():
    """Email last ISO week's performance digest to users who opted in
    (email.weekly_reports). Idempotent per company per ISO week via cache."""
    from core.models import Company, Load, Quote, Invoice, Payment, User
    from core.services.notification_prefs import should_notify
    from core.services.email_service import send_weekly_summary_email

    now = timezone.now()
    week_end = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)  # this Monday 00:00
    week_start = week_end - timedelta(days=7)
    isoweek = week_start.date().isocalendar()
    sent = 0

    for company in Company.objects.all():
        recipients = [u for u in User.objects.filter(company=company, is_active=True)
                      if should_notify(u, 'email', 'weekly_reports')]
        if not recipients:
            continue
        cache_key = f"weekly_digest_{company.id}_{isoweek.year}w{isoweek.week}"
        if cache.get(cache_key):
            continue

        in_week = {'created_at__gte': week_start, 'created_at__lt': week_end}
        stats = {
            'week_start': week_start.date(),
            'week_end': (week_end - timedelta(days=1)).date(),
            'bookings_created': Load.objects.filter(company=company, **in_week).count(),
            'bookings_delivered': Load.objects.filter(
                company=company, actual_delivered_at__gte=week_start,
                actual_delivered_at__lt=week_end).count(),
            'quotes_sent': Quote.objects.filter(
                company=company, status__in=('SENT', 'ACCEPTED', 'DECLINED', 'EXPIRED'),
                **in_week).count(),
            'quotes_accepted': Quote.objects.filter(
                company=company, accepted_at__gte=week_start,
                accepted_at__lt=week_end).count(),
            'invoiced_total': Invoice.objects.filter(company=company, **in_week)
                .aggregate(s=Sum('total_amount'))['s'] or 0,
            'collected_total': Payment.objects.filter(company=company, **in_week)
                .aggregate(s=Sum('amount'))['s'] or 0,
        }
        delivered_any = False
        for user in recipients:
            if send_weekly_summary_email(user, company, stats):
                delivered_any = True
                sent += 1
        if delivered_any:
            cache.set(cache_key, True, 6 * 24 * 3600)  # block re-sends for 6 days
    return {'emails_sent': sent}


def sweep_intelligence_recommendations():
    """Route HIGH/MEDIUM-severity BI recommendations through notify_company,
    once per company per finding per DOCUMENT_REALERT_DAYS-equivalent window
    (event: intelligence.recommendation). Recommendations have no persistent
    id, so dedup by a hash of company + title rather than a model field."""
    import hashlib
    from core.models import Company
    from core.services.notify import notify_company
    from core.services.intelligence import IntelligenceService
    type_map = {'HIGH': 'ALERT', 'MEDIUM': 'WARNING'}
    notified = 0
    for company in Company.objects.all():
        try:
            recs = IntelligenceService(company).generate_recommendations()
        except Exception as exc:
            logger.warning('intelligence sweep: company %s failed: %s', company.pk, exc)
            continue
        for rec in recs:
            severity = rec.get('severity', 'LOW')
            if severity not in type_map:
                continue
            digest = hashlib.md5(f"{company.id}:{rec['title']}".encode()).hexdigest()
            cache_key = f'intel_rec_{digest}'
            if cache.get(cache_key):
                continue
            notify_company(
                company.id, type_map[severity], rec['title'], rec.get('message', ''),
                link=rec.get('link', ''), event='intelligence.recommendation',
            )
            cache.set(cache_key, True, 7 * 24 * 3600)
            notified += 1
    return {'notified': notified}
