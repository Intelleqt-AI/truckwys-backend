"""QuoteOutcome actuals / estimate so far."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models



class Migration(migrations.Migration):
    dependencies = [
        ('core', '0173_load_external_id_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='quoteoutcome',
            name='actual_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='quoteoutcome',
            name='actual_cost_basis',
            field=models.CharField(blank=True, db_default='', default='', help_text='actual (expenses) | estimate', max_length=12),
        ),
        migrations.AddField(
            model_name='quoteoutcome',
            name='actual_margin_pct',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name='quoteoutcome',
            name='actual_revenue',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='quoteoutcome',
            name='actuals_recorded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='quoteoutcome',
            name='backhaul_found',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='quoteoutcome',
            name='estimated_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='quoteoutcome',
            name='estimated_margin_pct',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=9, null=True),
        ),
    ]
