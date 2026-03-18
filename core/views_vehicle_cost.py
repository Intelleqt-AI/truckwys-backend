"""
API views for VehicleCostProfile CRUD.

Endpoints (all prefixed with ``/api/``):

- ``GET  vehicle-costs/``              — list profiles (RFA defaults + company overrides)
- ``POST vehicle-costs/``              — create a company override
- ``GET  vehicle-costs/<id>/``         — retrieve a single profile
- ``PUT  vehicle-costs/<id>/``         — update a company override
- ``DELETE vehicle-costs/<id>/``       — delete a company override
- ``GET  vehicle-costs/defaults/``     — list RFA defaults only
- ``GET  vehicle-costs/total-cpk/``    — get total CPK for a truck type
"""

from decimal import Decimal
from typing import Any

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from .models import VehicleCostProfile
from .models.vehicle_cost_profile import TRUCK_TYPE_CHOICES
from .serializers_vehicle_cost import (
    VehicleCostProfileSerializer,
    VehicleCostProfileCreateSerializer,
)
from .services.vehicle_cost import calculate_total_cpk, get_cost_profile


class VehicleCostProfileViewSet(viewsets.ModelViewSet):
    """
    CRUD for Vehicle Cost Profiles.

    Authenticated users see:
    - All RFA defaults (``company=null``)
    - Their own company overrides

    Superusers see everything.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = VehicleCostProfileSerializer

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return VehicleCostProfile.objects.none()

        qs = VehicleCostProfile.objects.select_related('company')

        if user.is_superuser:
            return qs

        # Show RFA defaults + own company overrides
        company = getattr(user, 'company', None)
        if company is not None:
            return qs.filter(
                models_q_company_null_or_own(company),
            )
        return qs.filter(company__isnull=True)

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return VehicleCostProfileCreateSerializer
        return VehicleCostProfileSerializer

    def perform_create(self, serializer: VehicleCostProfileCreateSerializer) -> None:
        """Auto-set company and mark as custom override."""
        serializer.save(
            company=self.request.user.company,
            is_custom=True,
            source=serializer.validated_data.get('source', 'Company Override'),
        )

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Prevent deletion of RFA default profiles."""
        instance = self.get_object()
        if not instance.is_custom:
            return Response(
                {'detail': 'Cannot delete RFA default profiles.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Prevent modification of RFA default profiles."""
        instance = self.get_object()
        if not instance.is_custom:
            return Response(
                {'detail': 'Cannot modify RFA default profiles. Create a company override instead.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Prevent modification of RFA default profiles."""
        instance = self.get_object()
        if not instance.is_custom:
            return Response(
                {'detail': 'Cannot modify RFA default profiles. Create a company override instead.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().partial_update(request, *args, **kwargs)

    # ── Custom actions ────────────────────────────────────────────────────

    @action(detail=False, methods=['get'], url_path='defaults')
    def defaults(self, request: Request) -> Response:
        """List all RFA default cost profiles."""
        qs = VehicleCostProfile.objects.filter(
            company__isnull=True,
        ).order_by('truck_type')
        serializer = VehicleCostProfileSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='total-cpk')
    def total_cpk(self, request: Request) -> Response:
        """
        Get total CPK for a truck type.

        Query params:
        - ``truck_type`` (required) — e.g. ``interlink``
        """
        truck_type = request.query_params.get('truck_type')
        if not truck_type:
            return Response(
                {'detail': 'Query parameter "truck_type" is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_types = [choice[0] for choice in TRUCK_TYPE_CHOICES]
        if truck_type not in valid_types:
            return Response(
                {'detail': f'Invalid truck type. Must be one of: {", ".join(valid_types)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company = getattr(request.user, 'company', None)
        try:
            total = calculate_total_cpk(truck_type, company)
            profile = get_cost_profile(truck_type, company)
        except VehicleCostProfile.DoesNotExist:
            return Response(
                {'detail': f'No cost profile found for truck_type={truck_type!r}.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({
            'truck_type': truck_type,
            'total_cpk': str(total),
            'fuel_cpk': str(profile.fuel_cpk),
            'tyre_cpk': str(profile.tyre_cpk),
            'maintenance_cpk': str(profile.maintenance_cpk),
            'driver_cost_per_day': str(profile.driver_cost_per_day),
            'source': profile.source,
            'is_custom': profile.is_custom,
        })


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

from django.db.models import Q


def models_q_company_null_or_own(company) -> Q:
    """Q filter: RFA defaults OR profiles belonging to *company*."""
    return Q(company__isnull=True) | Q(company=company)
