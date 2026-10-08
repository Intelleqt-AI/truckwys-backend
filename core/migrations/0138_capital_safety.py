# Hand-written (capital-safety 2026-10). Depends on the tip of the foundation
# accounting chain (0137).
#
# Schema:
#   Load            + POD evidence metadata (captured_at, lat/lng, device,
#                     source, server-computed file SHA-256)
#   Facility        + reserved, and CheckConstraints outstanding >= 0,
#                     reserved >= 0, outstanding + reserved <= limit
#   AdvanceRequest  + capacity_reserved, settlement evidence
#                     (settlement_reference, settlement_payment, settled_by),
#                     and a partial UniqueConstraint: one active advance per invoice
#   IntegrationAPIKey + allowed_companies (lender key -> transporters binding)
#
# Data (both RunPython steps are idempotent; re-running converges on the same
# result):
#   dedupe_active_advances  runs before the unique constraint is added
#   reserve_open_advances   runs before the facility constraints are added

import logging
from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count, F, Q

logger = logging.getLogger('django')

ACTIVE = ['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']
UNDISBURSED = ['REQUESTED', 'SCORING', 'APPROVED']
DEDUPE_NOTE = 'Auto-cancelled by migration 0138: duplicate active advance on this invoice.'


def _flush_deferred(schema_editor):
    # Django FKs on Postgres are DEFERRABLE INITIALLY DEFERRED; firing any
    # queued checks now avoids "pending trigger events" when the following
    # AddConstraint alters the same table inside this transaction.
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')


def dedupe_active_advances(apps, schema_editor):
    """Leave at most one active advance per invoice.

    Keeps the DISBURSED one if there is one (money is out; cancelling it would
    hide real exposure), otherwise the newest. Older undisbursed duplicates
    are marked CANCELLED with a note and lose any reservation. Two DISBURSED
    advances on one invoice means the invoice was really funded twice — that
    needs a human, so the migration stops and names the invoice instead of
    guessing which payout to hide.
    """
    AdvanceRequest = apps.get_model('core', 'AdvanceRequest')
    dupes = (AdvanceRequest.objects.filter(status__in=ACTIVE)
             .values('invoice_id').annotate(n=Count('id')).filter(n__gt=1))
    double_funded = []
    for row in dupes:
        rows = list(AdvanceRequest.objects.filter(
            invoice_id=row['invoice_id'], status__in=ACTIVE).order_by('-created_at', '-id'))
        disbursed = [r for r in rows if r.status == 'DISBURSED']
        if len(disbursed) > 1:
            double_funded.append(row['invoice_id'])
            continue
        keep = disbursed[0] if disbursed else rows[0]
        for adv in rows:
            if adv.pk == keep.pk:
                continue
            notes = f'{adv.notes}\n{DEDUPE_NOTE}'.strip() if adv.notes else DEDUPE_NOTE
            AdvanceRequest.objects.filter(pk=adv.pk).update(
                status='CANCELLED', notes=notes, capacity_reserved=Decimal('0.00'))
            logger.warning('0138: cancelled duplicate advance %s on invoice %s (kept %s)',
                           adv.pk, row['invoice_id'], keep.pk)
    if double_funded:
        raise RuntimeError(
            'Migration 0138 stopped: invoices with more than one DISBURSED advance '
            f'(funded twice): {sorted(double_funded)}. Resolve these by hand '
            '(settle or write off the extra advance), then re-run migrate.')
    _flush_deferred(schema_editor)


