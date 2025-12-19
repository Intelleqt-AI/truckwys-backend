from django.contrib import admin
from .models import (
    User, Vehicle, VehicleLog, Load, Quote, Driver,
    Customer, Invoice, Payment, Expense, Notification, Settlement
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
    list_display = ['make', 'model', 'year', 'plate', 'status']
    list_filter = ['status', 'type']
    search_fields = ['vin', 'plate', 'make', 'model']

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
