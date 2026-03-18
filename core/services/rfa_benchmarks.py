"""RFA (Road Freight Association) benchmark service for vehicle cost profiles."""

from decimal import Decimal
from typing import Optional
from core.models import VehicleCostProfile, Company


class RFABenchmarkService:
    """
    Service for managing RFA VCI (Vehicle Cost Index) 2026 baseline data.

    Provides methods to retrieve cost profiles for different truck types,
    with support for company-specific overrides.
    """

    RFA_BASELINES_2026 = {
        'semi_34t': {
            'fuel_cpk': Decimal('6.33'),
            'tyre_cpk': Decimal('0.95'),
            'maintenance_cpk': Decimal('0.78'),
            'driver_cost_per_day': Decimal('640.00'),
        },
        'rigid_8t': {
            'fuel_cpk': Decimal('3.60'),
            'tyre_cpk': Decimal('0.45'),
            'maintenance_cpk': Decimal('0.42'),
            'driver_cost_per_day': Decimal('520.00'),
        },
        'flatbed': {
            'fuel_cpk': Decimal('6.10'),
            'tyre_cpk': Decimal('0.88'),
            'maintenance_cpk': Decimal('0.72'),
            'driver_cost_per_day': Decimal('640.00'),
        },
        'tipper': {
            'fuel_cpk': Decimal('5.90'),
            'tyre_cpk': Decimal('0.92'),
            'maintenance_cpk': Decimal('0.85'),
            'driver_cost_per_day': Decimal('640.00'),
        },
        'reefer': {
            'fuel_cpk': Decimal('7.20'),
            'tyre_cpk': Decimal('1.05'),
            'maintenance_cpk': Decimal('1.20'),
            'driver_cost_per_day': Decimal('680.00'),
        },
        'tanker': {
            'fuel_cpk': Decimal('6.50'),
            'tyre_cpk': Decimal('0.98'),
            'maintenance_cpk': Decimal('0.95'),
            'driver_cost_per_day': Decimal('650.00'),
        },
    }

    @staticmethod
    def get_profile(truck_type: str, company: Optional[Company] = None) -> Optional[VehicleCostProfile]:
        """
        Get vehicle cost profile for a truck type.

        Prioritizes company-specific overrides over RFA baselines.

        Args:
            truck_type: Truck type identifier (semi_34t, rigid_8t, flatbed, tipper, reefer, tanker)
            company: Optional Company instance for company-specific profile

        Returns:
            VehicleCostProfile or None: Cost profile if found, None otherwise
        """
        if company:
            company_profile = VehicleCostProfile.objects.filter(
                truck_type=truck_type,
                company=company,
                is_rfa_baseline=False
            ).first()

            if company_profile:
                return company_profile

        rfa_profile = VehicleCostProfile.objects.filter(
            truck_type=truck_type,
            is_rfa_baseline=True,
            company__isnull=True
        ).first()

        return rfa_profile

    @staticmethod
    def seed_rfa_baselines() -> int:
        """
        Seed RFA VCI 2026 baseline data into the database.

        Creates or updates RFA baseline cost profiles for all truck types.

        Returns:
            int: Number of profiles created or updated
        """
        count = 0

        for truck_type, costs in RFABenchmarkService.RFA_BASELINES_2026.items():
            profile, created = VehicleCostProfile.objects.update_or_create(
                truck_type=truck_type,
                is_rfa_baseline=True,
                company=None,
                defaults={
                    'fuel_cpk': costs['fuel_cpk'],
                    'tyre_cpk': costs['tyre_cpk'],
                    'maintenance_cpk': costs['maintenance_cpk'],
                    'driver_cost_per_day': costs['driver_cost_per_day'],
                }
            )
            count += 1

        return count

    @staticmethod
    def create_company_override(
        truck_type: str,
        company: Company,
        fuel_cpk: Decimal,
        tyre_cpk: Decimal,
        maintenance_cpk: Decimal,
        driver_cost_per_day: Decimal
    ) -> VehicleCostProfile:
        """
        Create or update a company-specific cost profile override.

        Args:
            truck_type: Truck type identifier
            company: Company instance
            fuel_cpk: Fuel cost per km
            tyre_cpk: Tyre wear cost per km
            maintenance_cpk: Maintenance cost per km
            driver_cost_per_day: Driver daily cost

        Returns:
            VehicleCostProfile: The created or updated profile
        """
        profile, created = VehicleCostProfile.objects.update_or_create(
            truck_type=truck_type,
            company=company,
            defaults={
                'fuel_cpk': fuel_cpk,
                'tyre_cpk': tyre_cpk,
                'maintenance_cpk': maintenance_cpk,
                'driver_cost_per_day': driver_cost_per_day,
                'is_rfa_baseline': False,
            }
        )

        return profile

    @staticmethod
    def get_all_rfa_baselines() -> list:
        """
        Get all RFA baseline cost profiles.

        Returns:
            list: QuerySet of all RFA baseline profiles
        """
        return list(VehicleCostProfile.objects.filter(
            is_rfa_baseline=True,
            company__isnull=True
        ).order_by('truck_type'))
