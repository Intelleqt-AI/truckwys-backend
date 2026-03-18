"""
Vehicle cost-per-km service — RFA VCI benchmark defaults and company overrides.

Usage::

    from core.services.vehicle_cost import get_cost_profile, calculate_total_cpk

    profile = get_cost_profile('interlink')
    total   = calculate_total_cpk('interlink', company=some_company)

Data sourced from the Road Freight Association (RFA) Vehicle Cost Index (VCI).
Figures below are representative 2024 benchmarks for South African operations.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from core.models import VehicleCostProfile
from core.models.company import Company

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# RFA VCI 2024 benchmark constants (ZAR)
#
# Format: truck_type -> (fuel_cpk, tyre_cpk, maintenance_cpk, driver_cost_per_day)
#
# Sources: RFA Vehicle Cost Index quarterly bulletin, cross-referenced with
# FleetWatch cost tables and SAPIA fuel pricing (inland diesel ~R21/litre).
# ──────────────────────────────────────────────────────────────────────────────

RFA_VCI_BENCHMARKS: dict[str, dict[str, str]] = {
    'rigid_8t': {
        'fuel_cpk':           '3.1500',   # ~15 l/100km @ R21/l
        'tyre_cpk':           '0.2800',
        'maintenance_cpk':    '0.6500',
        'driver_cost_per_day': '850.00',
    },
    'rigid_16t': {
        'fuel_cpk':           '4.6200',   # ~22 l/100km
        'tyre_cpk':           '0.4200',
        'maintenance_cpk':    '0.9800',
        'driver_cost_per_day': '950.00',
    },
    'horse_trailer': {
        'fuel_cpk':           '6.5100',   # ~31 l/100km
        'tyre_cpk':           '0.7500',
        'maintenance_cpk':    '1.3200',
        'driver_cost_per_day': '1150.00',
    },
    'interlink': {
        'fuel_cpk':           '7.3500',   # ~35 l/100km
        'tyre_cpk':           '0.9800',
        'maintenance_cpk':    '1.5600',
        'driver_cost_per_day': '1250.00',
    },
    'abnormal': {
        'fuel_cpk':           '9.4500',   # ~45 l/100km (heavy)
        'tyre_cpk':           '1.4500',
        'maintenance_cpk':    '2.1000',
        'driver_cost_per_day': '1500.00',
    },
}

RFA_SOURCE_LABEL: str = 'RFA VCI 2024'
RFA_EFFECTIVE_DATE: date = date(2024, 1, 1)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def get_cost_profile(
    truck_type: str,
    company: Optional[Company] = None,
) -> VehicleCostProfile:
    """
    Return the most-recent :class:`VehicleCostProfile` for *truck_type*.

    Resolution order:
    1. Company-specific override (if *company* given and override exists).
    2. RFA industry default (``company__isnull=True``).

    Raises ``VehicleCostProfile.DoesNotExist`` when no profile is found.
    """
    if company is not None:
        try:
            return (
                VehicleCostProfile.objects
                .filter(truck_type=truck_type, company=company)
                .order_by('-effective_date')
                .first()
            ) or _get_rfa_default(truck_type)
        except Exception:
            logger.debug(
                'No company override for %s / %s — falling back to RFA default',
                truck_type, company,
            )
    return _get_rfa_default(truck_type)


def calculate_total_cpk(
    truck_type: str,
    company: Optional[Company] = None,
) -> Decimal:
    """
    Sum of fuel + tyre + maintenance CPK for the resolved profile.

    Driver cost is **not** included — it is a daily rate, not per-km.
    """
    profile = get_cost_profile(truck_type, company)
    return profile.fuel_cpk + profile.tyre_cpk + profile.maintenance_cpk


def seed_rfa_defaults() -> list[VehicleCostProfile]:
    """
    Idempotently seed RFA VCI benchmark records (one per truck type).

    Returns the list of created (or existing) profiles.
    """
    created: list[VehicleCostProfile] = []
    for truck_type, values in RFA_VCI_BENCHMARKS.items():
        obj, was_created = VehicleCostProfile.objects.get_or_create(
            truck_type=truck_type,
            company=None,
            source=RFA_SOURCE_LABEL,
            defaults={
                'fuel_cpk': Decimal(values['fuel_cpk']),
                'tyre_cpk': Decimal(values['tyre_cpk']),
                'maintenance_cpk': Decimal(values['maintenance_cpk']),
                'driver_cost_per_day': Decimal(values['driver_cost_per_day']),
                'is_custom': False,
                'effective_date': RFA_EFFECTIVE_DATE,
            },
        )
        created.append(obj)
        if was_created:
            logger.info('Seeded RFA default for %s', truck_type)
    return created


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _get_rfa_default(truck_type: str) -> VehicleCostProfile:
    """Fetch the latest RFA default for *truck_type*."""
    profile = (
        VehicleCostProfile.objects
        .filter(truck_type=truck_type, company__isnull=True)
        .order_by('-effective_date')
        .first()
    )
    if profile is None:
        raise VehicleCostProfile.DoesNotExist(
            f'No RFA default cost profile found for truck_type={truck_type!r}. '
            'Run the seed migration or `seed_rfa_defaults()` first.'
        )
    return profile
