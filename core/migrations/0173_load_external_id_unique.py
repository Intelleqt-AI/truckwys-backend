"""Unique (company, external_id) where set — its own migration (PostgreSQL:
no ALTER after a data update in the same transaction)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0172_load_external_id_backfill'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='load',
            constraint=models.UniqueConstraint(condition=models.Q(('external_id', ''), _negated=True), fields=('company', 'external_id'), name='uniq_load_external_id_per_company'),
        ),
    ]
