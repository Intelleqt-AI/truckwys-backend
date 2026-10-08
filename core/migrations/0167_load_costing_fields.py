"""Load costing assumptions (trip economics). Columns only, with database
defaults (db_default) so an older app image can still insert loads."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0166_webhooksubscription_company'),
    ]

    operations = [
        migrations.AddField(
            model_name='load',
            name='cost_floor',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Floor of costing_snapshot (null = incomplete / unknown)', max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='costed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='costing_inputs',
            field=models.JSONField(blank=True, db_default=models.Value({}, output_field=models.JSONField()), default=dict),
        ),
        migrations.AddField(
            model_name='load',
            name='costing_snapshot',
            field=models.JSONField(blank=True, db_default=models.Value({}, output_field=models.JSONField()), default=dict),
        ),
        migrations.AddField(
            model_name='load',
            name='costing_source',
            field=models.CharField(blank=True, choices=[('', 'Not costed (legacy)'), ('quote', 'Quote snapshot'), ('computed', 'Computed from the load'), ('unknown', 'Not enough information')], db_default='', default='', max_length=10),
        ),
        migrations.AddField(
            model_name='load',
            name='empty_return_assumed',
            field=models.BooleanField(blank=True, help_text='The costing includes an empty return leg (null = unknown)', null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='fuel_effective_from',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='fuel_litres',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='fuel_price_source',
            field=models.CharField(blank=True, db_default='', default='', max_length=10),
        ),
        migrations.AddField(
            model_name='load',
            name='fuel_price_used',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='fuel_zone',
            field=models.CharField(blank=True, db_default='', default='', max_length=10),
        ),
        migrations.AddField(
            model_name='load',
            name='priced_vehicle_type',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='core.vehicletype'),
        ),
        migrations.AddField(
            model_name='load',
            name='quoted_cost_floor',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='quoted_margin_pct',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='quoted_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='return_cargo',
            field=models.TextField(blank=True, db_default='', default=''),
        ),
        migrations.AddField(
            model_name='load',
            name='return_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='return_distance',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='load',
            name='return_location',
            field=models.CharField(blank=True, db_default='', default='', max_length=500),
        ),
        migrations.AddField(
            model_name='load',
            name='trip_type',
            field=models.CharField(choices=[('ONE_WAY', 'One Way'), ('ROUND_TRIP', 'Round Trip')], db_default='ONE_WAY', default='ONE_WAY', max_length=20),
        ),
    ]
