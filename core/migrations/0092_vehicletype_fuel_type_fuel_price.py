from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0091_merge_20260807_1834'),
    ]

    operations = [
        migrations.AddField(
            model_name='vehicletype',
            name='fuel_type',
            field=models.CharField(
                choices=[('Diesel', 'Diesel'), ('Petrol', 'Petrol'), ('Electric', 'Electric'), ('Hybrid', 'Hybrid')],
                default='Diesel',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='vehicletype',
            name='fuel_price',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=8, null=True,
                help_text='Price per litre for this vehicle type. Falls back to the company/national fuel price when unset.',
            ),
        ),
    ]
