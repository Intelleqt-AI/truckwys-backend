from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0147_company_driver_allowance_quote_was_sent'),
    ]

    operations = [
        migrations.AddField(
            model_name='quotepricingdecision',
            name='superseded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