def reserve_open_advances(apps, schema_editor):
    """Rebuild Facility.reserved from the advances that should hold capacity.

    Recomputed from scratch every run (idempotent): all undisbursed active
    advances (REQUESTED/SCORING/APPROVED) are allocated oldest-first against
    each facility's headroom (limit - outstanding). An advance is reserved in
    full or not at all; one that does not fit is logged and left holding 0 —
    the facility ledger then reserves it at approve/disburse time, or refuses
    if the capacity is still not there. That cap is what keeps
    outstanding + reserved <= limit true so the CheckConstraint can be added.

    A facility whose outstanding already exceeds its limit cannot be repaired
    without changing a credit figure, so the migration stops and names it.
    """
    Facility = apps.get_model('core', 'Facility')
    AdvanceRequest = apps.get_model('core', 'AdvanceRequest')

    over = list(Facility.objects.filter(outstanding__gt=F('limit')).values_list('id', flat=True))
    negative = list(Facility.objects.filter(outstanding__lt=0).values_list('id', flat=True))
    if over or negative:
        raise RuntimeError(
            'Migration 0138 stopped: facilities with outstanding above limit '
            f'{sorted(over)} or below zero {sorted(negative)}. Correct the limit or '
            'outstanding by hand, then re-run migrate.')

    AdvanceRequest.objects.exclude(status__in=UNDISBURSED).exclude(
        capacity_reserved=Decimal('0.00')).update(capacity_reserved=Decimal('0.00'))

    for facility in Facility.objects.all():
        headroom = facility.limit - facility.outstanding
        reserved = Decimal('0.00')
        for adv in AdvanceRequest.objects.filter(
                facility_id=facility.pk, status__in=UNDISBURSED).order_by('created_at', 'id'):
            if adv.amount <= headroom - reserved:
                hold = adv.amount
                reserved += hold
            else:
                hold = Decimal('0.00')
                logger.warning(
                    '0138: facility %s has no headroom for advance %s (R%s); left unreserved',
                    facility.pk, adv.pk, adv.amount)
            if adv.capacity_reserved != hold:
                AdvanceRequest.objects.filter(pk=adv.pk).update(capacity_reserved=hold)
        if facility.reserved != reserved:
            Facility.objects.filter(pk=facility.pk).update(reserved=reserved)
    _flush_deferred(schema_editor)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0137_foundation_backfill'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # --- POD evidence -------------------------------------------------
        migrations.AddField(
            model_name='load',
            name='pod_captured_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='pod_latitude',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='pod_longitude',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='pod_device',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='load',
            name='pod_source',
            field=models.CharField(blank=True, choices=[('CAMERA', 'Camera'), ('LIBRARY', 'Photo library'), ('UPLOAD', 'File upload'), ('UNKNOWN', 'Unknown')], default='', max_length=10),
        ),
        migrations.AddField(
            model_name='load',
            name='pod_file_sha256',
            field=models.CharField(blank=True, max_length=64),
        ),

        # --- Facility reservation ------------------------------------------
        migrations.AddField(
            model_name='facility',
            name='reserved',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='Capacity reserved by requested/approved (undisbursed) advances in ZAR', max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))]),
        ),

        # --- Advance capacity + settlement evidence ------------------------
        migrations.AddField(
            model_name='advancerequest',
            name='capacity_reserved',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='Amount of facility capacity held by this advance', max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))]),
        ),
        migrations.AddField(
            model_name='advancerequest',
            name='settlement_reference',
            field=models.CharField(blank=True, help_text='Bank/payment reference proving the debtor paid', max_length=200),
        ),
        migrations.AddField(
            model_name='advancerequest',
            name='settlement_payment',
            field=models.ForeignKey(blank=True, help_text='Recorded payment on the advanced invoice that settled it', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='settled_advances', to='core.payment'),
        ),
        migrations.AddField(
            model_name='advancerequest',
            name='settled_by',
            field=models.ForeignKey(blank=True, help_text='Staff user who settled the advance (null = system)', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='settled_advances', to=settings.AUTH_USER_MODEL),
        ),

        # --- Lender key binding --------------------------------------------
        migrations.AddField(
            model_name='integrationapikey',
            name='allowed_companies',
            field=models.ManyToManyField(blank=True, help_text='Transporters this LENDER key may see and fund. Empty = none.', related_name='lender_api_keys', to='core.company'),
        ),

        # --- One active advance per invoice --------------------------------
        migrations.RunPython(dedupe_active_advances, noop),
        migrations.AddConstraint(
            model_name='advancerequest',
            constraint=models.UniqueConstraint(condition=Q(status__in=['REQUESTED', 'SCORING', 'APPROVED', 'DISBURSED']), fields=('invoice',), name='uniq_active_advance_per_invoice'),
        ),

        # --- Facility capacity invariants ----------------------------------
        migrations.RunPython(reserve_open_advances, noop),
        migrations.AddConstraint(
            model_name='facility',
            constraint=models.CheckConstraint(condition=Q(outstanding__gte=0), name='facility_outstanding_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='facility',
            constraint=models.CheckConstraint(condition=Q(reserved__gte=0), name='facility_reserved_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='facility',
            constraint=models.CheckConstraint(condition=Q(limit__gte=F('outstanding') + F('reserved')), name='facility_committed_within_limit'),
        ),
    ]
