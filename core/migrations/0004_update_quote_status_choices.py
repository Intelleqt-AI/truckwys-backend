# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_quote_confidence_quote_destination_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quote',
            name='status',
            field=models.CharField(
                choices=[
                    ('DRAFT', 'Draft'),
                    ('SENT', 'Sent'),
                    ('ACCEPTED', 'Accepted'),
                    ('IT', 'In-Transit'),
                    ('COMPLETED', 'Completed')
                ],
                default='DRAFT',
                max_length=50
            ),
        ),
    ]
