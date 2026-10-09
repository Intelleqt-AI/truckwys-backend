from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0165_retire_corridor_totals_and_weighbridge_fees'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='is_test_company',
            field=models.BooleanField(default=False),
        ),
    ]
