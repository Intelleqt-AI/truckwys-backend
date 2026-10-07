# Quote.margin_percentage becomes nullable: no cost floor -> no margin (null),
# never a 0,00 that reads as "no margin".

from django.db import migrations, models


def null_unknown_margins(apps, schema_editor):
    """A stored 0 on a quote with no cost floor was never a margin (the old
    default): make it null. Every other value is left as it is."""
    Quote = apps.get_model('core', 'Quote')
    Quote.objects.filter(cost_floor__isnull=True, margin_percentage=0).update(margin_percentage=None)


def zero_null_margins(apps, schema_editor):
    Quote = apps.get_model('core', 'Quote')
    Quote.objects.filter(margin_percentage__isnull=True).update(margin_percentage=0)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0155_fuel_price_unique_per_date_and_source'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quote',
            name='margin_percentage',
            field=models.DecimalField(blank=True, decimal_places=2, default=None, help_text='Profit margin %',
                                      max_digits=5, null=True),
        ),
        migrations.RunPython(null_unknown_margins, zero_null_margins),
    ]
