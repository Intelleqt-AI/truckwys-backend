from django import forms
from django.contrib import admin
from .models import (
    User, Vehicle, VehicleType, VehicleLog, Load, Quote, Driver,
    Customer, Invoice, Payment, Expense, Notification, Settlement, Company,
    Trip, Facility, RiskScore, AdvanceRequest, AuditLog, FuelPrice, TollPlaza
)
from .models.border_crossing_fee import BorderCrossingFee
from .models.verified_rate import VerifiedRate
from .models.country_transit_rate import CountryTransitRate
from .models.integration_api_key import IntegrationAPIKey

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ['username', 'email', 'role', 'is_active', 'created_at']
    list_filter = ['role', 'is_active']
    search_fields = ['username', 'email', 'first_name', 'last_name']

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'company', 'email', 'phone', 'status']
    list_filter = ['status']
    search_fields = ['name', 'company', 'email']

@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ['user', 'license_number', 'license_expiry', 'status']
    list_filter = ['status']
    search_fields = ['user__username', 'license_number']

@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ['make', 'model', 'vehicle_type', 'year', 'plate', 'status']
    list_filter = ['status', 'vehicle_type', 'type']
    search_fields = ['vin', 'plate', 'make', 'model']


@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'capacity', 'max_distance', 'base_rate', 'sanral_toll_class', 'active']
    list_filter = ['active', 'sanral_toll_class']
    search_fields = ['name', 'description']

@admin.register(VehicleLog)
class VehicleLogAdmin(admin.ModelAdmin):
    list_display = ['vehicle', 'log_type', 'date', 'cost']
    list_filter = ['log_type', 'date']

@admin.register(Load)
class LoadAdmin(admin.ModelAdmin):
    list_display = ['load_number', 'customer', 'driver', 'status', 'pickup_date']
    list_filter = ['status', 'pickup_date']
    search_fields = ['load_number', 'customer__name']

@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ['quote_number', 'customer', 'status', 'total_amount', 'valid_until']
    list_filter = ['status']
    search_fields = ['quote_number', 'customer__name']

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_number', 'customer', 'status', 'total_amount', 'due_date']
    list_filter = ['status']
    search_fields = ['invoice_number', 'customer__name']

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['payment_number', 'customer', 'amount', 'payment_date', 'payment_method']
    list_filter = ['payment_method', 'payment_date']
    search_fields = ['payment_number', 'customer__name']

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ['expense_number', 'category', 'amount', 'expense_date']
    list_filter = ['category', 'expense_date']
    search_fields = ['expense_number', 'vendor']

@admin.register(Settlement)
class SettlementAdmin(admin.ModelAdmin):
    list_display = ['settlement_number', 'driver', 'status', 'net_pay', 'start_date', 'end_date']
    list_filter = ['status']
    search_fields = ['settlement_number', 'driver__user__username']

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['user', 'title', 'type', 'is_read', 'created_at']
    list_filter = ['type', 'is_read']
    search_fields = ['title', 'user__username']

