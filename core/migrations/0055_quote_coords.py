from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0054_vehicle_km_service_scheduling'),
    ]

    operations = [
        migrations.AddField(
            model_name='quote',
            name='pickup_lat',
            field=models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='quote',
            name='pickup_lng',
            field=models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='quote',
            name='delivery_lat',
            field=models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='quote',
            name='delivery_lng',
            field=models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True),
        ),
    ]
