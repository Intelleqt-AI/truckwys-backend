"""Accounting integrations (Xero, QuickBooks Online; Sage later).

Provider-neutral. TruckWys owns quotes, loads, POD and the operational
invoice (locked once SENT, corrected by credit notes). The accounting system
owns the ledger, bank reconciliation and PAYMENTS: while a company has an
ACTIVE connection, payments are recorded there and flow back into TruckWys
(core.accounting.settlements); manual payment entry is refused.

    AccountingConnection   one per company at a time (tokens encrypted)
    ExternalLink           TruckWys row <-> provider object, with sync state
    AccountingSyncEvent    activity / error log shown in the UI
    AccountingWebhookEvent webhook receipts (respond fast, process async)
    ReconciliationRun      nightly "TruckWys vs provider" comparison
    ReconciliationDifference  one row per difference > R0.01
"""
from django.conf import settings as django_settings
from django.db import models
from django.db.models import Q


class AccountingProvider(models.TextChoices):
    XERO = 'XERO', 'Xero'
    QBO = 'QBO', 'QuickBooks Online'


class AccountingConnection(models.Model):
    PENDING_ORG = 'PENDING_ORG'   # authorised; the user must pick one of several orgs
    ACTIVE = 'ACTIVE'
    NEEDS_REAUTH = 'NEEDS_REAUTH'  # refresh failed / tokens unreadable: reconnect
    DISABLED = 'DISABLED'          # disconnected (kept for history)
    STATUS_CHOICES = [
        (PENDING_ORG, 'Choose organisation'),
        (ACTIVE, 'Connected'),
        (NEEDS_REAUTH, 'Reconnect required'),
        (DISABLED, 'Disconnected'),
    ]
    LIVE_STATUSES = (PENDING_ORG, ACTIVE, NEEDS_REAUTH)

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='accounting_connections')
    provider = models.CharField(max_length=10, choices=AccountingProvider.choices)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING_ORG, db_index=True)
    status_reason = models.TextField(blank=True, default='')

    # Xero: tenantId (+ the /connections id, needed to DELETE the connection).
    # QBO: realmId.
    tenant_id = models.CharField(max_length=100, blank=True, default='', db_index=True)
    tenant_name = models.CharField(max_length=255, blank=True, default='')
    provider_connection_id = models.CharField(max_length=100, blank=True, default='')
    short_code = models.CharField(max_length=50, blank=True, default='')
    base_currency = models.CharField(max_length=3, blank=True, default='')
    country = models.CharField(max_length=2, blank=True, default='')
    # Orgs authorised in the same consent, while the user picks one:
    # [{tenant_id, name, connection_id, currency}]
    pending_tenants = models.JSONField(default=list, blank=True)

    # Encrypted at rest with core.utils.crypto (fail closed). Never serialised.
    access_token = models.TextField(blank=True, default='')
    refresh_token = models.TextField(blank=True, default='')
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True, default='')

    # Mapping, cut-over and per-connection options (validated by
    # core.accounting.mapping): revenue_types, expense_categories, tax_sales,
    # tax_purchases, receipts_account, tracking, cutover_date, options cache.
    settings = models.JSONField(default=dict, blank=True)

    # Backfill progress (core.accounting.backfill).
    backfill = models.JSONField(default=dict, blank=True)

    # Payment pull cursors: {'payments': iso, 'overpayments': iso, ...}
    cursors = models.JSONField(default=dict, blank=True)
    last_payment_sync_at = models.DateTimeField(null=True, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)

    connected_by = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+')
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'accounting_connections'
        ordering = ['-created_at']
        constraints = [
            # One live accounting connection per company (any provider).
            models.UniqueConstraint(fields=['company'], condition=Q(status__in=['PENDING_ORG', 'ACTIVE', 'NEEDS_REAUTH']),
                                    name='uniq_live_accounting_connection_per_company'),
            # An org can feed only one TruckWys company: webhooks are routed by
            # tenant id, and two companies posting into one ledger would double it.
            models.UniqueConstraint(fields=['provider', 'tenant_id'],
                                    condition=Q(status__in=['ACTIVE', 'NEEDS_REAUTH']) & ~Q(tenant_id=''),
                                    name='uniq_live_accounting_tenant'),
        ]

    def __str__(self):
        return f'{self.get_provider_display()} for company {self.company_id} ({self.status})'

    @property
    def is_active(self):
        return self.status == self.ACTIVE

    @property
    def cutover_date(self):
        from datetime import date
        raw = (self.settings or {}).get('cutover_date')
        return date.fromisoformat(raw) if raw else None


