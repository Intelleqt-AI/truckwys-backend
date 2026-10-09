"""Fleet webhook replay protection table + legacy-signature opt-in."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0174_quoteoutcome_actuals'),
    ]

    operations = [
        migrations.CreateModel(
            name='UsedWebhookSignature',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=100, unique=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
            ],
            options={
                'db_table': 'used_webhook_signatures',
            },
        ),
        migrations.AddField(
            model_name='webhooksubscription',
            name='allow_legacy_signature',
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
