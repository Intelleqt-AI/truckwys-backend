from django.db import models

class Company(models.Model):
    company_name = models.CharField(max_length=200)
    registration_number = models.CharField(max_length=100, blank=True)
    vat_number = models.CharField(max_length=100, blank=True)
    industry = models.CharField(max_length=100, default='logistics')
    website = models.URLField(blank=True)
    description = models.TextField(blank=True)
    logo = models.ImageField(upload_to='company_logos/', null=True, blank=True)

    # Stamped once the admin skips or completes the post-signup onboarding
    # wizard — the durable, per-company source of truth for "don't show
    # onboarding again". Deliberately NOT keyed off vehicle count or any
    # other business state (vehicles are optional/added later), and NOT
    # solely a localStorage flag (that gets cleared on every logout, and
    # doesn't exist on a different browser/device at all).
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)
    
    # Using JSONField for flexible nested structures as requested in the prompt
    address = models.JSONField(default=dict)
    contact = models.JSONField(default=dict)

    # Default fuel prices, one per fuel type — used as the fallback price for
    # a VehicleType of that fuel type that doesn't have its own fuel_price
    # set. Diesel already has a live national-price feed elsewhere in the
    # system, so it keeps a sensible default; the other three have no such
    # feed, so they stay blank until the company sets a price manually.
    # decimal_places=4 (not 2) to match FuelPrice's own precision — the live
    # diesel feed's sub-cent values (e.g. 26.1721) get saved here verbatim
    # via the "Fetch Now" nudge, and rounding to 2dp before storage would
    # introduce a small but real, compounding error into fuel-cost
    # calculations that fall back to this field.
    fuel_price_per_litre = models.DecimalField(
        max_digits=8, decimal_places=4, default=23.50,
        help_text='Default Diesel price per litre in ZAR (default: R23.50)'
    )
    fuel_price_petrol = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Default Petrol price per litre in ZAR'
    )
    fuel_price_electric = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Default Electric price per kWh in ZAR'
    )
    fuel_price_hybrid = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text='Default Hybrid price per litre in ZAR'
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

    # Cartrack Fleet API integration fields (bring-your-own-account, mirrors Xero above)
    cartrack_username = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        help_text='Cartrack Fleet API username (Fleetweb > Settings > API Settings)'
    )
    cartrack_password = models.TextField(
        blank=True,
        null=True,
        help_text='Cartrack Fleet API password (encrypted at rest)'
    )
    cartrack_base_url = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        help_text='Regional Cartrack Fleet API base URL, e.g. https://fleetapi-za.cartrack.com'
    )
    cartrack_webhook_secret = models.TextField(
        blank=True,
        null=True,
        help_text='HMAC key used to verify X-Webhook-Signature on inbound Cartrack webhooks (encrypted at rest)'
    )
    # Nullable, no eager default: not used until the Phase 4 inbound webhook is
    # built. Generated lazily (uuid.uuid4()) the first time it's actually needed,
    # so existing rows don't collide on the unique constraint at migration time.
    cartrack_webhook_token = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
        help_text='Unique per-company token used in the inbound Cartrack webhook URL'
    )
    cartrack_connected_at = models.DateTimeField(null=True, blank=True)
    cartrack_last_status_sync = models.DateTimeField(
        null=True, blank=True,
        help_text='Last time vehicle status/location was polled from Cartrack'
    )
    cartrack_last_alert_sync = models.DateTimeField(
        null=True, blank=True,
        help_text='Watermark for GET /alerts/notifications polling'
    )

    # Billing / subscription fields
    subscription_plan = models.CharField(
        max_length=20,
        choices=[
            ('free', 'Free'),
            ('starter', 'Starter'),
            # The single flat-rate paid plan (see MONTHLY_FEE in services/paystack.py).
            ('pro', 'TruckWys Fleet'),
            ('enterprise', 'Enterprise'),
        ],
        default='free',
    )
    # State machine per TruckWys_Fee_Billing_Spec.pdf §4 — the ITN-equivalent
    # (Paystack charge_authorization result) webhook handler is the single
    # source of truth driving every transition (core.services.subscription_billing):
    #   active        billing current                    full access
    #   grace_period  most recent charge attempt failed   full access (temporary)
    #   suspended     grace period expired, unresolved     quoting/invoicing blocked
    #   cancelled     explicit cancellation                quoting/invoicing blocked
    # 'none'/'trialing' are pre-subscription values outside the spec's scope
    # (used for signup/free-tier bookkeeping before a company ever pays).
    subscription_status = models.CharField(
        max_length=20,
        choices=[
            ('none', 'None'),
            ('trialing', 'Trialing'),
            ('active', 'Active'),
            ('grace_period', 'Grace Period'),
            ('suspended', 'Suspended'),
            ('cancelled', 'Cancelled'),
        ],
        default='none',
    )
    cancel_at_period_end = models.BooleanField(
        default=False,
        help_text="Explicit cancellation was requested while still active/grace_period — access and billing "
                   "keep running until next_billing_date, at which point the daily "
                   "check_pending_cancellations sweep finalises subscription_status to 'cancelled'. Left False "
                   "once actually cancelled (a hard, immediate cancel from 'trialing' never sets this at all).",
    )

    # Paystack card-on-file — captured from the first (card-verifying) checkout
    # and reused for every later charge_authorization call: both the flat
    # monthly fee (core/services/subscription_billing.py) and the 0.25%
    # delivery take-rate (core/services/delivery_fee_billing.py).
    paystack_customer_code = models.CharField(max_length=255, blank=True, null=True)
    paystack_authorization_code = models.CharField(
        max_length=255, blank=True, null=True,
        help_text='Reusable Paystack authorization_code for this company\'s card on file',
    )
    paystack_authorization_email = models.EmailField(
        blank=True, null=True,
        help_text='Email the authorization was created with — Paystack requires an exact match on every charge',
    )
    paystack_card_last4 = models.CharField(max_length=4, blank=True)
    paystack_card_type = models.CharField(max_length=20, blank=True)
    paystack_bank = models.CharField(max_length=100, blank=True)

    subscription_start = models.DateTimeField(null=True, blank=True)
    subscription_end = models.DateTimeField(null=True, blank=True)
    next_billing_date = models.DateField(
        null=True, blank=True,
        help_text='Next date the flat monthly fee is due; advanced by core.services.subscription_billing',
    )
    # Display-only companion to next_billing_date, for a live countdown on
    # the billing page. Deliberately NOT used by any charging/idempotency
    # logic (which stays on next_billing_date, a plain calendar date, by
    # design) — this just carries the time-of-day precision a ticking
    # countdown needs. Normally this is midnight UTC on next_billing_date;
    # it only becomes meaningfully more precise than that on an accelerated
    # test cycle.
    next_billing_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Display-only timestamp version of next_billing_date, for a live countdown on the billing page.',
    )

    # TruckWys_Fee_Billing_Spec.pdf §7's suggested fields — stamped/managed by
    # core.services.subscription_billing's shared record_charge_* helpers,
    # called from every charge site (monthly fee, take-rate fee, both retries).
    last_charge_attempt_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Most recent charge attempt (subscription or take-rate), success or failure',
    )
    grace_period_expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set once on entering grace_period (now + grace days); cleared on return to active. '
                   'Past this timestamp with no successful charge, the company moves to suspended.',
    )

    # Quote ML flywheel — set when this company goes live for real, so
    # pre-launch/internal-testing QuoteOutcome rows (fake "accept" clicks
    # during demos) stop counting toward the win-model training threshold.
    # Null means "count everything" (unchanged behaviour for existing data).
    ai_training_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Outcomes recorded before this timestamp are excluded from win-model training/progress for this company',
    )

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
