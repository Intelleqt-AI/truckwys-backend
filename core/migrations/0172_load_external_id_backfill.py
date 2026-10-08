"""Move trips/sync's legacy 'ext_id:' notes onto external_id (batched,
each batch its own transaction)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


BATCH = 1000


def external_ids_from_notes(apps, schema_editor):
    """trips/sync wrote notes = 'ext_id:<id>': the id runs to the end of its
    line (ids may contain spaces). The first load per (company, id) gets it;
    ids over 100 characters stay in the note; a note with several ext_id
    lines keeps the others findable (tms_sync.find_by_external_id reads the
    notes too). Batches of 1 000, each in its own transaction."""
    import re
    from django.db import transaction
    Load = apps.get_model('core', 'Load')
    pattern = re.compile(r'^\s*ext_id:(.+?)\s*$', re.MULTILINE)
    seen = set(Load.objects.exclude(external_id='').values_list('company_id', 'external_id'))
    last = 0
    while True:
        with transaction.atomic():
            batch = list(Load.objects.filter(pk__gt=last, notes__contains='ext_id:', external_id='')
                         .order_by('pk').only('id', 'company_id', 'notes', 'external_id', 'external_source')
                         [:BATCH])
            if not batch:
                break
            changed = []
            for load in batch:
                for m in pattern.finditer(load.notes or ''):
                    ext = m.group(1).strip()
                    key = (load.company_id, ext)
                    if not ext or len(ext) > 100 or key in seen:
                        continue
                    seen.add(key)
                    load.external_id, load.external_source = ext, 'tms_api'
                    changed.append(load)
                    break
            if changed:
                Load.objects.bulk_update(changed, ['external_id', 'external_source'])
        last = batch[-1].pk


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('core', '0171_load_external_id'),
    ]

    operations = [
        migrations.RunPython(external_ids_from_notes, migrations.RunPython.noop, elidable=True),
    ]
