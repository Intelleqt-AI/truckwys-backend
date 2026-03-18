from decimal import Decimal
from typing import Any

from rest_framework import serializers

from .models import VehicleCostProfile
from .models.vehicle_cost_profile import TRUCK_TYPE_CHOICES


class VehicleCostProfileSerializer(serializers.ModelSerializer):
    """Read/write serializer for VehicleCostProfile."""

    total_cpk = serializers.SerializerMethodField()
    truck_type_display = serializers.CharField(
        source='get_truck_type_display', read_only=True,
    )

    class Meta:
        model = VehicleCostProfile
        fields = [
            'id',
            'truck_type',
            'truck_type_display',
            'fuel_cpk',
            'tyre_cpk',
            'maintenance_cpk',
            'driver_cost_per_day',
            'is_custom',
            'company',
            'source',
            'effective_date',
            'total_cpk',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'total_cpk', 'truck_type_display']

    def get_total_cpk(self, obj: VehicleCostProfile) -> str:
        """Sum of fuel + tyre + maintenance CPK."""
        total: Decimal = obj.fuel_cpk + obj.tyre_cpk + obj.maintenance_cpk
        return str(total)

    def validate_truck_type(self, value: str) -> str:
        valid_types = [choice[0] for choice in TRUCK_TYPE_CHOICES]
        if value not in valid_types:
            raise serializers.ValidationError(
                f'Invalid truck type. Must be one of: {", ".join(valid_types)}'
            )
        return value


class VehicleCostProfileCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating company-specific cost profile overrides.

    The *company* field is set automatically from the authenticated user.
    """

    class Meta:
        model = VehicleCostProfile
        fields = [
            'truck_type',
            'fuel_cpk',
            'tyre_cpk',
            'maintenance_cpk',
            'driver_cost_per_day',
            'source',
            'effective_date',
        ]

    def validate_truck_type(self, value: str) -> str:
        valid_types = [choice[0] for choice in TRUCK_TYPE_CHOICES]
        if value not in valid_types:
            raise serializers.ValidationError(
                f'Invalid truck type. Must be one of: {", ".join(valid_types)}'
            )
        return value
