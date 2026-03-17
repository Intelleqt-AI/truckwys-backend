from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0033_fuelprice'),
    ]

    operations = [
        migrations.CreateModel(
            name='TollPlaza',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='Official SANRAL plaza name', max_length=100)),
                ('route', models.CharField(
                    choices=[
                        ('N1', 'N1 — Cape Town to Johannesburg'),
                        ('N2', 'N2 — Cape Town to Durban (coastal)'),
                        ('N3', 'N3 — Johannesburg to Durban'),
                        ('N4', 'N4 — Pretoria to Maputo (TRAC concession)'),
                        ('N14', 'N14 — Johannesburg to Springbok'),
                    ],
                    db_index=True,
                    max_length=10,
                )),
                ('direction', models.CharField(help_text='Route description e.g. "Cape Town → Johannesburg"', max_length=100)),
                ('location_km', models.DecimalField(decimal_places=1, help_text='Distance from route origin (km)', max_digits=7)),
                ('tariff_class_2', models.DecimalField(decimal_places=2, help_text='Light motor vehicle tariff (ZAR)', max_digits=8)),
                ('tariff_class_3', models.DecimalField(decimal_places=2, help_text='Medium motor vehicle tariff (ZAR)', max_digits=8)),
                ('tariff_class_4', models.DecimalField(decimal_places=2, help_text='Heavy motor vehicle tariff (ZAR)', max_digits=8)),
                ('tariff_class_5', models.DecimalField(decimal_places=2, help_text='Multi-unit combination tariff (ZAR)', max_digits=8)),
                ('tariff_year', models.PositiveSmallIntegerField(default=2024, help_text='Tariff revision year (SANRAL announces increases annually)')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'toll_plazas',
                'ordering': ['route', 'location_km'],
                'unique_together': {('name', 'route')},
            },
        ),
    ]
