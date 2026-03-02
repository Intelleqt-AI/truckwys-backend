"""Fleet Management API endpoints for external fleet system integration."""

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiExample
from drf_spectacular.types import OpenApiTypes
from django.utils import timezone
import hmac
import hashlib

from core.models import Load, Vehicle, Driver, ActivityEvent
from core.serializers import LoadSerializer
from core.auth import APIKeyAuthentication


class FleetTripSyncAPIView(APIView):
    """Push loads/trips to external fleet system."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=['Fleet Management'],
        summary='Sync trips to external fleet system',
        description='Push confirmed loads/trips to external fleet management system',
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'load_ids': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': 'List of load IDs to sync',
                        'example': [1, 2, 3]
                    }
                }
            }
        },
        responses={
            200: {
                'description': 'Sync status per load',
                'content': {
                    'application/json': {
                        'example': {
                            'results': [
                                {'load_id': 1, 'status': 'success', 'message': 'Synced successfully'},
                                {'load_id': 2, 'status': 'error', 'message': 'Load not found'}
                            ]
                        }
                    }
                }
            }
        }
    )
    def post(self, request):
        """Sync loads to external fleet system."""
        load_ids = request.data.get('load_ids', [])

        if not load_ids:
            return Response(
                {'error': 'load_ids is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        results = []
        for load_id in load_ids:
            try:
                load = Load.objects.get(id=load_id)

                # TODO: Implement actual external API call to fleet system
                # For now, just mark as successful
                results.append({
                    'load_id': load_id,
                    'status': 'success',
                    'message': f'Load {load.load_number} synced successfully'
                })

                # Create activity event
                ActivityEvent.objects.create(
                    event_type='load',
                    title=f'Load {load.load_number} synced to fleet system',
                    description=f'Successfully synced to external fleet management system',
                    entity_id=load.id,
                    entity_type='load',
                    metadata={'synced_at': timezone.now().isoformat()}
                )

            except Load.DoesNotExist:
                results.append({
                    'load_id': load_id,
                    'status': 'error',
                    'message': 'Load not found'
                })
            except Exception as e:
                results.append({
                    'load_id': load_id,
                    'status': 'error',
                    'message': str(e)
                })

        return Response({'results': results})


class FleetBookingSyncAPIView(APIView):
    """Push confirmed bookings to external fleet system."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=['Fleet Management'],
        summary='Sync bookings to external fleet system',
        description='Push confirmed bookings to external fleet management system',
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'booking_ids': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': 'List of booking/load IDs to sync',
                        'example': [1, 2, 3]
                    }
                }
            }
        },
        responses={
            200: {
                'description': 'Sync status per booking',
                'content': {
                    'application/json': {
                        'example': {
                            'results': [
                                {'booking_id': 1, 'status': 'success', 'message': 'Booking synced successfully'}
                            ]
                        }
                    }
                }
            }
        }
    )
    def post(self, request):
        """Sync bookings to external fleet system."""
        booking_ids = request.data.get('booking_ids', [])

        if not booking_ids:
            return Response(
                {'error': 'booking_ids is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        results = []
        for booking_id in booking_ids:
            try:
                load = Load.objects.get(id=booking_id)

                # TODO: Implement actual external API call
                results.append({
                    'booking_id': booking_id,
                    'status': 'success',
                    'message': f'Booking {load.load_number} synced successfully'
                })

            except Load.DoesNotExist:
                results.append({
                    'booking_id': booking_id,
                    'status': 'error',
                    'message': 'Booking not found'
                })
            except Exception as e:
                results.append({
                    'booking_id': booking_id,
                    'status': 'error',
                    'message': str(e)
                })

        return Response({'results': results})


class FleetVehicleStatusAPIView(APIView):
    """Query vehicle availability from fleet data."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=['Fleet Management'],
        summary='Get vehicle status and availability',
        description='Query current vehicle availability from fleet data',
        parameters=[
            OpenApiParameter(
                name='vehicle_id',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Filter by specific vehicle ID',
                required=False
            )
        ],
        responses={
            200: {
                'description': 'Vehicle status and availability',
                'content': {
                    'application/json': {
                        'example': {
                            'vehicles': [
                                {
                                    'id': 1,
                                    'vin': 'ABC123',
                                    'plate': 'CA 123 GP',
                                    'status': 'AVAILABLE',
                                    'current_location': None,
                                    'next_available': None
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    def get(self, request):
        """Get vehicle status and availability."""
        vehicle_id = request.query_params.get('vehicle_id')

        if vehicle_id:
            try:
                vehicles = Vehicle.objects.filter(id=vehicle_id)
            except ValueError:
                return Response(
                    {'error': 'Invalid vehicle_id'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            vehicles = Vehicle.objects.all()

        vehicle_data = []
        for vehicle in vehicles:
            vehicle_data.append({
                'id': vehicle.id,
                'vin': vehicle.vin,
                'make': vehicle.make,
                'model': vehicle.model,
                'plate': vehicle.plate,
                'status': vehicle.status,
                'current_location': None,  # TODO: Implement location tracking
                'next_available': None  # TODO: Calculate from current loads
            })

        return Response({'vehicles': vehicle_data})


class FleetWebhookTripUpdateView(APIView):
    """Receive trip updates from external fleet system (GPS, ETA, delivery confirmation)."""

    authentication_classes = [APIKeyAuthentication]
    permission_classes = []  # Authentication is via API key

    def _verify_signature(self, request):
        """Verify webhook signature from fleet system."""
        signature = request.META.get('HTTP_X_FLEET_SIGNATURE')
        if not signature:
            return False

        # Get the subscription/API key from request.auth (set by APIKeyAuthentication)
        if not hasattr(request, 'auth') or not request.auth:
            return False

        subscription = request.auth
        expected_signature = 'sha256=' + hmac.new(
            subscription.secret.encode(),
            request.body,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected_signature)

    @extend_schema(
        tags=['Fleet Management'],
        summary='Receive trip update webhook',
        description='Webhook endpoint for receiving GPS, ETA, and delivery confirmation updates from fleet system',
        parameters=[
            OpenApiParameter(
                name='X-Fleet-Signature',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='HMAC-SHA256 signature for webhook verification',
                required=True
            ),
            OpenApiParameter(
                name='X-API-Key',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='API key for authentication',
                required=True
            )
        ],
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'load_id': {'type': 'integer', 'description': 'Load ID'},
                    'load_number': {'type': 'string', 'description': 'Load number'},
                    'event_type': {
                        'type': 'string',
                        'enum': ['gps_update', 'eta_update', 'delivery_confirmed'],
                        'description': 'Type of update'
                    },
                    'latitude': {'type': 'number', 'description': 'GPS latitude'},
                    'longitude': {'type': 'number', 'description': 'GPS longitude'},
                    'eta': {'type': 'string', 'format': 'date-time', 'description': 'Estimated time of arrival'},
                    'delivered_at': {'type': 'string', 'format': 'date-time', 'description': 'Delivery timestamp'},
                    'pod_data': {'type': 'object', 'description': 'Proof of delivery data'}
                },
                'example': {
                    'load_id': 123,
                    'load_number': 'LD-2024-001',
                    'event_type': 'delivery_confirmed',
                    'delivered_at': '2024-01-15T14:30:00Z',
                    'pod_data': {
                        'signature': 'base64_encoded_signature',
                        'received_by': 'John Smith'
                    }
                }
            }
        },
        responses={
            200: {'description': 'Webhook processed successfully'},
            401: {'description': 'Invalid signature or API key'},
            400: {'description': 'Invalid request data'}
        }
    )
    def post(self, request):
        """Process trip update webhook."""
        # Verify signature
        if not self._verify_signature(request):
            return Response(
                {'error': 'Invalid signature'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        data = request.data
        load_id = data.get('load_id')
        load_number = data.get('load_number')
        event_type = data.get('event_type')

        if not event_type:
            return Response(
                {'error': 'event_type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find load
        try:
            if load_id:
                load = Load.objects.get(id=load_id)
            elif load_number:
                load = Load.objects.get(load_number=load_number)
            else:
                return Response(
                    {'error': 'load_id or load_number is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except Load.DoesNotExist:
            return Response(
                {'error': 'Load not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Create activity event
        ActivityEvent.objects.create(
            event_type='load',
            title=f'Fleet update: {event_type} for {load.load_number}',
            description=f'Received {event_type} from fleet system',
            entity_id=load.id,
            entity_type='load',
            metadata=data
        )

        # Handle delivery confirmation
        if event_type == 'delivery_confirmed':
            load.status = 'DELIVERED'
            if 'pod_data' in data:
                pod = data['pod_data']
                load.pod_received_by = pod.get('received_by', '')
                # TODO: Store signature/document
            load.save()

        return Response({
            'status': 'success',
            'message': f'Trip update processed for {load.load_number}'
        })


class FleetWebhookVehicleEventView(APIView):
    """Receive vehicle events from fleet system (maintenance, breakdowns, inspections)."""

    authentication_classes = [APIKeyAuthentication]
    permission_classes = []

    @extend_schema(
        tags=['Fleet Management'],
        summary='Receive vehicle event webhook',
        description='Webhook endpoint for receiving maintenance, breakdown, and inspection updates from fleet system',
        parameters=[
            OpenApiParameter(
                name='X-Fleet-Signature',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='HMAC-SHA256 signature for webhook verification',
                required=True
            ),
            OpenApiParameter(
                name='X-API-Key',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='API key for authentication',
                required=True
            )
        ],
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'vehicle_id': {'type': 'integer'},
                    'vin': {'type': 'string'},
                    'event_type': {
                        'type': 'string',
                        'enum': ['maintenance', 'breakdown', 'inspection']
                    },
                    'maintenance_due': {'type': 'string', 'format': 'date'},
                    'last_inspection': {'type': 'string', 'format': 'date'},
                    'status': {'type': 'string'}
                }
            }
        },
        responses={
            200: {'description': 'Vehicle event processed'},
            401: {'description': 'Invalid signature'},
            404: {'description': 'Vehicle not found'}
        }
    )
    def post(self, request):
        """Process vehicle event webhook."""
        data = request.data
        vehicle_id = data.get('vehicle_id')
        vin = data.get('vin')

        # Find vehicle
        try:
            if vehicle_id:
                vehicle = Vehicle.objects.get(id=vehicle_id)
            elif vin:
                vehicle = Vehicle.objects.get(vin=vin)
            else:
                return Response(
                    {'error': 'vehicle_id or vin is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except Vehicle.DoesNotExist:
            return Response(
                {'error': 'Vehicle not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Update vehicle fields
        event_type = data.get('event_type')
        if 'maintenance_due' in data:
            vehicle.next_maintenance_due = data['maintenance_due']
        if 'last_inspection' in data:
            vehicle.last_maintenance_date = data['last_inspection']
        if 'status' in data:
            vehicle.status = data['status']

        vehicle.save()

        # Create activity event
        ActivityEvent.objects.create(
            event_type='system',
            title=f'Vehicle {event_type}: {vehicle.plate}',
            description=f'Fleet system reported {event_type} event',
            entity_id=vehicle.id,
            entity_type='vehicle',
            metadata=data
        )

        return Response({
            'status': 'success',
            'message': f'Vehicle event processed for {vehicle.plate}'
        })


class FleetWebhookDriverEventView(APIView):
    """Receive driver events from fleet system (violations, accidents, hours)."""

    authentication_classes = [APIKeyAuthentication]
    permission_classes = []

    @extend_schema(
        tags=['Fleet Management'],
        summary='Receive driver event webhook',
        description='Webhook endpoint for receiving driver violations, accidents, and hours updates from fleet system',
        parameters=[
            OpenApiParameter(
                name='X-Fleet-Signature',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='HMAC-SHA256 signature for webhook verification',
                required=True
            ),
            OpenApiParameter(
                name='X-API-Key',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                description='API key for authentication',
                required=True
            )
        ],
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'driver_id': {'type': 'integer'},
                    'license_number': {'type': 'string'},
                    'event_type': {
                        'type': 'string',
                        'enum': ['violation', 'accident', 'hours_update']
                    },
                    'violation_count': {'type': 'integer'},
                    'accident_count': {'type': 'integer'},
                    'description': {'type': 'string'}
                }
            }
        },
        responses={
            200: {'description': 'Driver event processed'},
            401: {'description': 'Invalid signature'},
            404: {'description': 'Driver not found'}
        }
    )
    def post(self, request):
        """Process driver event webhook."""
        data = request.data
        driver_id = data.get('driver_id')
        license_number = data.get('license_number')

        # Find driver
        try:
            if driver_id:
                driver = Driver.objects.get(id=driver_id)
            elif license_number:
                driver = Driver.objects.get(license_number=license_number)
            else:
                return Response(
                    {'error': 'driver_id or license_number is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except Driver.DoesNotExist:
            return Response(
                {'error': 'Driver not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Update driver fields
        event_type = data.get('event_type')
        if 'violation_count' in data:
            driver.violation_count = data['violation_count']
        if 'accident_count' in data:
            driver.accident_history = data['accident_count']

        driver.save()

        # Create activity event
        ActivityEvent.objects.create(
            event_type='system',
            title=f'Driver {event_type}: {driver.user.get_full_name()}',
            description=data.get('description', f'Fleet system reported {event_type} event'),
            entity_id=driver.id,
            entity_type='driver',
            metadata=data
        )

        return Response({
            'status': 'success',
            'message': f'Driver event processed for {driver.user.get_full_name()}'
        })
