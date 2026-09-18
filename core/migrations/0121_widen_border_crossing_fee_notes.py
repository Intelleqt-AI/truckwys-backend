"""Widen BorderCrossingFee.notes from 200 to 500 characters.

Runs BEFORE 0112_fix_bw_mz_border_fees (see its dependencies, changed to
point here instead of straight to 0111) rather than after it in file-number
order: 0112 is what actually writes the over-length values, so the schema
change has to land first in the dependency graph, not the filename sequence.
0113-0120 are untouched -- they already depend on 0112 and pick this up
transitively.

Root cause: 0112/0113/0114 cite the real regulation behind each corridor's
fee (a Statutory Instrument, a Decreto, a published RFA/SORCA rate) instead
of a one-line guess. The longest on record, SA->MZ, is 318 characters --
against a 200-char column. SQLite never enforces CharField max_length, so
this passed local testing; Postgres does, and the resulting DataError put
the web container into a crash-restart loop on every deploy that included
0112. 500 is chosen with headroom above 318, not shortened to fit -- these
are the citations the correction migrations were written to preserve.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0111_merge_20260915_1648'),
    ]

    operations = [
        migrations.AlterField(
            model_name='bordercrossingfee',
            name='notes',
            field=models.CharField(blank=True, help_text='e.g. includes COMESA transit docs', max_length=500),
        ),
    ]
