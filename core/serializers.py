from rest_framework import serializers
from django.db.models import Avg  # ADD THIS IMPORT
from .models import (
    User, Customer, Driver, Vehicle, VehicleLog, VehicleType, Load,
    Quote, Invoice, Payment, Expense, Settlement, Notification, Company, ActivityEvent
)

# User Serializer
class UserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='get_full_name', read_only=True)
    last_active = serializers.DateTimeField(source='last_login', read_only=True)
    # Declared as CharField (not the model ChoiceField) so we can normalise the
    # UI's lower-case role values to the model's upper-case choices.
    role = serializers.CharField(required=False)

    def validate_role(self, value):
        if not isinstance(value, str):
            return value
        normalized = value.upper()
        valid = {c[0] for c in User.ROLE_CHOICES}
        if normalized not in valid:
            raise serializers.ValidationError(f'"{value}" is not a valid role.')
        return normalized

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'password', 'first_name', 'last_name', 'name', 'job_title',
                  'role', 'status', 'phone', 'address', 'timezone', 'language', 'date_format',
                  'notification_settings', 'avatar', 'last_active', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at', 'last_active']
        extra_kwargs = {'password': {'write_only': True, 'required': False}}

    def create(self, validated_data):
        # Without this, password was silently dropped, leaving every API-created
        # user (e.g. drivers) unable to log in.
        password = validated_data.pop('password', None)
        user = User.objects.create_user(**validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=['password'])
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=['password'])
        return user


# Customer Serializer
class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = '__all__'
        read_only_fields = ['id', 'company', 'created_at', 'updated_at']
        # Address details are optional on quick-add (directory). They can be
        # filled in later from the full customer record.
        extra_kwargs = {
            'address': {'required': False, 'allow_blank': True, 'default': ''},
            'state': {'required': False, 'allow_blank': True, 'default': ''},
            'zip_code': {'required': False, 'allow_blank': True, 'default': ''},
            'city': {'required': False, 'allow_blank': True, 'default': ''},
            'phone': {'required': False, 'allow_blank': True, 'default': ''},
        }


# Driver Serializer
class DriverSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    assigned_vehicle = serializers.SerializerMethodField()
    total_trips = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = [
            'id', 'user', 'user_details', 'license_number', 'license_expiry',
            'license_state', 'medical_card_expiry', 'hire_date', 'status',
            'emergency_contact', 'emergency_phone', 'violation_count',
            'accident_history', 'experience_years', 'created_at', 'updated_at',
            # computed fields written by background task
            'efficiency_score', 'on_time_rate', 'safety_score', 'total_distance',
            'trips_this_month', 'revenue_generated', 'avg_revenue_per_trip',
            'margin_per_trip',
            # live helpers
            'total_trips', 'assigned_vehicle',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at',
            'efficiency_score', 'on_time_rate', 'safety_score', 'total_distance',
            'trips_this_month', 'revenue_generated', 'avg_revenue_per_trip',
            'margin_per_trip', 'total_trips', 'assigned_vehicle',
        ]

    def get_total_trips(self, obj):
        """Live count — cheap, used for the profile header before the task runs."""
        return obj.loads.filter(status__in=['DELIVERED', 'INVOICED']).count()

    def get_assigned_vehicle(self, obj):
        vehicle = obj.vehicles.first()
        if not vehicle:
            return None
        parts = [vehicle.plate]
        if vehicle.make or vehicle.model:
            parts.append(f"{vehicle.make or ''} {vehicle.model or ''}".strip())
        return ' — '.join(filter(None, parts))


# Vehicle Serializer
class VehicleSerializer(serializers.ModelSerializer):
    driver_name = serializers.SerializerMethodField()
    vehicle_type_name = serializers.CharField(source='vehicle_type.name', read_only=True)
    revenue_generated = serializers.SerializerMethodField()
    total_trips = serializers.SerializerMethodField()
    utilisation_rate = serializers.SerializerMethodField()
    type = serializers.CharField(max_length=50, required=False, default='TRUCK')
    vin = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')

    class Meta:
        model = Vehicle
        fields = [
            'id', 'company', 'vin', 'make', 'model', 'driver', 'vehicle_type',
            'year', 'plate', 'type', 'capacity', 'status', 'fuel_type', 'mileage',
            'service_interval_km', 'last_service_mileage',
            'last_maintenance_date', 'next_maintenance_due', 'insurance_expiry',
            'registration_expiry', 'ai_health_score', 'fuel_efficiency_score',
            'uptime_score', 'maintenance_score', 'uptime_percentage', 'cost_per_km',
            'margin_per_trip', 'fuel_consumption_per_km', 'created_at', 'updated_at',
            'driver_name', 'vehicle_type_name', 'revenue_generated', 'total_trips',
            'utilisation_rate'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'driver_name', 'vehicle_type_name',
                           'revenue_generated', 'total_trips', 'utilisation_rate']

    def get_driver_name(self, obj):
        if not obj.driver:
            return None
        u = obj.driver.user
        name = f"{u.first_name} {u.last_name}".strip()
        return name or u.username

    def get_revenue_generated(self, obj):
        """Calculate total revenue from all delivered loads for this vehicle."""
        from django.db.models import Sum
        total = obj.loads.filter(status='DELIVERED').aggregate(total=Sum('total_amount'))['total']
        return float(total) if total else 0.0

    def get_total_trips(self, obj):
        """Count total delivered loads for this vehicle."""
        return obj.loads.filter(status='DELIVERED').count()

    def get_utilisation_rate(self, obj):
        """Calculate utilization rate as percentage of loads in transit or delivered."""
        total_loads = obj.loads.count()
        if total_loads == 0:
            return 0.0
        active_loads = obj.loads.filter(status__in=['IN_TRANSIT', 'DELIVERED', 'LOADING', 'ASSIGNED']).count()
        return round((active_loads / total_loads) * 100, 2)


