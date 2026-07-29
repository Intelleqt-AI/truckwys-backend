from django.db import models


class FcmDevice(models.Model):
    """A mobile device's Firebase Cloud Messaging registration token.

    One row per app install. FCM is the transport for both platforms: Android
    natively, iOS via FCM's APNs relay. Rows are pruned automatically when FCM
    reports the token unregistered (see core/services/fcm_push.py), and on
    sign-out the app deletes its own row so a logged-out device never receives
    the previous user's notifications.
    """

    PLATFORM_CHOICES = [('ios', 'iOS'), ('android', 'Android')]

    user = models.ForeignKey(
        'User', on_delete=models.CASCADE, related_name='fcm_devices'
    )
    # FCM registration tokens are long and have no documented maximum; TextField
    # avoids a truncation bug if Google lengthens them.
    token = models.TextField(unique=True)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, blank=True)
    device_name = models.CharField(max_length=200, blank=True)
    app_version = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fcm_devices'
        ordering = ['-last_used_at']
        indexes = [models.Index(fields=['user'], name='fcm_devices_user_id_idx')]

    def __str__(self):
        return f'{self.user_id} · {self.platform} · {self.token[:16]}…'
