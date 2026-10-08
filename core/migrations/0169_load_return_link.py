"""Return-load link (+ costs_closed)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0168_load_costing_backfill'),
    ]

    operations = [
        migrations.AddField(
            model_name='load',
            name='costs_closed',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='load',
            name='expecting_return',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='load',
            name='return_link_source',
            field=models.CharField(blank=True, choices=[('manual', 'Linked by a user'), ('tms', 'Linked by the TMS'), ('convert', 'Linked when booking the quote')], db_default='', default='', max_length=10),
        ),
        migrations.AddField(
            model_name='load',
            name='return_linked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='return_linked_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='load',
            name='return_of',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='return_load', to='core.load'),
        ),
    ]
