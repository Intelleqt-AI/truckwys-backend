"""ON DELETE SET NULL in the database for the new load foreign keys, so an
older app image (which doesn't know them) can delete a linked load, a vehicle
type or a user without an IntegrityError. PostgreSQL only (production);
SQLite keeps Django's own handling."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


FKS = (('return_of_id', 'loads', 'id'), ('priced_vehicle_type_id', None, None), ('return_linked_by_id', None, None))


def _fk_constraints(cursor, column):
    cursor.execute("""
        SELECT con.conname, pg_get_constraintdef(con.oid)
          FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
          JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY (con.conkey)
         WHERE rel.relname = 'loads' AND con.contype = 'f' AND att.attname = %s""", [column])
    return cursor.fetchall()


def _rewrite(schema_editor, on_delete):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        for column, _t, _c in FKS:
            for name, definition in _fk_constraints(cursor, column):
                base = definition.split(' ON DELETE ')[0].split(' DEFERRABLE')[0]
                new = f'{base}{on_delete} DEFERRABLE INITIALLY DEFERRED'
                cursor.execute(f'ALTER TABLE loads DROP CONSTRAINT "{name}"')
                cursor.execute(f'ALTER TABLE loads ADD CONSTRAINT "{name}" {new}')


def db_on_delete_set_null(apps, schema_editor):
    _rewrite(schema_editor, ' ON DELETE SET NULL')


def db_on_delete_plain(apps, schema_editor):
    _rewrite(schema_editor, '')


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0175_webhook_signatures'),
    ]

    operations = [
        migrations.RunPython(db_on_delete_set_null, db_on_delete_plain),
    ]
