from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from core.models import Quote, Customer
from datetime import datetime, timedelta
from decimal import Decimal

User = get_user_model()

class Command(BaseCommand):
    help = 'Populate database with mock quotes matching the UI'

    def handle(self, *args, **kwargs):
        # Get or create admin user
        admin_user = User.objects.filter(username='admin').first()
        if not admin_user:
            self.stdout.write(self.style.ERROR('Admin user not found. Run populate_vehicles first.'))
            return
        
        # Get or create customers
        customers_data = [
            {'name': 'Makana Foods', 'email': 'contact@makanafoods.co.za'},
            {'name': 'Tiger Brands', 'email': 'info@tigerbrands.com'},
            {'name': 'Pick n Pay', 'email': 'logistics@pnp.co.za'},
            {'name': 'Shoprite', 'email': 'logistics@shoprite.co.za'},
            {'name': 'Woolworths', 'email': 'transport@woolworths.co.za'},
        ]
        
        customers = {}
        for cust_data in customers_data:
            customer, _ = Customer.objects.get_or_create(
                email=cust_data['email'],
                defaults={
                    'name': cust_data['name'],
                    'company': cust_data['name'],
                    'phone': '+27123456789',
                    'address': '123 Business Park',
                    'city': 'Johannesburg',
                    'state': 'GP',
                    'zip_code': '2000',
                    'status': 'ACTIVE'
                }
            )
            customers[cust_data['name']] = customer
        
        # Create quotes matching mockQuotes
        quotes_data = [
            {
                'quote_number': 'Q-1001',
                'customer': customers['Makana Foods'],
                'origin': 'JHB',
                'destination': 'CPT',
                'pickup_location': 'Johannesburg',
                'delivery_location': 'Cape Town',
                'sla_hours': 48,
                'total_amount': Decimal('21500.00'),
                'margin_percentage': Decimal('12.4'),
                'confidence': 'HIGH',
                'status': 'DRAFT',
                'updated_at': datetime(2025, 8, 28, 12, 12, 0)
            },
            {
                'quote_number': 'Q-1002',
                'customer': customers['Tiger Brands'],
                'origin': 'DUR',
                'destination': 'JHB',
                'pickup_location': 'Durban',
                'delivery_location': 'Johannesburg',
                'sla_hours': 24,
                'total_amount': Decimal('18900.00'),
                'margin_percentage': Decimal('15.2'),
                'confidence': 'MEDIUM',
                'status': 'SENT',
                'updated_at': datetime(2025, 8, 29, 9, 30, 0)
            },
            {
                'quote_number': 'Q-1003',
                'customer': customers['Pick n Pay'],
                'origin': 'CPT',
                'destination': 'PE',
                'pickup_location': 'Cape Town',
                'delivery_location': 'Port Elizabeth',
                'sla_hours': 72,
                'total_amount': Decimal('8500.00'),
                'margin_percentage': Decimal('9.8'),
                'confidence': 'LOW',
                'status': 'ACCEPTED',
                'updated_at': datetime(2025, 8, 25, 14, 45, 0)
            },
            {
                'quote_number': 'Q-1004',
                'customer': customers['Shoprite'],
                'origin': 'JHB',
                'destination': 'DBN',
                'pickup_location': 'Johannesburg',
                'delivery_location': 'Durban',
                'sla_hours': 36,
                'total_amount': Decimal('16200.00'),
                'margin_percentage': Decimal('11.5'),
                'confidence': 'HIGH',
                'status': 'DRAFT',
                'updated_at': datetime(2025, 8, 27, 16, 20, 0)
            },
            {
                'quote_number': 'Q-1005',
                'customer': customers['Woolworths'],
                'origin': 'PE',
                'destination': 'CPT',
                'pickup_location': 'Port Elizabeth',
                'delivery_location': 'Cape Town',
                'sla_hours': 48,
                'total_amount': Decimal('12800.00'),
                'margin_percentage': Decimal('13.2'),
                'confidence': 'MEDIUM',
                'status': 'SENT',
                'updated_at': datetime(2025, 8, 28, 11, 15, 0)
            },
            {
                'quote_number': 'Q-1006',
                'customer': customers['Tiger Brands'],
                'origin': 'JHB',
                'destination': 'CPT',
                'pickup_location': 'Johannesburg',
                'delivery_location': 'Cape Town',
                'sla_hours': 48,
                'total_amount': Decimal('22400.00'),
                'margin_percentage': Decimal('14.8'),
                'confidence': 'HIGH',
                'status': 'IN_TRANSIT',
                'updated_at': datetime(2025, 8, 29, 8, 45, 0)
            },
            {
                'quote_number': 'Q-1007',
                'customer': customers['Makana Foods'],
                'origin': 'DBN',
                'destination': 'PE',
                'pickup_location': 'Durban',
                'delivery_location': 'Port Elizabeth',
                'sla_hours': 60,
                'total_amount': Decimal('14500.00'),
                'margin_percentage': Decimal('10.2'),
                'confidence': 'MEDIUM',
                'status': 'COMPLETED',
                'updated_at': datetime(2025, 8, 26, 13, 30, 0)
            },
            {
                'quote_number': 'Q-1008',
                'customer': customers['Shoprite'],
                'origin': 'CPT',
                'destination': 'JHB',
                'pickup_location': 'Cape Town',
                'delivery_location': 'Johannesburg',
                'sla_hours': 48,
                'total_amount': Decimal('19800.00'),
                'margin_percentage': Decimal('12.9'),
                'confidence': 'HIGH',
                'status': 'DRAFT',
                'updated_at': datetime(2025, 8, 29, 15, 10, 0)
            },
        ]
        
        for quote_data in quotes_data:
            Quote.objects.update_or_create(
                quote_number=quote_data['quote_number'],
                defaults={
                    'customer': quote_data['customer'],
                    'origin': quote_data['origin'],
                    'destination': quote_data['destination'],
                    'pickup_location': quote_data['pickup_location'],
                    'delivery_location': quote_data['delivery_location'],
                    'sla_hours': quote_data['sla_hours'],
                    'cargo_description': 'General Freight',
                    'weight': Decimal('15000.00'),
                    'distance': Decimal('1400.00'),
                    'base_rate': quote_data['total_amount'],
                    'total_amount': quote_data['total_amount'],
                    'margin_percentage': quote_data['margin_percentage'],
                    'confidence': quote_data['confidence'],
                    'status': quote_data['status'],
                    'valid_until': (datetime.now() + timedelta(days=30)).date(),
                    'created_by': admin_user,
                    'updated_at': quote_data['updated_at']
                }
            )
        
        self.stdout.write(self.style.SUCCESS(f'Successfully created {len(quotes_data)} quotes'))
        self.stdout.write(self.style.SUCCESS(f'Total quotes in DB: {Quote.objects.count()}'))
