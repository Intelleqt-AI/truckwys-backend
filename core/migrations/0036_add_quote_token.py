import secrets
from django.db import migrations, models


def populate_tokens(apps, schema_editor):
    Quote = apps.get_model('core', 'Quote')
    for q in Quote.objects.filter(token=''):
        q.token = secrets.token_urlsafe(32)
        q.save(update_fields=['token'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0035_alter_company_subscription_plan_alter_user_role'),
    ]

    operations = [
        migrations.AddField(
            model_name='quote',
            name='token',
            field=models.CharField(blank=True, max_length=64, default=''),
        ),
        migrations.RunPython(populate_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='quote',
            name='token',
            field=models.CharField(blank=True, max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name='quote',
            name='status',
            field=models.CharField(
                choices=[('DRAFT', 'Draft'), ('SENT', 'Sent'), ('ACCEPTED', 'Accepted'),
                         ('IN_TRANSIT', 'In Transit'), ('COMPLETED', 'Completed'), ('DECLINED', 'Declined')],
                default='DRAFT', max_length=20
            ),
        ),
    ]
