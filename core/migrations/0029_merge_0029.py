from django.db import migrations


class Migration(migrations.Migration):
    """Merge migration: resolves conflict between 0029_add_api_usage_tracking and 0029_alter_company_subscription_plan"""

    dependencies = [
        ('core', '0029_add_api_usage_tracking'),
        ('core', '0029_alter_company_subscription_plan'),
    ]

    operations = [
    ]
