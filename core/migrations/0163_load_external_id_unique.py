# Separate migration (own transaction): on PostgreSQL an ALTER TABLE right
# after the data update in 0162 could fail with "pending trigger events".
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0162_load_external_id'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='load',
            constraint=models.UniqueConstraint(condition=models.Q(('external_id', ''), _negated=True), fields=('company', 'external_id'), name='uniq_load_external_id_per_company'),
        ),
    ]
