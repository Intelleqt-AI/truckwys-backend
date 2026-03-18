"""Create VehicleCostProfile model."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0034_tollplaza'),
    ]

    operations = [
        migrations.CreateModel(
            name='VehicleCostProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('truck_type', models.CharField(
                    choices=[
                        ('rigid_8t', 'Rigid 8-Ton'),
                        ('rigid_16t', 'Rigid 16-Ton'),
                        ('horse_trailer', 'Horse & Trailer'),
                        ('interlink', 'Interlink'),
                        ('abnormal', 'Abnormal Load'),
                    ],
                    help_text='SA truck category (RFA classification)',
                    max_length=20,
                )),
                ('fuel_cpk', models.DecimalField(decimal_places=4, help_text='Fuel cost per km in ZAR', max_digits=8)),
                ('tyre_cpk', models.DecimalField(decimal_places=4, help_text='Tyre cost per km in ZAR', max_digits=8)),
                ('maintenance_cpk', models.DecimalField(decimal_places=4, help_text='Maintenance cost per km in ZAR', max_digits=8)),
                ('driver_cost_per_day', models.DecimalField(decimal_places=2, help_text='Daily driver cost in ZAR', max_digits=10)),
                ('is_custom', models.BooleanField(default=False, help_text='True if this is a company override (not an RFA default)')),
                ('source', models.CharField(help_text='Data source identifier, e.g. "RFA VCI 2024"', max_length=100)),
                ('effective_date', models.DateField(help_text='Date from which this profile is effective')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.ForeignKey(
                    blank=True,
                    help_text='Null for RFA defaults; set for company overrides',
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='cost_profiles',
                    to='core.company',
                )),
            ],
            options={
                'db_table': 'vehicle_cost_profiles',
                'ordering': ['-effective_date'],
                'verbose_name': 'Vehicle Cost Profile',
                'verbose_name_plural': 'Vehicle Cost Profiles',
            },
        ),
        migrations.AddIndex(
            model_name='vehiclecostprofile',
            index=models.Index(fields=['truck_type', 'company'], name='idx_vcp_type_company'),
        ),
    ]