# VehicleType Serializer
class VehicleTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleType
        fields = '__all__'
        read_only_fields = ['id', 'company', 'created_at', 'updated_at']
        extra_kwargs = {
            'description': {'required': False, 'allow_blank': True, 'default': ''},
            'capacity': {'required': False, 'default': 0},
            'max_distance': {'required': False, 'default': 0},
            'base_rate': {'required': False, 'default': 0},
        }


# VehicleLog Serializer
class VehicleLogSerializer(serializers.ModelSerializer):
    vehicle_details = VehicleSerializer(source='vehicle', read_only=True)
    user_name = serializers.CharField(source='user.username', read_only=True)
    
    class Meta:
        model = VehicleLog
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


# Load Serializer
class LoadSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    driver_name = serializers.SerializerMethodField()
    vehicle_info = serializers.SerializerMethodField()
    quote_number = serializers.SerializerMethodField()

    class Meta:
        model = Load
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by', 'actual_delivered_at', 'company']

    def get_driver_name(self, obj):
        if not obj.driver:
            return None
        u = obj.driver.user
        name = f"{u.first_name} {u.last_name}".strip()
        return name or u.username

    def get_vehicle_info(self, obj):
        if obj.vehicle:
            return f"{obj.vehicle.make} {obj.vehicle.model} - {obj.vehicle.plate}"
        return None

    def get_quote_number(self, obj):
        if obj.quote:
            return obj.quote.quote_number
        return None


# Quote Serializer
class QuoteSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    quote_number = serializers.CharField(required=False, allow_blank=True)
    vehicle_display = serializers.SerializerMethodField()
    driver_display = serializers.SerializerMethodField()

    class Meta:
        model = Quote
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']

    def get_vehicle_display(self, obj):
        if obj.vehicle:
            return f"{obj.vehicle.make} {obj.vehicle.model} ({obj.vehicle.plate})"
        return None

    def get_driver_display(self, obj):
        if obj.driver:
            u = obj.driver.user
            name = f"{u.first_name} {u.last_name}".strip() or u.username
            return name
        return None


# Invoice Serializer
class InvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    load_number = serializers.CharField(source='load.load_number', read_only=True)
    
    class Meta:
        model = Invoice
        fields = '__all__'
        read_only_fields = ['id', 'company', 'created_at', 'updated_at']


