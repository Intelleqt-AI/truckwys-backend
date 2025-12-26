from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from core.models import Vehicle, Driver, Load, Customer
from datetime import datetime, timedelta
from decimal import Decimal
from django.utils import timezone

User = get_user_model()

class Command(BaseCommand):
    help = 'Populate database with mock vehicle and related data'

    def handle(self, *args, **kwargs):
        # Create admin user if doesn't exist
        admin_user, created = User.objects.get_or_create(
            username='admin',
            defaults={
                'email': 'admin@truckwys.com',
                'first_name': 'Admin',
                'last_name': 'User',
                'role': 'ADMIN',
                'is_staff': True,
                'is_superuser': True
            }
        )
        if created:
            admin_user.set_password('admin123')
            admin_user.save()
            self.stdout.write(self.style.SUCCESS(f'Created admin user: admin/admin123'))

        # Create driver users
        drivers_data = [
            {'username': 'john_smith', 'first_name': 'John', 'last_name': 'Smith', 'license': 'DL-001'},
            {'username': 'sarah_jones', 'first_name': 'Sarah', 'last_name': 'Jones', 'license': 'DL-007'},
            {'username': 'mike_johnson', 'first_name': 'Mike', 'last_name': 'Johnson', 'license': 'DL-012'},
            {'username': 'lisa_brown', 'first_name': 'Lisa', 'last_name': 'Brown', 'license': 'DL-045'},
            {'username': 'david_wilson', 'first_name': 'David', 'last_name': 'Wilson', 'license': 'DL-023'},
            {'username': 'emma_davis', 'first_name': 'Emma', 'last_name': 'Davis', 'license': 'DL-089'},
        ]

        drivers = []
        for driver_data in drivers_data:
            user, _ = User.objects.get_or_create(
                username=driver_data['username'],
                defaults={
                    'email': f"{driver_data['username']}@truckwys.com",
                    'first_name': driver_data['first_name'],
                    'last_name': driver_data['last_name'],
                    'role': 'DRIVER',
                    'phone': '+27123456789'
                }
            )
            if _:
                user.set_password('driver123')
                user.save()

            driver, _ = Driver.objects.get_or_create(
                user=user,
                defaults={
                    'license_number': driver_data['license'],
                    'license_expiry': datetime.now().date() + timedelta(days=365),
                    'license_state': 'GP',
                    'hire_date': datetime.now().date() - timedelta(days=180),
                    'status': 'ACTIVE'
                }
            )
            drivers.append(driver)

        # Create vehicles with exact data from UI
        vehicles_data = [
            {
                'vin': 'VIN-TRK001', 'plate': 'TRK-001', 'make': 'Freightliner', 'model': 'Cascadia',
                'status': 'AVAILABLE', 'driver': drivers[0], 'ai_health_score': 87,
                'uptime_percentage': Decimal('94.2'), 'cost_per_km': Decimal('21.4'),
                'margin_per_trip': Decimal('8350.00')
            },
            {
                'vin': 'VIN-TRK007', 'plate': 'TRK-007', 'make': 'Volvo', 'model': 'FH16',
                'status': 'AVAILABLE', 'driver': drivers[1], 'ai_health_score': 72,
                'uptime_percentage': Decimal('82.5'), 'cost_per_km': Decimal('23.1'),
                'margin_per_trip': Decimal('6750.00')
            },
            {
                'vin': 'VIN-TRK012', 'plate': 'TRK-012', 'make': 'Mercedes', 'model': 'Actros',
                'status': 'AVAILABLE', 'driver': drivers[2], 'ai_health_score': 92,
                'uptime_percentage': Decimal('96.8'), 'cost_per_km': Decimal('19.8'),
                'margin_per_trip': Decimal('9100.00')
            },
            {
                'vin': 'VIN-TRK045', 'plate': 'TRK-045', 'make': 'Scania', 'model': 'R500',
                'status': 'AVAILABLE', 'driver': drivers[3], 'ai_health_score': 65,
                'uptime_percentage': Decimal('78.3'), 'cost_per_km': Decimal('24.5'),
                'margin_per_trip': Decimal('5200.00')
            },
            {
                'vin': 'VIN-TRK023', 'plate': 'TRK-023', 'make': 'MAN', 'model': 'TGX',
                'status': 'AVAILABLE', 'driver': drivers[4], 'ai_health_score': 81,
                'uptime_percentage': Decimal('91.5'), 'cost_per_km': Decimal('20.7'),
                'margin_per_trip': Decimal('7800.00')
            },
            {
                'vin': 'VIN-TRK089', 'plate': 'TRK-089', 'make': 'DAF', 'model': 'XF',
                'status': 'AVAILABLE', 'driver': drivers[5], 'ai_health_score': 74,
                'uptime_percentage': Decimal('85.0'), 'cost_per_km': Decimal('22.3'),
                'margin_per_trip': Decimal('6400.00')
            },
        ]

        for vehicle_data in vehicles_data:
            Vehicle.objects.update_or_create(
                vin=vehicle_data['vin'],
                defaults={
                    'plate': vehicle_data['plate'],
                    'make': vehicle_data['make'],
                    'model': vehicle_data['model'],
                    'year': 2022,
                    'type': 'TRUCK',
                    'capacity': Decimal('30000'),
                    'status': vehicle_data['status'],
                    'fuel_type': 'DIESEL',
                    'mileage': Decimal('50000'),
                    'ai_health_score': vehicle_data['ai_health_score'],
                    'uptime_percentage': vehicle_data['uptime_percentage'],
                    'cost_per_km': vehicle_data['cost_per_km'],
                    'margin_per_trip': vehicle_data['margin_per_trip'],
                    'fuel_efficiency_score': 75,
                    'uptime_score': 85,
                    'maintenance_score': 78,
                    'next_maintenance_due': datetime.now().date() + timedelta(days=30)
                }
            )

        # Create sample customer
        customer, _ = Customer.objects.get_or_create(
            email='customer@example.com',
            defaults={
                'name': 'ABC Logistics',
                'company': 'ABC Logistics Ltd',
                'phone': '+27123456789',
                'address': '123 Main St',
                'city': 'Johannesburg',
                'state': 'GP',
                'zip_code': '2000',
                'status': 'ACTIVE'
            }
        )

        # Create sample loads
        vehicles = Vehicle.objects.all()
        for i, vehicle in enumerate(vehicles):
            # Safely get driver - use modulo to wrap around if more vehicles than drivers
            driver = drivers[i % len(drivers)] if drivers else None
            
            Load.objects.get_or_create(
                load_number=f'LD-2025-{str(i+1).zfill(3)}',
                defaults={
                    'customer': customer,
                    'driver': driver,
                    'vehicle': vehicle,
                    'pickup_location': 'Johannesburg',
                    'pickup_city': 'Johannesburg',
                    'pickup_state': 'GP',
                    'pickup_zip': '2000',
                    'pickup_date': timezone.now() + timedelta(days=1),
                    'delivery_location': 'Cape Town',
                    'delivery_city': 'Cape Town',
                    'delivery_state': 'WC',
                    'delivery_zip': '8000',
                    'delivery_date': timezone.now() + timedelta(days=3),
                    'cargo_description': 'General Freight',
                    'weight': Decimal('15000'),
                    'distance': Decimal('1400'),
                    'rate': Decimal('10000'),
                    'fuel_surcharge': Decimal('500'),
                    'additional_charges': Decimal('200'),
                    'total_amount': vehicle.margin_per_trip,
                    'status': 'IN_TRANSIT' if i % 2 == 0 else 'ASSIGNED',
                    'created_by': admin_user
                }
            )

        self.stdout.write(self.style.SUCCESS('Successfully populated database with mock data'))
        self.stdout.write(self.style.SUCCESS(f'Created {Vehicle.objects.count()} vehicles'))
        self.stdout.write(self.style.SUCCESS(f'Created {Driver.objects.count()} drivers'))
        self.stdout.write(self.style.SUCCESS(f'Created {Load.objects.count()} loads'))
