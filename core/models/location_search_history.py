from django.db import models


class LocationSearchHistory(models.Model):
    """A location a company's users have picked before, in the Collection/
    Delivery fields on a quote. Shared per-company (not per-user) so the
    whole team benefits from each other's regular routes, not just their own
    browser. One row per distinct address text — repeated picks bump
    use_count/last_used_at instead of creating duplicates, which is what lets
    the location field's dropdown rank "most used" ahead of "most recent"."""
    company = models.ForeignKey("Company", on_delete=models.CASCADE, related_name="location_search_history")
    location_text = models.CharField(max_length=500)
    lat = models.DecimalField(max_digits=9, decimal_places=6)
    lon = models.DecimalField(max_digits=9, decimal_places=6)
    use_count = models.PositiveIntegerField(default=1)
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "location_search_history"
        ordering = ["-use_count", "-last_used_at"]
        unique_together = [("company", "location_text")]
        indexes = [
            models.Index(fields=["company", "-use_count", "-last_used_at"]),
        ]

    def __str__(self):
        return f"{self.company_id}: {self.location_text} (×{self.use_count})"