@admin.action(description='Run billing sweeps now (monthly fee + delivery-fee retry + grace-period + pending-cancellation check)')
def run_billing_sweeps(modeladmin, request, queryset):
    """Manual trigger for the four Celery Beat billing crons, scoped to the
    selected companies where possible — lets someone test the state machine
    entirely by clicking, without waiting for the daily schedule or opening a
    terminal. Same functions Celery Beat calls; nothing test-only about them."""
    from core.services.subscription_billing import (
        charge_monthly_subscription_fee, check_grace_period_expirations, check_pending_cancellations,
    )
    from core.services.delivery_fee_billing import retry_failed_delivery_fee_charges

    monthly_results = [charge_monthly_subscription_fee(c) for c in queryset]
    retry_summary = retry_failed_delivery_fee_charges()  # not company-scoped — sweeps all failed charges
    grace_summary = check_grace_period_expirations()      # not company-scoped — sweeps all grace periods
    cancel_summary = check_pending_cancellations()        # not company-scoped — sweeps all pending cancellations
    modeladmin.message_user(
        request,
        f'Monthly fee ({len(monthly_results)} co.): {monthly_results} | '
        f'Delivery-fee retry (all companies): {retry_summary} | '
        f'Grace-period check (all companies): {grace_summary} | '
        f'Pending-cancellation check (all companies): {cancel_summary}',
    )


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ['company_name', 'vat_registered', 'vat_number', 'subscription_status', 'cancel_at_period_end', 'subscription_plan', 'grace_period_expires_at', 'next_billing_date', 'updated_at']
    list_filter = ['vat_registered', 'subscription_status', 'cancel_at_period_end', 'subscription_plan']
    search_fields = ['company_name', 'registration_number', 'vat_number']
    actions = [run_billing_sweeps, 'mark_vat_registered', 'mark_not_vat_registered']

    # A company that isn't a SARS VAT vendor may not charge VAT: switching it
    # off issues new invoices as INVOICE without VAT. Past invoices never change.
    @admin.action(description='Mark as VAT registered (charges 15% VAT)')
    def mark_vat_registered(self, request, queryset):
        n = queryset.update(vat_registered=True)
        self.message_user(request, f'{n} marked as VAT registered.')

    @admin.action(description='Mark as not VAT registered (no VAT on new invoices)')
    def mark_not_vat_registered(self, request, queryset):
        n = queryset.update(vat_registered=False)
        self.message_user(request, f'{n} marked as not VAT registered.')


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ['id', 'load', 'vehicle', 'driver', 'status', 'start_time', 'end_time', 'pod_uploaded']
    list_filter = ['status', 'pod_type', 'pod_verified']
    search_fields = ['load__load_number', 'vehicle__plate', 'driver__user__username']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ['id', 'company', 'limit', 'outstanding', 'reserved', 'utilization_percent', 'status']
    list_filter = ['status']
    search_fields = ['company__company_name']
    # outstanding/reserved are moved only by core.services.facility_ledger.
    readonly_fields = ['created_at', 'updated_at', 'utilization_percent', 'available',
                       'outstanding', 'reserved']


@admin.register(RiskScore)
class RiskScoreAdmin(admin.ModelAdmin):
    list_display = ['id', 'invoice', 'customer', 'total_score', 'tier', 'fee_percent', 'is_eligible', 'calculated_at']
    list_filter = ['tier', 'is_eligible']
    search_fields = ['invoice__invoice_number', 'customer__name']
    readonly_fields = ['calculated_at', 'created_at']


@admin.register(AdvanceRequest)
class AdvanceRequestAdmin(admin.ModelAdmin):
    list_display = ['id', 'invoice', 'facility', 'amount', 'fee_amount', 'net_amount', 'status', 'requested_at']
    list_filter = ['status']
    search_fields = ['invoice__invoice_number']
    # Status and capacity change only through the lifecycle (facility_ledger);
    # editing them here would desync the facility ledger.
    readonly_fields = ['created_at', 'updated_at', 'status', 'capacity_reserved',
                       'settlement_reference', 'settlement_payment', 'settled_by']


class _SafeWebhookURLForm(forms.ModelForm):
    """Applies the webhook SSRF rule to whichever URL field the model has."""
    _url_field = 'webhook_url'

    def clean(self):
        cleaned = super().clean()
        url = cleaned.get(self._url_field)
        if url:
            from core.services.webhook_url import is_safe_webhook_url, MESSAGE
            if not is_safe_webhook_url(url):
                self.add_error(self._url_field, MESSAGE)
        return cleaned


@admin.register(IntegrationAPIKey)
class IntegrationAPIKeyAdmin(admin.ModelAdmin):
    form = _SafeWebhookURLForm
    """Where platform staff bind a LENDER key to the transporters it funds."""
    list_display = ['id', 'name', 'key_type', 'operator', 'active', 'last_used_at']
    list_filter = ['key_type', 'active']
    search_fields = ['name', 'operator__username']
    filter_horizontal = ['allowed_companies']
    readonly_fields = ['key', 'created_at', 'last_used_at', 'usage_count', 'quota_used', 'quota_period']




@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'action', 'resource_type', 'resource_id', 'ip_address', 'created_at']
    list_filter = ['action', 'resource_type']
    search_fields = ['user__email', 'resource_type', 'resource_id']
    readonly_fields = ['created_at']


