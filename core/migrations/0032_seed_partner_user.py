"""Seed partner test account: partner@truckwys.com / Partner2026!"""

from django.db import migrations
from django.contrib.auth.hashers import make_password


def seed_partner_user(apps, schema_editor):
    User = apps.get_model('core', 'User')

    # Don't create if already exists
    if User.objects.filter(email='partner@truckwys.com').exists():
        return

    User.objects.create(
        username='partner',
        email='partner@truckwys.com',
        password=make_password('Partner2026!'),
        role='PARTNER',
        status='ACTIVE',
        is_active=True,
    )


def remove_partner_user(apps, schema_editor):
    User = apps.get_model('core', 'User')
    User.objects.filter(email='partner@truckwys.com', role='PARTNER').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_make_invoice_optional_in_payment_outcome'),
    ]

    operations = [
        migrations.RunPython(seed_partner_user, remove_partner_user),
    ]
