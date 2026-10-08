"""Cached pair-aware estimate."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0169_load_return_link'),
    ]

    operations = [
        migrations.AddField(
            model_name='load',
            name='economics_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='estimate_basis',
            field=models.CharField(blank=True, db_default='', default='', max_length=30),
        ),
        migrations.AddField(
            model_name='load',
            name='estimated_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
    ]
