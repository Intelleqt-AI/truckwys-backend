from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0094_company_fuel_price_defaults'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='vehicletype',
            name='fuel_price',
        ),
    ]
