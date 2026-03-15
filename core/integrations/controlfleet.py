"""
ControlFleet Integration Adapter
Handles inbound webhooks from ControlFleet fleet management system.
Maps ControlFleet events to TruckWys Load/Vehicle/Driver models.
"""

from typing import Dict, Any, Optional
from datetime import datetime
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Load, Vehicle, Driver, ActivityEvent


class ControlFleetAdapter:
    """
    Adapter for ControlFleet fleet management system integration.
    Processes inbound webhook events and maps to TruckWys models.
    """

    def __init__(self):
        self.api_key = settings.CONTROLFLEET_API_KEY

    def verify_api_key(self, request) -> bool:
        """
        Verify X-ControlFleet-Key header against settings.CONTROLFLEET_WEBHOOK_KEY.

        Args:
            request: Django request object

        Returns:
            True if API key is valid, False otherwise
        """
        webhook_key = request.META.get('HTTP_X_CONTROLFLEET_KEY')
        if not webhook_key:
            return False

        expected_key = settings.CONTROLFLEET_WEBHOOK_KEY
        return webhook_key == expected_key

    def handle_trip_update(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle trip update event from ControlFleet.

        Expected payload:
        {
            "trip_id": "CF-12345",
            "load_number": "LD-2024-001",  # or load_id
            "event_type": "started|in_progress|completed|delayed",
            "status": "IN_TRANSIT|DELIVERED|DELAYED",
            "location": {"lat": -26.2041, "lng": 28.0473},
            "eta": "2024-03-15T14:30:00Z",
            "distance_covered_km": 120.5,
            "delivered_at": "2024-03-15T14:30:00Z",
            "pod": {
                "signature": "base64_signature",
                "received_by": "John Smith"
            }
        }

        Args:
            data: Webhook payload from ControlFleet

        Returns:
            Dict with status and message
        """
        load_id = data.get('load_id')
        load_number = data.get('load_number')
        event_type = data.get('event_type')

        if not event_type:
            return {'status': 'error', 'message': 'event_type is required'}

        # Find the load
        try:
            if load_id:
                load = Load.objects.get(id=load_id)
            elif load_number:
                load = Load.objects.get(load_number=load_number)
            else:
                return {'status': 'error', 'message': 'load_id or load_number is required'}
        except Load.DoesNotExist:
            return {'status': 'error', 'message': f'Load not found: {load_id or load_number}'}

        # Update load based on event type
        if event_type == 'started':
            load.status = 'IN_TRANSIT'
        elif event_type == 'completed':
            load.status = 'DELIVERED'
            # Handle proof of delivery
            if 'pod' in data:
                pod = data['pod']
                load.pod_received_by = pod.get('received_by', '')
                load.pod_signature = pod.get('signature', '')
            if 'delivered_at' in data:
                # Store in notes for now (no delivered_at field on Load)
                delivered_at = data['delivered_at']
                load.notes = f"{load.notes}\nDelivered at: {delivered_at}".strip()
        elif event_type == 'delayed':
            # Keep current status but log the delay
            pass
        elif 'status' in data:
            # Allow direct status mapping
            cf_status = data['status']
            status_map = {
                'IN_TRANSIT': 'IN_TRANSIT',
                'DELIVERED': 'DELIVERED',
                'DELAYED': 'IN_TRANSIT',  # Keep in transit but flagged
            }
            if cf_status in status_map:
                load.status = status_map[cf_status]

        # Update distance if provided
        if 'distance_covered_km' in data:
            distance = Decimal(str(data['distance_covered_km']))
            if not load.distance or distance > load.distance:
                load.distance = distance

        load.save()

        # Create activity event
        ActivityEvent.objects.create(
            event_type='load',
            title=f'ControlFleet: {event_type} for {load.load_number}',
            description=f'Trip update from ControlFleet fleet management system',
            entity_id=load.id,
            entity_type='load',
            metadata=data
        )

        return {
            'status': 'success',
            'message': f'Trip update processed for {load.load_number}',
            'load_id': load.id
        }

    def handle_vehicle_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle vehicle event from ControlFleet.

        Expected payload:
        {
            "vehicle_id": 123,  # or vin
            "vin": "ABC123XYZ",
            "event_type": "maintenance_due|breakdown|inspection|location_update",
            "status": "AVAILABLE|IN_USE|MAINTENANCE|BREAKDOWN",
            "maintenance_due": "2024-04-15",
            "next_service_km": 15000,
            "location": {"lat": -26.2041, "lng": 28.0473},
            "mileage": 125000
        }

        Args:
            data: Webhook payload from ControlFleet

        Returns:
            Dict with status and message
        """
        vehicle_id = data.get('vehicle_id')
        vin = data.get('vin')
        event_type = data.get('event_type', 'status_update')

        # Find the vehicle
        try:
            if vehicle_id:
                vehicle = Vehicle.objects.get(id=vehicle_id)
            elif vin:
                vehicle = Vehicle.objects.get(vin=vin)
            else:
                return {'status': 'error', 'message': 'vehicle_id or vin is required'}
        except Vehicle.DoesNotExist:
            return {'status': 'error', 'message': f'Vehicle not found: {vehicle_id or vin}'}

        # Update vehicle fields
        if 'status' in data:
            vehicle.status = data['status']

        if 'maintenance_due' in data:
            try:
                maintenance_date = datetime.strptime(data['maintenance_due'], '%Y-%m-%d').date()
                vehicle.next_maintenance_due = maintenance_date
            except ValueError:
                pass  # Invalid date format, skip

        if 'mileage' in data:
            vehicle.mileage = Decimal(str(data['mileage']))

        vehicle.save()

        # Create activity event
        ActivityEvent.objects.create(
            event_type='system',
            title=f'ControlFleet: {event_type} for {vehicle.plate}',
            description=f'Vehicle event from ControlFleet fleet management system',
            entity_id=vehicle.id,
            entity_type='vehicle',
            metadata=data
        )

        return {
            'status': 'success',
            'message': f'Vehicle event processed for {vehicle.plate}',
            'vehicle_id': vehicle.id
        }

    def handle_driver_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle driver event from ControlFleet.

        Expected payload:
        {
            "driver_id": 456,  # or license_number
            "license_number": "ABC123456",
            "event_type": "violation|accident|license_renewal|hours_update",
            "violation_count": 2,
            "accident_history": 1,
            "license_expiry": "2025-06-30",
            "description": "Speeding violation on N1"
        }

        Args:
            data: Webhook payload from ControlFleet

        Returns:
            Dict with status and message
        """
        driver_id = data.get('driver_id')
        license_number = data.get('license_number')
        event_type = data.get('event_type', 'status_update')

        # Find the driver
        try:
            if driver_id:
                driver = Driver.objects.get(id=driver_id)
            elif license_number:
                driver = Driver.objects.get(license_number=license_number)
            else:
                return {'status': 'error', 'message': 'driver_id or license_number is required'}
        except Driver.DoesNotExist:
            return {'status': 'error', 'message': f'Driver not found: {driver_id or license_number}'}

        # Update driver fields
        if 'violation_count' in data:
            driver.violation_count = int(data['violation_count'])

        if 'accident_history' in data:
            driver.accident_history = int(data['accident_history'])

        if 'license_expiry' in data:
            try:
                expiry_date = datetime.strptime(data['license_expiry'], '%Y-%m-%d').date()
                driver.license_expiry = expiry_date
            except ValueError:
                pass  # Invalid date format, skip

        driver.save()

        # Create activity event
        ActivityEvent.objects.create(
            event_type='system',
            title=f'ControlFleet: {event_type} for {driver.user.get_full_name()}',
            description=data.get('description', f'Driver event from ControlFleet fleet management system'),
            entity_id=driver.id,
            entity_type='driver',
            metadata=data
        )

        return {
            'status': 'success',
            'message': f'Driver event processed for {driver.user.get_full_name()}',
            'driver_id': driver.id
        }


class ControlFleetWebhookView(APIView):
    """
    Public webhook endpoint for ControlFleet inbound events.
    Authentication via X-ControlFleet-Key header (no JWT required).
    """

    authentication_classes = []  # No JWT auth
    permission_classes = []  # API key auth only

    def post(self, request):
        """
        Process ControlFleet webhook events.

        Expects X-ControlFleet-Key header for authentication.
        Routes to appropriate handler based on event_category.
        """
        adapter = ControlFleetAdapter()

        # Verify API key
        if not adapter.verify_api_key(request):
            return Response(
                {'error': 'Invalid or missing X-ControlFleet-Key header'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        data = request.data
        event_category = data.get('event_category', 'trip')  # Default to trip

        # Route to appropriate handler
        if event_category == 'trip':
            result = adapter.handle_trip_update(data)
        elif event_category == 'vehicle':
            result = adapter.handle_vehicle_event(data)
        elif event_category == 'driver':
            result = adapter.handle_driver_event(data)
        else:
            return Response(
                {'error': f'Unknown event_category: {event_category}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Return result
        if result['status'] == 'error':
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)
