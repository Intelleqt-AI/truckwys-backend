"""Data for the accounting integrations.

1. revenue_type on existing invoice lines: only lines whose description is
   exactly what TruckWys' own invoice generator writes ("Fuel Surcharge...",
   "Toll Charges", "Extra Distance (...", "Driver Premium (...") get a
   non-default type. Everything else stays FREIGHT (the field default).
   Credit note lines copy the type of the invoice line they credit.
2. Companies connected through the old Xero prototype (Company.xero_*) get
   an AccountingConnection in NEEDS_REAUTH: the new integration needs more
   scopes (tax rates, accounts, tracking) and an org name, so the user
   reconnects once. The old token columns are left untouched (no data is
   destroyed); nothing reads them any more.

Batched, idempotent, safe to re-run.
"""
from django.db import migrations

BATCH = 500


def forwards(apps, schema_editor):
    from core.revenue_types import GENERATED_PREFIXES, FREIGHT

    InvoiceLine = apps.get_model('core', 'InvoiceLine')
    CreditNoteLine = apps.get_model('core', 'CreditNoteLine')
    Company = apps.get_model('core', 'Company')
    AccountingConnection = apps.get_model('core', 'AccountingConnection')

    for prefix, kind in GENERATED_PREFIXES:
        (InvoiceLine.objects.filter(revenue_type=FREIGHT, description__startswith=prefix)
         .update(revenue_type=kind))

    qs = (CreditNoteLine.objects.filter(invoice_line__isnull=False)
          .exclude(invoice_line__revenue_type=FREIGHT).select_related('invoice_line'))
    batch = []
    for cnl in qs.iterator(chunk_size=BATCH):
        if cnl.revenue_type != cnl.invoice_line.revenue_type:
            cnl.revenue_type = cnl.invoice_line.revenue_type
            batch.append(cnl)
        if len(batch) >= BATCH:
            CreditNoteLine.objects.bulk_update(batch, ['revenue_type'])
            batch = []
    if batch:
        CreditNoteLine.objects.bulk_update(batch, ['revenue_type'])

    for company in Company.objects.exclude(xero_tenant_id__isnull=True).exclude(xero_tenant_id='').iterator():
        if AccountingConnection.objects.filter(company=company).exists():
            continue
        AccountingConnection.objects.create(
            company=company, provider='XERO', status='NEEDS_REAUTH', tenant_id='',
            status_reason='Xero was connected with the earlier integration. Reconnect once to '
                          'grant the new permissions (tax rates, accounts, tracking).',
            settings={'legacy_tenant_id': company.xero_tenant_id},
            connected_at=company.xero_connected_at,
        )


def backwards(apps, schema_editor):
    AccountingConnection = apps.get_model('core', 'AccountingConnection')
    AccountingConnection.objects.filter(status='NEEDS_REAUTH', settings__has_key='legacy_tenant_id').delete()


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('core', '0139_accounting_integrations'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
