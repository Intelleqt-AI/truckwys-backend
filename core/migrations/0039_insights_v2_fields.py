# Generated migration for Insights v2

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0038_auto_20260319_1855'),
    ]

    operations = [
        # Add fields to Quote model
        migrations.AddField(
            model_name='quote',
            name='predicted_margin_pct',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='AI-predicted profit margin percentage at quote time',
                max_digits=5
            ),
        ),
        migrations.AddField(
            model_name='quote',
            name='revenue_guard_flagged',
            field=models.BooleanField(
                default=False,
                help_text='Whether Revenue Guard flagged this quote as low-margin'
            ),
        ),

        # Add fields to Load model
        migrations.AddField(
            model_name='load',
            name='actual_margin_pct',
            field=models.DecimalField(
                decimal_places=2,
                help_text='Actual margin percentage calculated at completion',
                max_digits=5,
                null=True,
                blank=True
            ),
        ),
        migrations.AddField(
            model_name='load',
            name='total_km',
            field=models.DecimalField(
                decimal_places=2,
                help_text='Total distance in kilometers',
                max_digits=10,
                null=True,
                blank=True
            ),
        ),
    ]