@admin.register(FuelPrice)
class FuelPriceAdmin(admin.ModelAdmin):
    # To override a price by hand, set source to MANUAL: automated refreshes
    # never overwrite a MANUAL row (they do overwrite any other live source).
    list_display = ['date', 'diesel_inland', 'diesel_coastal', 'diesel_grade', 'effective_from',
                    'petrol_95', 'petrol_93', 'source', 'fetched_at', 'fetch_failed_at']
    list_filter = ['source', 'diesel_grade']
    search_fields = ['date']
    readonly_fields = ['created_at', 'updated_at']


class TollTariffInline(admin.TabularInline):
    from .models.toll_plaza import TollTariff as model
    extra = 0
    fields = ['effective_from', 'effective_to', 'tariff_class_2', 'tariff_class_3', 'tariff_class_4',
              'tariff_class_5', 'source_name', 'source_url']


@admin.register(TollPlaza)
class TollPlazaAdmin(admin.ModelAdmin):
    inlines = [TollTariffInline]
    list_display = ['name', 'route', 'plaza_type', 'plaza_group', 'operator', 'country', 'location_km', 'tariff_class_3', 'tariff_class_4', 'tariff_class_5', 'tariff_year',
                    'tariff_effective_from', 'tariff_verified_at', 'is_active']
    list_filter = ['route', 'plaza_type', 'operator', 'country', 'is_active', 'tariff_year', 'tariff_verified_at']
    search_fields = ['name', 'direction']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(BorderCrossingFee)
class BorderCrossingFeeAdmin(admin.ModelAdmin):
    list_display = ['from_country', 'to_country', 'fee_zar', 'notes', 'is_active', 'updated_at']
    list_filter = ['is_active', 'from_country']
    search_fields = ['from_country', 'to_country', 'notes']
    readonly_fields = ['updated_at']


@admin.register(CountryTransitRate)
class CountryTransitRateAdmin(admin.ModelAdmin):
    list_display = ['country_code', 'country_name', 'toll_rate_per_km', 'sa_border_distance_km', 'is_active', 'updated_at']
    # No weighbridge fees exist; the column stays at R0 and is not editable.
    exclude = ['weighbridge_fee_zar']
    list_filter = ['is_active']
    search_fields = ['country_code', 'country_name']
    readonly_fields = ['updated_at']


@admin.register(VerifiedRate)
class VerifiedRateAdmin(admin.ModelAdmin):
    """Stored figures for the AI quote price check and the refresh job's
    proposals. Approve / reject through the actions (they apply the figure
    exactly like /api/v1/admin/verified-rates/<id>/approve/); a new driver
    allowance can be added here as PENDING and then approved."""
    list_display = ['kind', 'label', 'value', 'published_value', 'previous_value', 'effective_from', 'status',
                    'verified_at', 'proposed_by', 'created_at']
    list_filter = ['kind', 'status']
    search_fields = ['key', 'label', 'source_name', 'source_url']
    readonly_fields = ['status', 'approved_by', 'approved_at', 'rejected_by', 'rejected_at', 'refresh_run',
                       'created_at', 'updated_at']
    actions = ['approve_selected', 'reject_selected']

    def save_model(self, request, obj, form, change):
        # Rows are created pending; only the approve action applies them.
        if not change:
            obj.status = VerifiedRate.STATUS_PENDING
            obj.proposed_by = obj.proposed_by or f'admin:{request.user.username}'[:100]
        super().save_model(request, obj, form, change)

    @admin.action(description='Approve selected pending proposals')
    def approve_selected(self, request, queryset):
        from core.services import verified_rates
        for rate in queryset.filter(status=VerifiedRate.STATUS_PENDING):
            try:
                verified_rates.approve(rate.id, request.user)
            except verified_rates.ReviewError as exc:
                self.message_user(request, f'{rate}: {exc}', level='error')

    @admin.action(description='Reject selected pending proposals')
    def reject_selected(self, request, queryset):
        from core.services import verified_rates
        for rate in queryset.filter(status=VerifiedRate.STATUS_PENDING):
            verified_rates.reject(rate.id, request.user)


# Foundation (accounting). Read-mostly: money moves through the services
# (ledger, credit notes, payments), never by editing rows here.
from .models import CreditNote, Supplier, DocumentSequence, DebtorIdentity  # noqa: E402


