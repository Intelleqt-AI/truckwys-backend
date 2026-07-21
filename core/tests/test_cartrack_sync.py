"""Tests for the Cartrack vehicle-status/door-event polling services and their consumer view."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import ActivityEvent, Company, Vehicle
from core.services.cartrack_sync import poll_door_events, poll_vehicle_status

User = get_user_model()


def _make_company(name):
    return Company.objects.create(
        company_name=name,
        cartrack_username='demo',
        cartrack_password='demo',
        cartrack_base_url='https://fleetapi-za.cartrack.com',
    )


def _make_vehicle(company, plate, **extra):
    return Vehicle.objects.create(
        company=company,
        vin=f'VIN-{company.id}-{plate}',
        make='Mercedes',
        model='Actros',
        year=2020,
        plate=plate,
        type='Truck',
        capacity=Decimal('20000.00'),
        fuel_type='Diesel',
        status='AVAILABLE',
        **extra,
    )


class PollVehicleStatusTests(TestCase):
    def setUp(self):
        self.company = _make_company('Test Fleet Co')
        self.other_company = _make_company('Other Fleet Co')

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_matches_by_plate_and_updates_location_fields(self, mock_client_cls):
        vehicle = _make_vehicle(self.company, 'CA123GP')
        mock_client_cls.for_company.return_value.get_vehicle_status.return_value = [
            {
                'registration': 'CA123GP',
                'latitude': -33.918861,
                'longitude': 18.423300,
                'heading': 90.5,
                'speed': 62.3,
                'ignition': True,
            }
        ]

        result = poll_vehicle_status(self.company)

        vehicle.refresh_from_db()
        self.assertEqual(result, {'matched': 1, 'unmatched': []})
        self.assertEqual(vehicle.latitude, Decimal('-33.918861'))
        self.assertEqual(vehicle.longitude, Decimal('18.423300'))
        self.assertTrue(vehicle.ignition_on)
        self.assertIsNotNone(vehicle.last_location_at)

        self.company.refresh_from_db()
        self.assertIsNotNone(self.company.cartrack_last_status_sync)

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_cartrack_registration_override_takes_priority_over_plate(self, mock_client_cls):
        vehicle = _make_vehicle(self.company, 'CA123GP', cartrack_registration='CA 123 GP')
        mock_client_cls.for_company.return_value.get_vehicle_status.return_value = [
            {'registration': 'CA 123 GP', 'latitude': -26.2, 'longitude': 28.0},
        ]

        result = poll_vehicle_status(self.company)

        vehicle.refresh_from_db()
        self.assertEqual(result['matched'], 1)
        self.assertEqual(vehicle.latitude, Decimal('-26.2'))

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_unmatched_registration_does_not_raise_and_is_reported(self, mock_client_cls):
        _make_vehicle(self.company, 'CA123GP')
        mock_client_cls.for_company.return_value.get_vehicle_status.return_value = [
            {'registration': 'UNKNOWN999', 'latitude': -26.2, 'longitude': 28.0},
        ]

        result = poll_vehicle_status(self.company)

        self.assertEqual(result, {'matched': 0, 'unmatched': ['UNKNOWN999']})

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_only_updates_vehicles_belonging_to_the_polled_company(self, mock_client_cls):
        own_vehicle = _make_vehicle(self.company, 'SHARED123')
        other_vehicle = _make_vehicle(self.other_company, 'SHARED123')
        mock_client_cls.for_company.return_value.get_vehicle_status.return_value = [
            {'registration': 'SHARED123', 'latitude': -26.2, 'longitude': 28.0},
        ]

        poll_vehicle_status(self.company)

        own_vehicle.refresh_from_db()
        other_vehicle.refresh_from_db()
        self.assertIsNotNone(own_vehicle.last_location_at)
        self.assertIsNone(other_vehicle.last_location_at)

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_updates_temperature_fields(self, mock_client_cls):
        vehicle = _make_vehicle(self.company, 'CA123GP')
        mock_client_cls.for_company.return_value.get_vehicle_status.return_value = [
            {'registration': 'CA123GP', 'temp1': 4.5, 'temp2': -18.0, 'temp3': None, 'temp4': None},
        ]

        poll_vehicle_status(self.company)

        vehicle.refresh_from_db()
        self.assertEqual(vehicle.temp1, Decimal('4.5'))
        self.assertEqual(vehicle.temp2, Decimal('-18.0'))
        self.assertIsNone(vehicle.temp3)

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_sets_driver_ref_without_touching_driver_fk(self, mock_client_cls):
        vehicle = _make_vehicle(self.company, 'CA123GP')
        self.assertIsNone(vehicle.driver_id)
        mock_client_cls.for_company.return_value.get_vehicle_status.return_value = [
            {'registration': 'CA123GP', 'driver_id': 'TAG-9981'},
        ]

        poll_vehicle_status(self.company)

        vehicle.refresh_from_db()
        self.assertEqual(vehicle.cartrack_current_driver_ref, 'TAG-9981')
        self.assertIsNone(vehicle.driver_id)


class PollDoorEventsTests(TestCase):
    def setUp(self):
        self.company = _make_company('Door Test Co')
        self.other_company = _make_company('Other Door Co')

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_applies_door_open_event_and_creates_activity_event(self, mock_client_cls):
        vehicle = _make_vehicle(self.company, 'CA123GP')
        mock_client_cls.for_company.return_value.get_door_events.return_value = [
            {'registration': 'CA123GP', 'event_ts': '2026-07-15T10:00:00Z', 'is_open': True},
        ]

        result = poll_door_events(self.company)

        vehicle.refresh_from_db()
        self.assertEqual(result, {'applied': 1})
        self.assertTrue(vehicle.door_open)
        self.assertIsNotNone(vehicle.last_door_event_at)
        self.assertEqual(
            ActivityEvent.objects.filter(entity_type='vehicle', entity_id=vehicle.id).count(), 1
        )

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_derives_open_state_from_event_type_when_is_open_absent(self, mock_client_cls):
        vehicle = _make_vehicle(self.company, 'CA123GP')
        mock_client_cls.for_company.return_value.get_door_events.return_value = [
            {'registration': 'CA123GP', 'event_ts': '2026-07-15T10:00:00Z', 'event_type': 'DOOR_CLOSE'},
        ]

        poll_door_events(self.company)

        vehicle.refresh_from_db()
        self.assertFalse(vehicle.door_open)

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_skips_event_not_newer_than_last_applied(self, mock_client_cls):
        already_applied_at = timezone.now()
        vehicle = _make_vehicle(self.company, 'CA123GP', door_open=False, last_door_event_at=already_applied_at)
        older_event_ts = (already_applied_at - timedelta(minutes=10)).isoformat()
        mock_client_cls.for_company.return_value.get_door_events.return_value = [
            {'registration': 'CA123GP', 'event_ts': older_event_ts, 'is_open': True},
        ]

        result = poll_door_events(self.company)

        vehicle.refresh_from_db()
        self.assertEqual(result, {'applied': 0})
        self.assertFalse(vehicle.door_open)  # unchanged — stale event was skipped
        self.assertEqual(ActivityEvent.objects.filter(entity_type='vehicle', entity_id=vehicle.id).count(), 0)

    @patch('core.services.cartrack_sync.CartrackClient')
    def test_only_applies_events_for_the_polled_companys_vehicles(self, mock_client_cls):
        own_vehicle = _make_vehicle(self.company, 'SHARED123')
        other_vehicle = _make_vehicle(self.other_company, 'SHARED123')
        mock_client_cls.for_company.return_value.get_door_events.return_value = [
            {'registration': 'SHARED123', 'event_ts': '2026-07-15T10:00:00Z', 'is_open': True},
        ]

        poll_door_events(self.company)

        own_vehicle.refresh_from_db()
        other_vehicle.refresh_from_db()
        self.assertIsNotNone(own_vehicle.last_door_event_at)
        self.assertIsNone(other_vehicle.last_door_event_at)


class FleetVehicleStatusAPIViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = _make_company('View Co')
        self.other_company = _make_company('Other View Co')

        self.user = User.objects.create_user(
            username='viewtester', email='view@example.com', password='testpass123',
        )
        self.user.company = self.company
        self.user.save()
        self.client.force_authenticate(user=self.user)

        self.my_vehicle = _make_vehicle(self.company, 'MINE123')
        self.other_vehicle = _make_vehicle(self.other_company, 'THEIRS123')

    def test_only_returns_own_companys_vehicles(self):
        response = self.client.get(reverse('fleet-vehicle-status'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plates = [v['plate'] for v in response.data['vehicles']]
        self.assertIn('MINE123', plates)
        self.assertNotIn('THEIRS123', plates)

    def test_current_location_reflects_recent_poll(self):
        self.my_vehicle.latitude = Decimal('-26.2')
        self.my_vehicle.longitude = Decimal('28.0')
        self.my_vehicle.last_location_at = timezone.now()
        self.my_vehicle.save()

        response = self.client.get(reverse('fleet-vehicle-status'))
        entry = next(v for v in response.data['vehicles'] if v['plate'] == 'MINE123')
        self.assertIsNotNone(entry['current_location'])
        self.assertFalse(entry['current_location']['is_stale'])

    def test_current_location_is_none_when_never_polled(self):
        response = self.client.get(reverse('fleet-vehicle-status'))
        entry = next(v for v in response.data['vehicles'] if v['plate'] == 'MINE123')
        self.assertIsNone(entry['current_location'])
