"""Index for quotes.priced_vehicle_type_id (added without one in 0149).

Postgres: CREATE INDEX CONCURRENTLY (no write lock on the quotes table), so
this migration is non-atomic. Other backends (sqlite in tests): a plain
CREATE INDEX. IF NOT EXISTS makes a re-run safe. Reverse drops it.
"""
from django.db import migrations

INDEX = 'quotes_priced_vehicle_type_id_idx'


def forwards(apps, schema_editor):
    concurrently = 'CONCURRENTLY ' if schema_editor.connection.vendor == 'postgresql' else ''
    schema_editor.execute(f'CREATE INDEX {concurrently}IF NOT EXISTS {INDEX} ON quotes (priced_vehicle_type_id)')


def backwards(apps, schema_editor):
    concurrently = 'CONCURRENTLY ' if schema_editor.connection.vendor == 'postgresql' else ''
    schema_editor.execute(f'DROP INDEX {concurrently}IF EXISTS {INDEX}')


class Migration(migrations.Migration):
    atomic = False

    dependencies = [('core', '0150_company_fuel_price_mode_backfill')]

    operations = [migrations.RunPython(forwards, backwards)]
