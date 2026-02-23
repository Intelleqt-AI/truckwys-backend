"""Trip model for tracking vehicle journeys and deliveries."""

from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from .load import Load
from .vehicle import Vehicle
from .driver import Driver


class Trip(models.Model):
    """
    Represents a trip/journey for a load delivery.

    Links a load with specific vehicle and driver assignment,
    tracking the journey details, POD (Proof of Delivery), and actual costs.
    """

    STATUS_CHOICES = [
        ('PLANNED', 'Planned'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    POD_TYPE_CHOICES = [
        ('E_SIGNATURE', 'E-Signature'),
        ('PHOTO', 'Photo'),
        ('MANUAL', 'Manual'),
        ('PENDING', 'Pending'),
    ]

    # Core relationships
    load = models.ForeignKey(
        Load,
        on_delete=models.PROTECT,
        related_name='trips',
        help_text='The load/order this trip is delivering'
    )
    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.PROTECT,
        related_name='trips',
        help_text='Vehicle assigned to this trip'
    )
    driver = models.ForeignKey(
        Driver,
        on_delete=models.PROTECT,
        related_name='trips',
        help_text='Driver assigned to this trip'
    )

    # Route details
    origin = models.CharField(
        max_length=500,
        help_text='Starting location of the trip'
    )
    destination = models.CharField(
        max_length=500,
        help_text='Destination location of the trip'
    )
    distance_km = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text='Actual distance traveled in kilometers'
    )
    estimated_distance_km = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text='Estimated distance in kilometers'
    )

    # Time tracking
    start_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Actual trip start time'
    )
    end_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Actual trip end time'
    )
    estimated_duration_hours = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text='Estimated trip duration in hours'
    )

    # Status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PLANNED',
        db_index=True
    )

    # Proof of Delivery (POD)
    pod_uploaded = models.BooleanField(
        default=False,
        help_text='Whether POD has been uploaded'
    )
    pod_file = models.FileField(
        upload_to='pod_files/%Y/%m/%d/',
        null=True,
        blank=True,
        help_text='Uploaded POD file (signature image or document)'
    )
    pod_type = models.CharField(
        max_length=20,
        choices=POD_TYPE_CHOICES,
        default='PENDING',
        help_text='Type of POD submitted'
    )
    pod_verified = models.BooleanField(
        default=False,
        help_text='Whether POD has been verified by admin'
    )
    pod_quality_score = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(15)],
        help_text='POD quality score (0-15) used in risk calculation'
    )

    # Actual costs
    actual_fuel_litres = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text='Actual fuel consumed in litres'
    )
    actual_toll_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text='Actual toll costs in ZAR'
    )

    # Additional info
    notes = models.TextField(
        blank=True,
        help_text='Additional notes about the trip'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'trips'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['start_time']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self) -> str:
        return f"Trip {self.id} - {self.load.load_number} ({self.status})"

    @property
    def actual_duration_hours(self) -> float | None:
        """Calculate actual trip duration in hours."""
        if self.start_time and self.end_time:
            delta = self.end_time - self.start_time
            return round(delta.total_seconds() / 3600, 2)
        return None

    @property
    def is_completed(self) -> bool:
        """Check if trip is completed."""
        return self.status == 'COMPLETED'

    @property
    def has_pod(self) -> bool:
        """Check if trip has a valid POD."""
        return self.pod_uploaded and self.pod_type != 'PENDING'

    def calculate_pod_quality_score(self) -> int:
        """
        Calculate POD quality score (0-15 points).

        E-Signature: 15 points
        Photo: 12 points
        Manual: 8 points
        Pending/None: 0 points

        Returns:
            int: Quality score from 0 to 15
        """
        if not self.pod_uploaded or self.pod_type == 'PENDING':
            return 0

        score_map = {
            'E_SIGNATURE': 15,
            'PHOTO': 12,
            'MANUAL': 8,
        }

        return score_map.get(self.pod_type, 0)

    def save(self, *args, **kwargs) -> None:
        """Override save to auto-calculate POD quality score."""
        # Auto-calculate POD quality score if not manually set
        if self.pod_uploaded and self.pod_quality_score == 0:
            self.pod_quality_score = self.calculate_pod_quality_score()

        super().save(*args, **kwargs)
