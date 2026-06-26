from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0057_integration_api_key_allowed_ips_webhook_call_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='view_token',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
