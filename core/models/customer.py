from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from datetime import date


class Customer(models.Model):
    PAYMENT_TERMS_CHOICES = [
        ('NET30', 'Net 30 Days'),
        ('NET60', 'Net 60 Days'),
        ('NET90', 'Net 90 Days'),
    ]

    CREDIT_SCORE_SOURCE_CHOICES = [
        ('MANUAL', 'Manual Entry'),
        ('DNB', 'Dun & Bradstreet'),
        ('TRUCKWYS', 'TruckWys Internal'),
    ]

    name = models.CharField(max_length=200, db_index=True)
    company = models.CharField(max_length=200, blank=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20)
    address = models.TextField()
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)
    zip_code = models.CharField(max_length=20)
    billing_address = models.TextField(blank=True)

    # NEW: Structured payment terms
    payment_terms_default = models.CharField(
        max_length=20,
        choices=PAYMENT_TERMS_CHOICES,
        default='NET30',
        help_text='Default payment terms for this customer'
    )

    # Keep old field for backward compatibility
    payment_terms = models.CharField(max_length=100, blank=True)

    credit_limit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # NEW: Credit score tracking
    credit_score = models.IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Customer credit score (0-100)'
    )
    credit_score_source = models.CharField(
        max_length=20,
        choices=CREDIT_SCORE_SOURCE_CHOICES,
        default='MANUAL',
        help_text='Source of credit score data'
    )
    credit_score_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When credit score was last updated'
    )

    # NEW: Active status flag
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text='Whether customer account is active'
    )

    # Keep old status field for backward compatibility
    status = models.CharField(max_length=50, default='ACTIVE')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'customers'
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['is_active']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f"{self.name} - {self.company}" if self.company else self.name

    @property
    def relationship_months(self) -> int:
        """Calculate relationship length in months."""
        delta = date.today() - self.created_at.date()
        return max(0, delta.days // 30)

    @property
    def relationship_days(self) -> int:
        """Calculate relationship length in days."""
        delta = date.today() - self.created_at.date()
        return max(0, delta.days)

    def update_credit_score(self, score: int, source: str = 'MANUAL') -> None:
        """
        Update customer credit score.

        Args:
            score: New credit score (0-100)
            source: Source of the score
        """
        if not (0 <= score <= 100):
            raise ValueError("Credit score must be between 0 and 100")

        self.credit_score = score
        self.credit_score_source = source
        self.credit_score_updated_at = timezone.now()
        self.save()

    def deactivate(self) -> None:
        """Deactivate customer account."""
        self.is_active = False
        self.status = 'INACTIVE'
        self.save()

    def activate(self) -> None:
        """Activate customer account."""
        self.is_active = True
        self.status = 'ACTIVE'
        self.save()
