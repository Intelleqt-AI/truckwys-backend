"""Minimal data that makes a quote priceable and sendable under
QUOTE-RULES.md (an official diesel price in force, a vehicle type, a route
distance and known tolls), for tests whose subject is something else."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone


def official_price_now(inland='32.7989', coastal='31.9269'):
    from core.models import FuelPrice
    now = timezone.now()
    return FuelPrice.objects.create(date=timezone.localdate(now), diesel_inland=Decimal(inland),
                                    diesel_coastal=Decimal(coastal), source='FIASA', diesel_grade='50ppm',
                                    effective_from=now - timedelta(hours=1))


def sendable_quote_fields(company, name='Test Tautliner'):
    """Quote fields (API payload style) for a sendable one-way quote."""
    from core.models import VehicleType
    VehicleType.objects.get_or_create(company=company, name=name, defaults={
        'capacity': 30, 'max_distance': 3000, 'base_rate': 20, 'fuel_consumption_l_per_100km': 38})
    return {'distance': '120', 'vehicle_type': name, 'toll_charges': '150.00', 'estimated_duration_minutes': 100}
