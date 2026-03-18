"""Tests for T1.3 — VehicleCostProfile model, vehicle_cost service, and API."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Company, VehicleCostProfile
from core.models.vehicle_cost_profile import TRUCK_TYPE_CHOICES
from core.services.vehicle_cost import (
    RFA_EFFECTIVE_DATE,
    RFA_SOURCE_LABEL,
    RFA_VCI_BENCHMARKS,
    calculate_total_cpk,
    get_cost_profile,
    seed_rfa_defaults,
)

User = get_user_model()


# ──────────────────────────────────────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────────────────────────────────────

def _make_rfa_profile(truck_type: str) -> VehicleCostProfile:
    """Create an RFA default profile from benchmark constants."""
    values = RFA_VCI_BENCHMARKS[truck_type]
    return VehicleCostProfile.objects.create(
        truck_type=truck_type,
        fuel_cpk=Decimal(values['fuel_cpk']),
        tyre_cpk=Decimal(values['tyre_cpk']),
        maintenance_cpk=Decimal(values['maintenance_cpk']),
        driver_cost_per_day=Decimal(values['driver_cost_per_day']),
        is_custom=False,
        company=None,
        source=RFA_SOURCE_LABEL,
        effective_date=RFA_EFFECTIVE_DATE,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Model tests
# ──────────────────────────────────────────────────────────────────────────────

class VehicleCostProfileModelTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_rfa_profile('interlink')

    def test_str_representation(self) -> None:
        text = str(self.profile)
        self.assertIn('Interlink', text)
        self.assertIn('RFA Default', text)

    def test_default_ordering_by_effective_date_desc(self) -> None:
        older = VehicleCostProfile.objects.create(
            truck_type='interlink',
            fuel_cpk=Decimal('6.0000'),
            tyre_cpk=Decimal('0.8000'),
            maintenance_cpk=Decimal('1.2000'),
            driver_cost_per_day=Decimal('1100.00'),
            is_custom=False,
            company=None,
            source='RFA VCI 2023',
            effective_date=date(2023, 1, 1),
        )
        profiles = list(VehicleCostProfile.objects.filter(truck_type='interlink'))
        self.assertEqual(profiles[0].pk, self.profile.pk)
        self.assertEqual(profiles[1].pk, older.pk)

    def test_company_fk_nullable(self) -> None:
        self.assertIsNone(self.profile.company)

    def test_company_override_str(self) -> None:
        company = Company.objects.create(company_name='Test Co')
        override = VehicleCostProfile.objects.create(
            truck_type='rigid_8t',
            fuel_cpk=Decimal('3.5000'),
            tyre_cpk=Decimal('0.3000'),
            maintenance_cpk=Decimal('0.7000'),
            driver_cost_per_day=Decimal('900.00'),
            is_custom=True,
            company=company,
            source='Company Override',
            effective_date=date(2024, 6, 1),
        )
        text = str(override)
        self.assertIn('Test Co', text)
        self.assertIn('Company Override', text)


# ──────────────────────────────────────────────────────────────────────────────
# Service tests
# ──────────────────────────────────────────────────────────────────────────────

class VehicleCostServiceTests(TestCase):
    def setUp(self) -> None:
        seed_rfa_defaults()
        self.company = Company.objects.create(company_name='Service Test Co')

    def test_get_cost_profile_returns_rfa_default(self) -> None:
        profile = get_cost_profile('interlink')
        self.assertFalse(profile.is_custom)
        self.assertIsNone(profile.company)
        self.assertEqual(profile.fuel_cpk, Decimal('7.3500'))

    def test_get_cost_profile_returns_company_override(self) -> None:
        VehicleCostProfile.objects.create(
            truck_type='interlink',
            fuel_cpk=Decimal('8.0000'),
            tyre_cpk=Decimal('1.1000'),
            maintenance_cpk=Decimal('1.7000'),
            driver_cost_per_day=Decimal('1300.00'),
            is_custom=True,
            company=self.company,
            source='Company Override',
            effective_date=date(2024, 6, 1),
        )
        profile = get_cost_profile('interlink', company=self.company)
        self.assertTrue(profile.is_custom)
        self.assertEqual(profile.fuel_cpk, Decimal('8.0000'))

    def test_get_cost_profile_falls_back_to_rfa(self) -> None:
        """No company override → RFA default."""
        profile = get_cost_profile('rigid_8t', company=self.company)
        self.assertFalse(profile.is_custom)

    def test_get_cost_profile_raises_for_unknown_type(self) -> None:
        with self.assertRaises(VehicleCostProfile.DoesNotExist):
            get_cost_profile('nonexistent_type')

    def test_calculate_total_cpk(self) -> None:
        total = calculate_total_cpk('interlink')
        expected = Decimal('7.3500') + Decimal('0.9800') + Decimal('1.5600')
        self.assertEqual(total, expected)

    def test_calculate_total_cpk_all_types(self) -> None:
        """Ensure every truck type has a valid total CPK."""
        for truck_type, _ in TRUCK_TYPE_CHOICES:
            total = calculate_total_cpk(truck_type)
            self.assertGreater(total, Decimal('0'))

    def test_seed_rfa_defaults_is_idempotent(self) -> None:
        initial_count = VehicleCostProfile.objects.count()
        seed_rfa_defaults()  # call again
        self.assertEqual(VehicleCostProfile.objects.count(), initial_count)

    def test_seed_creates_all_truck_types(self) -> None:
        for truck_type, _ in TRUCK_TYPE_CHOICES:
            self.assertTrue(
                VehicleCostProfile.objects.filter(
                    truck_type=truck_type, company__isnull=True,
                ).exists(),
                f'Missing RFA default for {truck_type}',
            )


# ──────────────────────────────────────────────────────────────────────────────
# API integration tests
# ──────────────────────────────────────────────────────────────────────────────

class VehicleCostAPITests(TestCase):
    def setUp(self) -> None:
        seed_rfa_defaults()
        self.client = APIClient()
        self.company = Company.objects.create(company_name='API Test Co')
        self.user = User.objects.create_user(
            username='apiuser',
            email='api@test.com',
            password='testpass123',
            first_name='API',
            last_name='User',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

    # ── List ──────────────────────────────────────────────────────────────

    def test_list_profiles(self) -> None:
        url = reverse('vehiclecostprofile-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should see at least the 5 RFA defaults
        self.assertGreaterEqual(len(response.data), len(TRUCK_TYPE_CHOICES))

    # ── Defaults action ───────────────────────────────────────────────────

    def test_defaults_action(self) -> None:
        url = reverse('vehiclecostprofile-defaults')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(TRUCK_TYPE_CHOICES))
        for item in response.data:
            self.assertFalse(item['is_custom'])

    # ── Total CPK action ──────────────────────────────────────────────────

    def test_total_cpk_action(self) -> None:
        url = reverse('vehiclecostprofile-total-cpk')
        response = self.client.get(url, {'truck_type': 'interlink'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('total_cpk', response.data)
        expected = Decimal('7.3500') + Decimal('0.9800') + Decimal('1.5600')
        self.assertEqual(Decimal(response.data['total_cpk']), expected)

    def test_total_cpk_missing_param(self) -> None:
        url = reverse('vehiclecostprofile-total-cpk')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_total_cpk_invalid_type(self) -> None:
        url = reverse('vehiclecostprofile-total-cpk')
        response = self.client.get(url, {'truck_type': 'invalid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ── Create (company override) ─────────────────────────────────────────

    def test_create_company_override(self) -> None:
        url = reverse('vehiclecostprofile-list')
        data = {
            'truck_type': 'rigid_8t',
            'fuel_cpk': '3.5000',
            'tyre_cpk': '0.3500',
            'maintenance_cpk': '0.7500',
            'driver_cost_per_day': '900.00',
            'source': 'Company Override',
            'effective_date': '2024-06-01',
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        profile = VehicleCostProfile.objects.get(pk=response.data['id'])
        self.assertTrue(profile.is_custom)
        self.assertEqual(profile.company, self.company)

    # ── Update / Delete guards ────────────────────────────────────────────

    def test_cannot_update_rfa_default(self) -> None:
        rfa = VehicleCostProfile.objects.filter(company__isnull=True).first()
        url = reverse('vehiclecostprofile-detail', args=[rfa.pk])
        response = self.client.put(url, {
            'truck_type': rfa.truck_type,
            'fuel_cpk': '99.0000',
            'tyre_cpk': '99.0000',
            'maintenance_cpk': '99.0000',
            'driver_cost_per_day': '9999.00',
            'source': 'Hacked',
            'effective_date': '2024-01-01',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_delete_rfa_default(self) -> None:
        rfa = VehicleCostProfile.objects.filter(company__isnull=True).first()
        url = reverse('vehiclecostprofile-detail', args=[rfa.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_can_update_company_override(self) -> None:
        override = VehicleCostProfile.objects.create(
            truck_type='rigid_8t',
            fuel_cpk=Decimal('3.5000'),
            tyre_cpk=Decimal('0.3500'),
            maintenance_cpk=Decimal('0.7500'),
            driver_cost_per_day=Decimal('900.00'),
            is_custom=True,
            company=self.company,
            source='Company Override',
            effective_date=date(2024, 6, 1),
        )
        url = reverse('vehiclecostprofile-detail', args=[override.pk])
        response = self.client.patch(url, {'fuel_cpk': '4.0000'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_can_delete_company_override(self) -> None:
        override = VehicleCostProfile.objects.create(
            truck_type='rigid_8t',
            fuel_cpk=Decimal('3.5000'),
            tyre_cpk=Decimal('0.3500'),
            maintenance_cpk=Decimal('0.7500'),
            driver_cost_per_day=Decimal('900.00'),
            is_custom=True,
            company=self.company,
            source='Company Override',
            effective_date=date(2024, 6, 1),
        )
        url = reverse('vehiclecostprofile-detail', args=[override.pk])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    # ── Auth required ─────────────────────────────────────────────────────

    def test_unauthenticated_returns_401(self) -> None:
        self.client.force_authenticate(user=None)
        url = reverse('vehiclecostprofile-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # ── Response includes total_cpk field ─────────────────────────────────

    def test_list_response_includes_total_cpk(self) -> None:
        url = reverse('vehiclecostprofile-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for item in response.data:
            self.assertIn('total_cpk', item)
            total = Decimal(item['total_cpk'])
            self.assertGreater(total, Decimal('0'))