@admin.register(CreditNote)
class CreditNoteAdmin(admin.ModelAdmin):
    list_display = ['credit_note_number', 'company', 'invoice', 'issue_date', 'total_amount', 'status']
    list_filter = ['status']
    search_fields = ['credit_note_number', 'invoice__invoice_number']
    readonly_fields = [f.name for f in CreditNote._meta.fields]


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ['name', 'company', 'vat_number', 'category', 'is_active']
    search_fields = ['name', 'vat_number']


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ['company', 'doc_type', 'prefix', 'next_number', 'padding']


@admin.register(DebtorIdentity)
class DebtorIdentityAdmin(admin.ModelAdmin):
    list_display = ['registration_number', 'vat_number', 'legal_name_key', 'country']
    search_fields = ['registration_number', 'vat_number', 'legal_name_key']


# ---- Accounting integrations (core.accounting). Tokens are never shown.
from .models.accounting import (  # noqa: E402
    AccountingConnection, AccountingSyncEvent, AccountingWebhookEvent, ExternalLink, ReconciliationRun,
)


@admin.register(AccountingConnection)
class AccountingConnectionAdmin(admin.ModelAdmin):
    list_display = ['company', 'provider', 'status', 'tenant_name', 'connected_at', 'last_payment_sync_at']
    list_filter = ['provider', 'status']
    search_fields = ['company__company_name', 'tenant_name', 'tenant_id']
    exclude = ['access_token', 'refresh_token']
    readonly_fields = ['tenant_id', 'provider_connection_id', 'access_token_expires_at', 'refresh_token_expires_at',
                       'scopes', 'cursors', 'backfill', 'connected_at', 'disconnected_at', 'created_at', 'updated_at']


@admin.register(ExternalLink)
class ExternalLinkAdmin(admin.ModelAdmin):
    list_display = ['company', 'provider', 'object_type', 'local_id', 'external_number', 'status', 'attempts',
                    'last_synced_at']
    list_filter = ['provider', 'object_type', 'status']
    search_fields = ['external_id', 'external_number', 'last_error']


@admin.register(AccountingSyncEvent)
class AccountingSyncEventAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'company', 'level', 'action', 'label', 'message']
    list_filter = ['level', 'action']


@admin.register(AccountingWebhookEvent)
class AccountingWebhookEventAdmin(admin.ModelAdmin):
    list_display = ['received_at', 'provider', 'tenant_id', 'resource_type', 'resource_id', 'processed_at', 'error']
    list_filter = ['provider', 'resource_type']


@admin.register(ReconciliationRun)
class ReconciliationRunAdmin(admin.ModelAdmin):
    list_display = ['ran_at', 'company', 'status', 'difference_count']


from .models.webhook_subscription import WebhookSubscription as _WebhookSubscription


@admin.register(_WebhookSubscription)
class WebhookSubscriptionAdmin(admin.ModelAdmin):
    """Where platform staff review partner webhook subscriptions and bind each
    to ONE company. Outbound events go only to the event company's own
    subscriptions; an unbound subscription receives nothing. Non-superuser
    staff only see and assign their own company."""
    list_display = ['id', 'partner_name', 'company', 'webhook_url', 'is_active', 'fleet_write_enabled',
                    'allow_legacy_signature', 'last_delivery_at', 'failure_count']
    list_filter = ['is_active', 'fleet_write_enabled']

    def get_readonly_fields(self, request, obj=None):
        # Fleet write access and legacy signatures are platform decisions:
        # only superusers change them.
        ro = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            ro += ['fleet_write_enabled', 'allow_legacy_signature']
        return ro
    search_fields = ['partner_name', 'webhook_url', 'company__company_name']
    form = _SafeWebhookURLForm
    readonly_fields = ['api_key', 'secret', 'created_at', 'updated_at', 'last_delivery_at', 'failure_count']

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('company')
        if request.user.is_superuser:
            return qs
        return qs.filter(company_id=getattr(request.user, 'company_id', None) or -1)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'company' and not request.user.is_superuser:
            from .models import Company
            kwargs['queryset'] = Company.objects.filter(pk=getattr(request.user, 'company_id', None) or -1)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            obj.company_id = getattr(request.user, 'company_id', None)
        super().save_model(request, obj, form, change)
