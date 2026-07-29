from django.db import models


class PushSubscription(models.Model):
    """A browser's Web Push subscription for a user. One row per browser
    profile/device; pruned automatically when the push service reports the
    endpoint gone (404/410)."""
    user = models.ForeignKey("User", on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "push_subscriptions"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user_id} @ {self.endpoint[:40]}…"
