from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_seed_partner_user'),
    ]

    operations = [
        migrations.CreateModel(
            name='FuelPrice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(help_text='First day of the price month', unique=True)),
                ('diesel_inland', models.DecimalField(decimal_places=4, help_text='Diesel 50ppm inland retail price (ZAR/litre)', max_digits=8)),
                ('diesel_coastal', models.DecimalField(decimal_places=4, help_text='Diesel 50ppm coastal retail price (ZAR/litre)', max_digits=8)),
                ('petrol_95', models.DecimalField(decimal_places=4, help_text='Petrol 95 ULP inland retail price (ZAR/litre)', max_digits=8)),
                ('petrol_93', models.DecimalField(decimal_places=4, help_text='Petrol 93 ULP inland retail price (ZAR/litre)', max_digits=8)),
                ('source', models.CharField(default='SAPIA', help_text='Data source identifier', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'fuel_prices',
                'ordering': ['-date'],
            },
        ),
    ]
