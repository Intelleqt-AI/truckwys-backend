"""TMS identity / flags (columns only)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0170_load_estimate_cache'),
    ]

    operations = [
        migrations.AddField(
            model_name='load',
            name='external_id',
            field=models.CharField(blank=True, db_default='', default='', max_length=100),
        ),
        migrations.AddField(
            model_name='load',
            name='external_source',
            field=models.CharField(blank=True, db_default='', default='', max_length=50),
        ),
        migrations.AddField(
            model_name='load',
            name='invoice_mismatch',
            field=models.JSONField(blank=True, db_default=models.Value({}, output_field=models.JSONField()), default=dict),
        ),
        migrations.AddField(
            model_name='load',
            name='return_of_external_ref',
            field=models.CharField(blank=True, db_default='', default='', max_length=120),
        ),
    ]
