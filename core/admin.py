from django.contrib import admin
from .models import (
    User, Vehicle, VehicleType, VehicleLog, Load, Quote, Driver,
    Customer, Invoice, Payment, Expense, Notification, Settlement, Company,
    Trip, Facility, RiskScore, AdvanceRequest, AuditLog
)

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

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ['company_name', 'registration_number', 'vat_number', 'updated_at']
    search_fields = ['company_name', 'registration_number']


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
