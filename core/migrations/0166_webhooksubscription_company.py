"""WebhookSubscription.company — ISOLATED on purpose: when PR #130 (same field)
merges first, delete this file and point 0167 at the latest migration."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0165_retire_corridor_totals_and_weighbridge_fees'),
    ]

    operations = [
        migrations.AddField(
            model_name='webhooksubscription',
            name='company',
            field=models.ForeignKey(blank=True, help_text='Transporter this subscription acts for on fleet webhooks. Empty = none.', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='webhook_subscriptions', to='core.company'),
        ),
    ]
