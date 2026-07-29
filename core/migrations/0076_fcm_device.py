import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0075_copilotusermemory'),
    ]

    operations = [
        migrations.CreateModel(
            name='FcmDevice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.TextField(unique=True)),
                ('platform', models.CharField(blank=True, choices=[('ios', 'iOS'), ('android', 'Android')], max_length=10)),
                ('device_name', models.CharField(blank=True, max_length=200)),
                ('app_version', models.CharField(blank=True, max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_used_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fcm_devices', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'fcm_devices',
                'ordering': ['-last_used_at'],
            },
        ),
        migrations.AddIndex(
            model_name='fcmdevice',
            index=models.Index(fields=['user'], name='fcm_devices_user_id_idx'),
        ),
    ]
