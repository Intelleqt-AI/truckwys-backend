"""Shared fixtures for the trip-economics / TMS sync tests."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from core.models import Company, Customer, IntegrationAPIKey, Load

User = get_user_model()


def make_company(name, **extra):
    return Company.objects.create(company_name=name, **extra)


def make_user(username, company, role='ADMIN', **extra):
    user = User.objects.create_user(username=username, email=f'{username}@trip.test', password='x')
    user.role = role
    user.company = company
    for k, v in extra.items():
        setattr(user, k, v)
    user.save()
    return user


def make_customer(company, tag):
    return Customer.objects.create(
        company=company, name=f'Customer {tag}', email=f'{tag}@cust.test', phone='+27110000000',
        address='', city='JHB', state='', zip_code='')


def make_key(user, key, key_type='FLEET_TMS'):
    return IntegrationAPIKey.objects.create(name=f'key {key}', key=key, key_type=key_type, operator=user)


def make_load(company, customer, number, *, pickup='Johannesburg', delivery='Cape Town',
              pickup_lat=None, pickup_lng=None, delivery_lat=None, delivery_lng=None,
              distance='1400', total='30000', status='PENDING', pickup_in_days=1, days=2, **extra):
    now = timezone.now()
    return Load.objects.create(
        company=company, customer=customer, load_number=number,
        pickup_location=pickup, pickup_city=pickup, pickup_state='', pickup_zip='',
        pickup_lat=pickup_lat, pickup_lng=pickup_lng,
        pickup_date=now + timedelta(days=pickup_in_days),
        delivery_location=delivery, delivery_city=delivery, delivery_state='', delivery_zip='',
        delivery_lat=delivery_lat, delivery_lng=delivery_lng,
        delivery_date=now + timedelta(days=pickup_in_days + days),
        cargo_description='Freight', weight=Decimal('10000'), distance=Decimal(distance),
        rate=Decimal(total), total_amount=Decimal(total), status=status, **extra)


def make_vehicle_type(company, name='Trip Tautliner', burn=38, capacity=30):
    from core.models import VehicleType
    vt, _ = VehicleType.objects.get_or_create(company=company, name=name, defaults={
        'capacity': capacity, 'max_distance': 3000, 'base_rate': 20, 'fuel_consumption_l_per_100km': burn})
    return vt


def priced_quote(company, customer, number, *, vt, distance='600', total='30000', tolls='800',
                 minutes=420, origin='Johannesburg', destination='Durban', status='ACCEPTED',
                 trip_type='ONE_WAY', include_empty_return=None, **extra):
    """A saved quote with a real pricing snapshot (compute())."""
    from datetime import date, timedelta
    from core.models import Quote
    from core.services.quote_snapshot import snapshot_quote
    ci = {'vehicle_type_id': vt.id}
    if include_empty_return is not None:
        ci['include_empty_return'] = include_empty_return
    q = Quote.objects.create(
        company=company, customer=customer, quote_number=number, pickup_location=origin,
        delivery_location=destination, cargo_description='Pallets', weight=Decimal('10000'),
        distance=Decimal(distance), vehicle_type=vt.name, toll_charges=Decimal(tolls),
        estimated_duration_minutes=minutes, base_rate=Decimal(total), total_amount=Decimal(total),
        valid_until=date.today() + timedelta(days=14), status=status, trip_type=trip_type,
        costing_inputs=ci, **extra)
    snapshot_quote(q)
    q.refresh_from_db()
    return q