# Payment Serializer
class PaymentSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    
    class Meta:
        model = Payment
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# Expense Serializer
class ExpenseSerializer(serializers.ModelSerializer):
    vehicle_info = serializers.CharField(source='vehicle.__str__', read_only=True)
    driver_name = serializers.CharField(source='driver.user.username', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = Expense
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']
        extra_kwargs = {'expense_number': {'required': False}}

    def create(self, validated_data):
        # Auto-generate a unique expense_number if the client didn't supply one.
        if not validated_data.get('expense_number'):
            import random
            from django.utils import timezone
            ts = timezone.now().strftime('%Y%m%d')
            num = f"EXP-{ts}-{random.randint(1000, 9999)}"
            while Expense.objects.filter(expense_number=num).exists():
                num = f"EXP-{ts}-{random.randint(1000, 9999)}"
            validated_data['expense_number'] = num
        return super().create(validated_data)


# Settlement Serializer
class SettlementSerializer(serializers.ModelSerializer):
    driver_name = serializers.CharField(source='driver.user.username', read_only=True)
    
    class Meta:
        model = Settlement
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# Notification Serializer
class NotificationSerializer(serializers.ModelSerializer):
    description = serializers.CharField(source='message')
    unread = serializers.BooleanField(source='is_read', read_only=True)
    
    class Meta:
        model = Notification
        fields = ['id', 'title', 'description', 'type', 'unread', 'link', 'created_at']
        read_only_fields = ['id', 'created_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['unread'] = not instance.is_read
        data['type'] = instance.type.lower()
        return data


# Company Serializer
class CompanySerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Company
        fields = [
            'company_name', 'registration_number', 'vat_number',
            'industry', 'website', 'description', 'logo_url',
            'address', 'contact',
            'default_base_rate_per_km', 'weight_surcharge_threshold_kg',
            'weight_surcharge_pct', 'default_sla_hours',
            'default_quote_validity_days', 'allow_cross_border',
            'fuel_price_per_litre',
            'margin_at_risk_pct', 'margin_caution_pct', 'margin_target_pct',
            'default_toll_rate_per_km',
        ]
    
    def get_logo_url(self, obj):
        if obj.logo:
            return obj.logo.url
        return "/brand/logo.svg" # Default as requested


# Quote Pipeline Serializer (NEW)
class QuotePipelineSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    price = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2, read_only=True)
    margin_pct = serializers.DecimalField(source='margin_percentage', max_digits=5, decimal_places=2, read_only=True)
    updated_at_iso = serializers.DateTimeField(source='updated_at', format='%Y-%m-%dT%H:%M:%SZ', read_only=True)
    confidence = serializers.SerializerMethodField()  # ADD THIS
    status = serializers.SerializerMethodField()  # ADD THIS
    
    class Meta:
        model = Quote
        fields = [
            'id', 'quote_number', 'customer', 'customer_name', 
            'origin', 'destination', 'sla_hours', 'price', 
            'margin_pct', 'confidence', 'status', 'updated_at', 'updated_at_iso'
        ]
        read_only_fields = ['id', 'updated_at']
    
    def get_confidence(self, obj):
        # Convert "HIGH" → "High", "MEDIUM" → "Medium"
        return obj.confidence.capitalize()
    
    def get_status(self, obj):
        # Convert "DRAFT" → "Draft", "SENT" → "Sent"
        return obj.status.capitalize()


# Driver Performance Serializer
class DriverPerformanceSerializer(serializers.ModelSerializer):
    driver_id = serializers.CharField(source='user.username')
    driver_name = serializers.SerializerMethodField()
    vehicle = serializers.SerializerMethodField()
    on_time_percentage = serializers.DecimalField(source='on_time_rate', max_digits=5, decimal_places=2, read_only=True)
    fuel_efficiency = serializers.IntegerField(source='efficiency_score', read_only=True)
    roi_score = serializers.SerializerMethodField()
    driver_status = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = [
            'id', 'driver_id', 'driver_name', 'vehicle', 'on_time_percentage',
            'safety_score', 'fuel_efficiency', 'margin_per_trip',
            'roi_score', 'driver_status', 'status',
        ]

    def get_driver_name(self, obj):
        name = f"{obj.user.first_name} {obj.user.last_name}".strip()
        return name or obj.user.username

    def get_vehicle(self, obj):
        vehicle = obj.vehicles.first()
        return vehicle.plate if vehicle else None

    def get_roi_score(self, obj):
        # Composite of stored scores — no fake randomness
        return int((float(obj.on_time_rate) * 0.4 + obj.safety_score * 0.35 + obj.efficiency_score * 0.25))

    def get_driver_status(self, obj):
        if obj.status != 'ACTIVE':
            return obj.status.replace('_', ' ').title()
        recent = obj.loads.order_by('-created_at').first()
        if recent and recent.status == 'IN_TRANSIT':
            return 'In Transit'
        if recent and recent.status == 'ASSIGNED':
            return 'Assigned'
        return 'Available'


class WebhookSerializer(serializers.ModelSerializer):
    """Serializer for Webhook model."""

    class Meta:
        from core.models import Webhook
        model = Webhook
        fields = [
            'id', 'url', 'secret', 'events', 'active',
            'failure_count', 'last_fired_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'secret', 'failure_count', 'last_fired_at', 'created_at', 'updated_at']


class IntegrationAPIKeySerializer(serializers.ModelSerializer):
    """Serializer for IntegrationAPIKey model."""

    class Meta:
        from core.models import IntegrationAPIKey
        model = IntegrationAPIKey
        fields = [
            'id', 'name', 'key', 'key_type', 'active',
            'created_at', 'last_used_at',
            'usage_count', 'monthly_quota', 'quota_used',
        ]
        read_only_fields = ['id', 'key', 'created_at', 'last_used_at', 'usage_count', 'quota_used']


class ActivityEventSerializer(serializers.ModelSerializer):
    """Serializer for ActivityEvent model."""

    class Meta:
        model = ActivityEvent
        fields = ['id', 'event_type', 'title', 'description', 'entity_id', 'entity_type', 'metadata', 'created_at']
        read_only_fields = ['id', 'created_at']
