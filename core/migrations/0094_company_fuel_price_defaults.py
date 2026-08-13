from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0093_fix_vehicletype_defaults'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='fuel_price_petrol',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=6, null=True,
                help_text='Default Petrol price per litre in ZAR',
            ),
        ),
        migrations.AddField(
            model_name='company',
            name='fuel_price_electric',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=6, null=True,
                help_text='Default Electric price per kWh in ZAR',
            ),
        ),
        migrations.AddField(
            model_name='company',
            name='fuel_price_hybrid',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=6, null=True,
                help_text='Default Hybrid price per litre in ZAR',
            ),
        ),
        migrations.AlterField(
            model_name='company',
            name='fuel_price_per_litre',
            field=models.DecimalField(
                decimal_places=2, default=23.50, max_digits=6,
                help_text='Default Diesel price per litre in ZAR (default: R23.50)',
            ),
        ),
    ]
