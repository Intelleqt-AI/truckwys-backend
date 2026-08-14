from django.contrib import admin
from .models import (
    User, Vehicle, VehicleType, VehicleLog, Load, Quote, Driver,
    Customer, Invoice, Payment, Expense, Notification, Settlement, Company,
    Trip, Facility, RiskScore, AdvanceRequest, AuditLog, FuelPrice, TollPlaza
)
from .models.border_crossing_fee import BorderCrossingFee
from .models.country_transit_rate import CountryTransitRate

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
    list_display = ['name', 'capacity', 'max_distance', 'base_rate', 'active']
    list_filter = ['active']
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
    list_display = ['company_name', 'subscription_status', 'cancel_at_period_end', 'subscription_plan', 'grace_period_expires_at', 'next_billing_date', 'updated_at']
    list_filter = ['subscription_status', 'cancel_at_period_end', 'subscription_plan']
    search_fields = ['company_name', 'registration_number']
    actions = [run_billing_sweeps]


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ['id', 'load', 'vehicle', 'driver', 'status', 'start_time', 'end_time', 'pod_uploaded']
    list_filter = ['status', 'pod_type', 'pod_verified']
    search_fields = ['load__load_number', 'vehicle__plate', 'driver__user__username']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ['id', 'company', 'limit', 'outstanding', 'utilization_percent', 'status']
    list_filter = ['status']
    search_fields = ['company__company_name']
    readonly_fields = ['created_at', 'updated_at', 'utilization_percent', 'available']


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
    readonly_fields = ['created_at', 'updated_at']


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'action', 'resource_type', 'resource_id', 'ip_address', 'created_at']
    list_filter = ['action', 'resource_type']
    search_fields = ['user__email', 'resource_type', 'resource_id']
    readonly_fields = ['created_at']


@admin.register(FuelPrice)
class FuelPriceAdmin(admin.ModelAdmin):
    list_display = ['date', 'diesel_inland', 'diesel_coastal', 'petrol_95', 'petrol_93', 'source']
    list_filter = ['source']
    search_fields = ['date']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(TollPlaza)
class TollPlazaAdmin(admin.ModelAdmin):
    list_display = ['name', 'route', 'location_km', 'tariff_class_3', 'tariff_class_4', 'tariff_class_5', 'tariff_year', 'is_active']
    list_filter = ['route', 'is_active', 'tariff_year']
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
    list_display = ['country_code', 'country_name', 'weighbridge_fee_zar', 'toll_rate_per_km', 'sa_border_distance_km', 'is_active', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['country_code', 'country_name']
    readonly_fields = ['updated_at']
