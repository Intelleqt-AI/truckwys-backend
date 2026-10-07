# Quote.margin_percentage holds the true margin on price (a price far under
# cost can be −4 000 %), instead of a fake ±999,99 cap.

from django.db import migrations, models


def null_values_too_wide(apps, schema_editor):
    """Reverse: values that don't fit the old (5, 2) column become null."""
    Quote = apps.get_model('core', 'Quote')
    Quote.objects.filter(models.Q(margin_percentage__gte=1000) | models.Q(margin_percentage__lte=-1000)) \
        .update(margin_percentage=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0156_quote_margin_percentage_nullable'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quote',
            name='margin_percentage',
            field=models.DecimalField(blank=True, decimal_places=2, default=None, help_text='Profit margin %',
                                      max_digits=9, null=True),
        ),
        migrations.RunPython(migrations.RunPython.noop, null_values_too_wide),
    ]
