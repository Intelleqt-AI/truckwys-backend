from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0090_company_onboarding_completed_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='ctrlfleet_api_key',
            field=models.TextField(blank=True, null=True, help_text='CtrlFleet External API key, x-api-key header (encrypted at rest)'),
        ),
        migrations.AddField(
            model_name='company',
            name='ctrlfleet_connected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='company',
            name='ctrlfleet_last_vehicle_sync',
            field=models.DateTimeField(blank=True, null=True, help_text='Last time the vehicle roster was matched against CtrlFleet by licence plate'),
        ),
        migrations.AddField(
            model_name='vehicle',
            name='ctrlfleet_vehicle_code',
            field=models.CharField(blank=True, max_length=100, null=True, help_text='CtrlFleet vehicleCode this vehicle was matched to by licence plate'),
        ),
    ]
