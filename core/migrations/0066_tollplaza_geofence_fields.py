from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0065_create_cache_table'),
    ]

    operations = [
        migrations.AddField(
            model_name='tollplaza',
            name='lat',
            field=models.DecimalField(
                blank=True, decimal_places=6, max_digits=9, null=True,
                help_text='Plaza GPS latitude (WGS84) for geofence matching',
            ),
        ),
        migrations.AddField(
            model_name='tollplaza',
            name='lng',
            field=models.DecimalField(
                blank=True, decimal_places=6, max_digits=9, null=True,
                help_text='Plaza GPS longitude (WGS84) for geofence matching',
            ),
        ),
        migrations.AddField(
            model_name='tollplaza',
            name='radius_meters',
            field=models.IntegerField(
                default=500,
                help_text='Geofence trigger radius in metres (default 500 m to tolerate GPS uncertainty)',
            ),
        ),
    ]
