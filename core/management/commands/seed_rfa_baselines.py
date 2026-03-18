"""Management command to seed RFA VCI 2026 baseline cost profiles."""

from django.core.management.base import BaseCommand

from core.services.rfa_benchmarks import RFABenchmarkService
from core.models import VehicleCostProfile


class Command(BaseCommand):
    """
    Seeds RFA (Road Freight Association) VCI 2026 baseline cost profiles.

    Includes cost-per-km data for:
    - Semi-trailer 34 ton
    - Rigid 8 ton
    - Flatbed
    - Tipper
    - Refrigerated (reefer)
    - Tanker

    Usage:
        python manage.py seed_rfa_baselines
    """

    help = 'Seeds RFA VCI 2026 baseline vehicle cost profiles'

    def handle(self, *args, **options):
        """Execute the command to seed RFA baseline cost profiles."""
        self.stdout.write(self.style.NOTICE('Seeding RFA VCI 2026 baseline cost profiles...'))

        count = RFABenchmarkService.seed_rfa_baselines()

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully seeded {count} RFA baseline cost profiles'
            )
        )

        profiles = VehicleCostProfile.objects.filter(
            is_rfa_baseline=True,
            company__isnull=True
        ).order_by('truck_type')

        for profile in profiles:
            self.stdout.write(
                f"  {profile.get_truck_type_display()}:"
            )
            self.stdout.write(f"    Fuel CPK: R{profile.fuel_cpk}")
            self.stdout.write(f"    Tyre CPK: R{profile.tyre_cpk}")
            self.stdout.write(f"    Maintenance CPK: R{profile.maintenance_cpk}")
            self.stdout.write(f"    Driver cost/day: R{profile.driver_cost_per_day}")
            self.stdout.write(f"    Total CPK: R{profile.total_cpk()}")
            self.stdout.write("")