class ExternalLink(models.Model):
    """One TruckWys object <-> one provider object, plus its sync state.

    Pushes are idempotent: the provider id is stored the moment it exists,
    and last_hash is the hash of the payload last accepted, so a re-run
    skips unchanged documents."""
    CONTACT_CUSTOMER = 'CONTACT_CUSTOMER'
    CONTACT_SUPPLIER = 'CONTACT_SUPPLIER'
    INVOICE = 'INVOICE'
    CREDIT_NOTE = 'CREDIT_NOTE'
    BILL = 'BILL'
    PAYMENT = 'PAYMENT'            # historic TruckWys receipt pushed during backfill
    OVERPAYMENT = 'OVERPAYMENT'    # excess of a historic receipt, held as customer credit
    OBJECT_TYPES = [
        (CONTACT_CUSTOMER, 'Customer contact'), (CONTACT_SUPPLIER, 'Supplier contact'),
        (INVOICE, 'Invoice'), (CREDIT_NOTE, 'Credit note'), (BILL, 'Supplier bill'),
        (PAYMENT, 'Payment'), (OVERPAYMENT, 'Overpayment'),
    ]

    PENDING = 'PENDING'      # queued for push
    RUNNING = 'RUNNING'      # a worker holds it (stale after 15 min: re-queued)
    SYNCED = 'SYNCED'
    ERROR = 'ERROR'          # failed; will retry with backoff
    DEAD = 'DEAD'            # gave up after max attempts or permanent error: needs a human
    BLOCKED = 'BLOCKED'      # waiting on the user (mapping, contact confirmation)
    VOIDED = 'VOIDED'        # voided/deleted in the provider
    # Contact matching states (CONTACT_* only)
    SUGGESTED = 'SUGGESTED'  # name-only match, needs confirmation
    UNMATCHED = 'UNMATCHED'
    CREATE = 'CREATE'        # user chose (or no match found): create in provider
    SKIPPED = 'SKIPPED'      # user chose not to sync this contact
    STATUS_CHOICES = [(s, s.title()) for s in (PENDING, RUNNING, SYNCED, ERROR, DEAD, BLOCKED, VOIDED,
                                                SUGGESTED, UNMATCHED, CREATE, SKIPPED)]
    RETRYABLE = (ERROR,)

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='external_links')
    connection = models.ForeignKey(AccountingConnection, on_delete=models.CASCADE, related_name='links')
    provider = models.CharField(max_length=10, choices=AccountingProvider.choices)
    object_type = models.CharField(max_length=20, choices=OBJECT_TYPES)
    local_id = models.PositiveBigIntegerField()
    external_id = models.CharField(max_length=100, blank=True, default='')
    external_number = models.CharField(max_length=100, blank=True, default='')
    # QBO needs the SyncToken for updates; Xero ignores it.
    external_version = models.CharField(max_length=50, blank=True, default='')
    last_hash = models.CharField(max_length=64, blank=True, default='')
    last_synced_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    last_error = models.TextField(blank=True, default='')
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Contact matching: how the link was made, and candidates for the wizard.
    match_method = models.CharField(max_length=20, blank=True, default='')
    candidates = models.JSONField(default=list, blank=True)
    # Free-form provider detail (e.g. origin invoice of an overpayment).
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'accounting_external_links'
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(fields=['connection', 'object_type', 'local_id'],
                                    name='uniq_external_link_local'),
            models.UniqueConstraint(fields=['connection', 'object_type', 'external_id'],
                                    condition=~Q(external_id=''),
                                    name='uniq_external_link_remote'),
        ]
        indexes = [models.Index(fields=['company', 'object_type', 'local_id'])]

    def __str__(self):
        return f'{self.object_type}:{self.local_id} -> {self.provider}:{self.external_id or "?"} ({self.status})'


class AccountingSyncEvent(models.Model):
    INFO, WARNING, ERROR = 'INFO', 'WARNING', 'ERROR'
    LEVELS = [(INFO, 'Info'), (WARNING, 'Warning'), (ERROR, 'Error')]

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='accounting_events')
    connection = models.ForeignKey(AccountingConnection, on_delete=models.CASCADE, related_name='events')
    level = models.CharField(max_length=10, choices=LEVELS, default=INFO)
    action = models.CharField(max_length=40)
    object_type = models.CharField(max_length=20, blank=True, default='')
    local_id = models.PositiveBigIntegerField(null=True, blank=True)
    label = models.CharField(max_length=120, blank=True, default='')
    message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'accounting_sync_events'
        ordering = ['-created_at', '-id']


class AccountingWebhookEvent(models.Model):
    """Every webhook notification is stored, then processed by a task, so
    the endpoint can answer within the provider's deadline (Xero: 5 s)."""
    provider = models.CharField(max_length=10, choices=AccountingProvider.choices)
    tenant_id = models.CharField(max_length=100, db_index=True)
    resource_type = models.CharField(max_length=40)
    resource_id = models.CharField(max_length=100)
    event_type = models.CharField(max_length=40, blank=True, default='')
    event_at = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error = models.TextField(blank=True, default='')
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'accounting_webhook_events'
        ordering = ['received_at', 'id']


class ReconciliationRun(models.Model):
    OK, DIFFERENCES, FAILED = 'OK', 'DIFFERENCES', 'FAILED'

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='reconciliation_runs')
    connection = models.ForeignKey(AccountingConnection, on_delete=models.CASCADE, related_name='reconciliation_runs')
    ran_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, default=OK)
    error = models.TextField(blank=True, default='')
    checked = models.JSONField(default=dict, blank=True)
    difference_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'accounting_reconciliation_runs'
        ordering = ['-ran_at', '-id']


class ReconciliationDifference(models.Model):
    INVOICE, CUSTOMER, MONTH = 'INVOICE', 'CUSTOMER', 'MONTH'

    run = models.ForeignKey(ReconciliationRun, on_delete=models.CASCADE, related_name='differences')
    scope = models.CharField(max_length=10)
    key = models.CharField(max_length=100)
    label = models.CharField(max_length=255, blank=True, default='')
    field = models.CharField(max_length=40)
    truckwys_value = models.CharField(max_length=60, blank=True, default='')
    provider_value = models.CharField(max_length=60, blank=True, default='')
    difference = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    local_url = models.CharField(max_length=255, blank=True, default='')
    provider_url = models.CharField(max_length=500, blank=True, default='')

    class Meta:
        db_table = 'accounting_reconciliation_differences'
        ordering = ['scope', 'key', 'field']
