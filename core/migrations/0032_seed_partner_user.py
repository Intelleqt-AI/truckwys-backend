"""Seed partner test account: partner@truckwys.com / Partner2026!"""

from django.db import migrations


def seed_partner_user(apps, schema_editor):
    User = apps.get_model('core', 'User')
    
    # Don't create if already exists
    if User.objects.filter(email='partner@truckwys.com').exists():
        return
    
    user = User(
        username='partner',
        email='partner@truckwys.com',
        role='PARTNER',
        status='ACTIVE',
        is_active=True,
    )
    user.set_password('Partner2026!')
    user.save()


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
