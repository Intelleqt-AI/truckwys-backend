from django.db import models

class Company(models.Model):
    company_name = models.CharField(max_length=200)
    registration_number = models.CharField(max_length=100, blank=True)
    vat_number = models.CharField(max_length=100, blank=True)
    industry = models.CharField(max_length=100, default='logistics')
    website = models.URLField(blank=True)
    description = models.TextField(blank=True)
    logo = models.ImageField(upload_to='company_logos/', null=True, blank=True)
    
    # Using JSONField for flexible nested structures as requested in the prompt
    address = models.JSONField(default=dict)
    contact = models.JSONField(default=dict)

    # NEW: Fuel price for expense calculations
    fuel_price_per_litre = models.DecimalField(
        max_digits=6, decimal_places=2, default=23.50,
        help_text='Current fuel price per litre in ZAR (default: R23.50)'
    )

    # Quote defaults — configurable per company
    default_base_rate_per_km = models.DecimalField(
        max_digits=8, decimal_places=2, default=10.00,
        help_text='Default base rate per km used when creating a new quote (ZAR)'
    )
    weight_surcharge_threshold_kg = models.IntegerField(
        default=5000,
        help_text='Cargo weight above which a surcharge is applied (kg)'
    )
    weight_surcharge_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=15.00,
        help_text='Weight surcharge percentage applied to base cost when threshold is exceeded'
    )
    default_sla_hours = models.IntegerField(
        default=48,
        help_text='Default SLA delivery time in hours'
    )
    default_quote_validity_days = models.IntegerField(
        default=7,
        help_text='Default number of days a quote remains valid'
    )
    allow_cross_border = models.BooleanField(
        default=True,
        help_text='Whether cross-border routes are enabled for this company'
    )

    # Revenue Guard thresholds — configurable per company
    margin_at_risk_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=5.00,
        help_text='Margin % below which a quote is flagged AT RISK (default: 5%)'
    )
    margin_caution_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=12.00,
        help_text='Margin % below which a quote is flagged CAUTION (default: 12%)'
    )
    margin_target_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=10.00,
        help_text='Target margin % used in price-increase suggestions (default: 10%)'
    )
    default_toll_rate_per_km = models.DecimalField(
        max_digits=6, decimal_places=3, default=0.500,
        help_text='Fallback toll cost in ZAR/km used when TomTom returns no toll data (default: R0.50/km)'
    )

    # Risk engine fields
    cipc_age_years = models.IntegerField(
        default=5,
        help_text='Years since CIPC registration'
    )
    annual_turnover = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=5000000.00,
        help_text='ZAR annual turnover'
    )
    turnover_trend = models.CharField(
        max_length=20,
        default='stable',
        choices=[
            ('growing', 'Growing'),
            ('stable', 'Stable'),
            ('declining', 'Declining')
        ],
        help_text='Annual turnover trend'
    )
    fleet_size = models.IntegerField(
        default=10,
        help_text='Number of vehicles in fleet'
    )
    province_count = models.IntegerField(
        default=1,
        help_text='Number of provinces operated in'
    )
    business_type = models.CharField(
        max_length=20,
        default='owner_operator',
        choices=[
            ('owner_operator', 'Owner Operator'),
            ('fleet_operator', 'Fleet Operator'),
            ('broker', 'Broker')
        ],
        help_text='Type of business operation'
    )
    sub_sector = models.CharField(
        max_length=20,
        default='general_freight',
        choices=[
            ('general_freight', 'General Freight'),
            ('refrigerated', 'Refrigerated'),
            ('hazmat', 'Hazmat'),
            ('abnormal', 'Abnormal'),
            ('container', 'Container')
        ],
        help_text='Freight sub-sector specialization'
    )
    insurance_status = models.CharField(
        max_length=20,
        default='comprehensive',
        choices=[
            ('comprehensive', 'Comprehensive'),
            ('basic', 'Basic'),
            ('none', 'None')
        ],
        help_text='Insurance coverage level'
    )
    b_bbee_level = models.IntegerField(
        null=True,
        blank=True,
        default=4,
        help_text='B-BBEE level (1-8)'
    )

    # NEW: Xero integration fields (Phase 4)
    xero_access_token = models.TextField(
        blank=True,
        null=True,
        help_text='Xero OAuth access token (encrypted in production)'
    )
    xero_refresh_token = models.TextField(
        blank=True,
        null=True,
        help_text='Xero OAuth refresh token (encrypted in production)'
    )
    xero_tenant_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text='Xero tenant/organization ID'
    )
    xero_connected_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When Xero was connected'
    )
    xero_token_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the current Xero access token expires'
    )
    xero_last_invoice_sync = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Last time invoices were pushed to Xero'
    )
    xero_last_payment_sync = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Last time payments were pulled from Xero'
    )

    # Billing / subscription fields
    subscription_plan = models.CharField(
        max_length=20,
        choices=[
            ('free', 'Free'),
            ('starter', 'Starter'),
            ('pro', 'Pro'),  # Changed from 'professional' to match middleware
            ('enterprise', 'Enterprise'),
        ],
        default='free',
    )
    subscription_status = models.CharField(
        max_length=20,
        choices=[
            ('none', 'None'),
            ('trialing', 'Trialing'),
            ('active', 'Active'),
            ('past_due', 'Past Due'),
            ('cancelled', 'Cancelled'),
        ],
        default='none',
    )
    payfast_token = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text='PayFast subscription token',
    )
    subscription_start = models.DateTimeField(null=True, blank=True)
    subscription_end = models.DateTimeField(null=True, blank=True)

    # API usage tracking for plan limits (T1.3)
    api_calls_this_month = models.IntegerField(
        default=0,
        help_text='Number of API calls made this month (resets monthly)'
    )
    api_calls_reset_date = models.DateField(
        null=True,
        blank=True,
        help_text='Date when API call counter was last reset'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'company_profile'
        verbose_name = 'Company Profile'
        verbose_name_plural = 'Company Profile'

    def __str__(self):
        return self.company_name
