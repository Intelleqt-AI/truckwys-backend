from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0053_activity_event_company_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='vehicle',
            name='service_interval_km',
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text='How many km between services (e.g. 10000)',
            ),
        ),
        migrations.AddField(
            model_name='vehicle',
            name='last_service_mileage',
            field=models.DecimalField(
                blank=True,
                null=True,
                max_digits=10,
                decimal_places=2,
                help_text='Odometer reading at last service',
            ),
        ),
    ]
