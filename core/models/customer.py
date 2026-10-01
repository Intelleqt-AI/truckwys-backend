from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from datetime import date


class Customer(models.Model):
    # Days a customer is given to pay. 14 and 45 were already in use across the
    # seeders and in real customer lists before they were choices here, so a
    # 45-day customer could not be imported and, worse, was invoiced at 30 —
    # chased a fortnight early. Anything NET<n> is understood when an invoice
    # is dated, so this list is what the UI offers, not a hard limit.
    PAYMENT_TERMS_DAYS = [7, 14, 30, 45, 60, 90]
    PAYMENT_TERMS_CHOICES = [(f'NET{d}', f'Net {d} Days') for d in PAYMENT_TERMS_DAYS]

    CREDIT_SCORE_SOURCE_CHOICES = [
        ('MANUAL', 'Manual Entry'),
        ('DNB', 'Dun & Bradstreet'),
        ('TRUCKWYS', 'TruckWys Internal'),
    ]

    name = models.CharField(max_length=200, db_index=True)
    company_name = models.CharField(max_length=200, blank=True)
    # The human you actually phone at that customer. Every fleet's spreadsheet
    # has this column; without it a pasted list had to either lose the contact
    # or put their name where the customer's belongs.
    contact_person = models.CharField(max_length=200, blank=True)
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="customers")
    # Scoped to the company, not global. A globally unique email meant two
    # different fleets could not both deal with the same customer — and on a
    # bulk import one tenant's rows would fail against rows they cannot even
    # see, with no way to diagnose it.
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    zip_code = models.CharField(max_length=20, blank=True)
    billing_address = models.TextField(blank=True)

    # Business identity. Normalised by the serializer (core.services.identity):
    # VAT 10 digits starting with 4; CIPC as YYYY/NNNNNN/NN.
    vat_number = models.CharField(max_length=20, blank=True, default='')
    registration_number = models.CharField(max_length=20, blank=True, default='', db_index=True)
    country = models.CharField(max_length=2, default='ZA', help_text='ISO 3166-1 alpha-2')
    # Normalised legal name ('abc logistics' for 'ABC Logistics (Pty) Ltd').
    legal_name_key = models.CharField(max_length=200, blank=True, default='', db_index=True)
    # Global identity shared across tenants for Capital only; never serialised.
    debtor_identity = models.ForeignKey('DebtorIdentity', on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='customers')

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

    # Risk engine payment history fields
    payment_consistency = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.75,
        help_text='Payment consistency ratio (0 to 1)'
    )
    dispute_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.02,
        help_text='Dispute rate ratio (0 to 1)'
    )
    avg_days_to_pay = models.IntegerField(
        default=30,
        help_text='Historical average days to pay invoices'
    )
    total_invoices_paid = models.IntegerField(
        default=10,
        help_text='Total number of invoices paid by this customer'
    )
    total_invoices_late = models.IntegerField(
        default=2,
        help_text='Total number of late payments'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'customers'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'email'],
                name='uniq_customer_email_per_company',
            ),
        ]
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['is_active']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f"{self.name} - {self.company_name}" if self.company_name else self.name

    def save(self, *args, **kwargs):
        from core.services.identity import legal_name_key, link_debtor_identity
        self.legal_name_key = legal_name_key(self.company_name or self.name)
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = set(update_fields) | {'legal_name_key'}
        super().save(*args, **kwargs)
        if self.registration_number or self.vat_number:
            try:
                identity = link_debtor_identity(self)
            except Exception:  # identity is best-effort; never block a customer save
                identity = None
            if identity is not None and identity.pk != self.debtor_identity_id:
                self.debtor_identity = identity
                type(self).objects.filter(pk=self.pk).update(debtor_identity=identity)

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
