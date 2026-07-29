# Aligns the billing state machine with TruckWys_Fee_Billing_Spec.pdf §4/§7:
# consolidates subscription_status + the separate take_rate_frozen boolean
# into one 4-state machine (active / grace_period / suspended / cancelled).
from django.db import migrations, models


def migrate_billing_state(apps, schema_editor):
    """past_due -> grace_period; take_rate_frozen=True -> suspended. Runs
    while the old fields still exist (removed later in this same migration)."""
    Company = apps.get_model('core', 'Company')
    for company in Company.objects.all():
        changed = []
        if company.subscription_status == 'past_due':
            company.subscription_status = 'grace_period'
            changed.append('subscription_status')
        if getattr(company, 'take_rate_frozen', False):
            company.subscription_status = 'suspended'
            if 'subscription_status' not in changed:
                changed.append('subscription_status')
        lapsed_at = getattr(company, 'subscription_lapsed_at', None)
        if lapsed_at and company.subscription_status in ('grace_period', 'suspended'):
            # Best-effort carry-over — the exact original grace deadline math
            # isn't reconstructable, so this is just a reasonable non-null stamp.
            company.grace_period_expires_at = lapsed_at
            changed.append('grace_period_expires_at')
        if changed:
            company.save(update_fields=changed)


def reverse_noop(apps, schema_editor):
    pass  # one-way: 'suspended' could have come from either prior source, no reliable inverse


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0085_company_subscription_lapsed_at_and_more'),
    ]

    operations = [
        # 1. Add the new fields first so the data migration can write to them
        #    while the old fields (removed below) are still readable.
        migrations.AddField(
            model_name='company',
            name='last_charge_attempt_at',
            field=models.DateTimeField(blank=True, null=True, help_text='Most recent charge attempt (subscription or take-rate), success or failure'),
        ),
        migrations.AddField(
            model_name='company',
            name='grace_period_expires_at',
            field=models.DateTimeField(blank=True, null=True, help_text='Set once on entering grace_period (now + grace days); cleared on return to active. Past this timestamp with no successful charge, the company moves to suspended.'),
        ),
        # 2. Migrate existing data before the old fields are dropped.
        migrations.RunPython(migrate_billing_state, reverse_noop),
        # 3. Drop the old fields and finalize the new choices/statuses.
        migrations.RemoveField(model_name='company', name='subscription_lapsed_at'),
        migrations.RemoveField(model_name='company', name='take_rate_frozen'),
        migrations.RemoveField(model_name='company', name='take_rate_frozen_at'),
        migrations.AlterField(
            model_name='company',
            name='subscription_status',
            field=models.CharField(
                choices=[('none', 'None'), ('trialing', 'Trialing'), ('active', 'Active'), ('grace_period', 'Grace Period'), ('suspended', 'Suspended'), ('cancelled', 'Cancelled')],
                default='none', max_length=20,
            ),
        ),
        migrations.RemoveField(model_name='deliveryfeecharge', name='first_failed_at'),
        migrations.AlterField(
            model_name='deliveryfeecharge',
            name='status',
            field=models.CharField(choices=[('pending', 'Pending'), ('charged', 'Charged'), ('failed', 'Failed')], default='pending', max_length=20),
        ),
    ]
