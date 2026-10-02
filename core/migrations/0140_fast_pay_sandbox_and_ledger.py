"""Fast Pay book, data step: sandbox funder, lines under it, opening ledger balances.

* Every existing per-company Facility becomes a transporter line under one
  SANDBOX funder (no funder is signed; it moves no real money). Its pot is the
  sum of the existing line limits, so no line loses headroom, and
  ``staff_may_approve`` keeps today's staff approval flow working.
* The append-only ledger starts from the balances on record: one OPENING row
  per disbursed advance (outstanding) and per held reservation (reserved),
  plus one facility-level OPENING row for any difference between those and
  the cached ``Facility.outstanding``/``reserved`` (legacy direct writes).
  After this, ledger-derived balances equal the cached ones for every line,
  which ``manage.py capital_reconcile`` checks from then on.
* Advances get their funder and debtor identity stamped.
* On Postgres a trigger refuses UPDATE and DELETE on capital_ledger_entries.

Reverse: the trigger is dropped and the opening rows, stamps and sandbox
funder are removed (only while no non-opening ledger rows exist).
"""
from decimal import Decimal

from django.db import migrations

ZERO = Decimal('0.00')

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION capital_ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'capital_ledger_entries is append-only: write a new entry instead of %', TG_OP;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS capital_ledger_append_only ON capital_ledger_entries;
CREATE TRIGGER capital_ledger_append_only
    BEFORE UPDATE OR DELETE ON capital_ledger_entries
    FOR EACH ROW EXECUTE FUNCTION capital_ledger_append_only();
"""
DROP_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS capital_ledger_append_only ON capital_ledger_entries;
DROP FUNCTION IF EXISTS capital_ledger_append_only();
"""


def _run_raw(schema_editor, sql):
    # A raw cursor without params, so the '%' in RAISE is not read as a placeholder.
    with schema_editor.connection.cursor() as cur:
        cur.execute(sql)


def add_trigger(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        _run_raw(schema_editor, TRIGGER_SQL)


def drop_trigger(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        _run_raw(schema_editor, DROP_TRIGGER_SQL)


def forwards(apps, schema_editor):
    Funder = apps.get_model('core', 'Funder')
    Facility = apps.get_model('core', 'Facility')
    AdvanceRequest = apps.get_model('core', 'AdvanceRequest')
    Entry = apps.get_model('core', 'CapitalLedgerEntry')

    pot = sum((f.limit for f in Facility.objects.all()), ZERO)
    funder, _ = Funder.objects.get_or_create(
        code='sandbox',
        defaults=dict(
            name='Sandbox (pre-launch, no funder signed)',
            status='SANDBOX',
            pot_limit=pot,
            operating_mode='A',
            staff_may_approve=True,
        ),
    )
    Facility.objects.filter(funder__isnull=True).update(funder=funder)

    rows = []
    for fac in Facility.objects.all().order_by('id'):
        disbursed_total = ZERO
        reserved_total = ZERO
        advances = AdvanceRequest.objects.filter(facility=fac).select_related('invoice__customer')
        for adv in advances.order_by('id'):
            customer = getattr(adv.invoice, 'customer', None)
            debtor_id = getattr(customer, 'debtor_identity_id', None)
            AdvanceRequest.objects.filter(pk=adv.pk).update(funder=funder, debtor_id=debtor_id)
            common = dict(funder=funder, facility=fac, company_id=fac.company_id, debtor_id=debtor_id,
                          invoice_id=adv.invoice_id, advance=adv, actor_label='migration 0140')
            if adv.status == 'DISBURSED':
                rows.append(Entry(entry_type='OPENING', amount=adv.amount, outstanding_delta=adv.amount,
                                  memo='Opening balance: advance disbursed before the ledger existed', **common))
                disbursed_total += adv.amount
            held = adv.capacity_reserved or ZERO
            if held > 0:
                rows.append(Entry(entry_type='OPENING', amount=held, reserved_delta=held,
                                  memo='Opening balance: reservation held before the ledger existed', **common))
                reserved_total += held
        out_gap = (fac.outstanding or ZERO) - disbursed_total
        res_gap = (fac.reserved or ZERO) - reserved_total
        if out_gap or res_gap:
            rows.append(Entry(
                entry_type='OPENING', funder=funder, facility=fac, company_id=fac.company_id,
                amount=abs(out_gap) + abs(res_gap), outstanding_delta=out_gap, reserved_delta=res_gap,
                actor_label='migration 0140',
                memo=('Opening balance: line figures not explained by individual advances '
                      f'(outstanding {out_gap}, reserved {res_gap}); review with the capital desk'),
            ))
    Entry.objects.bulk_create(rows, batch_size=500)


def backwards(apps, schema_editor):
    Funder = apps.get_model('core', 'Funder')
    Facility = apps.get_model('core', 'Facility')
    AdvanceRequest = apps.get_model('core', 'AdvanceRequest')
    Entry = apps.get_model('core', 'CapitalLedgerEntry')
    if Entry.objects.exclude(entry_type='OPENING').exists():
        raise RuntimeError('Capital ledger has entries beyond the opening balances; refusing to reverse 0140')
    Entry.objects.all().delete()
    AdvanceRequest.objects.update(funder=None, debtor=None)
    Facility.objects.update(funder=None)
    Funder.objects.filter(code='sandbox').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0139_fast_pay_book'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
        # Last, so on reverse the trigger is dropped before the opening rows go.
        migrations.RunPython(add_trigger, drop_trigger),
    ]
