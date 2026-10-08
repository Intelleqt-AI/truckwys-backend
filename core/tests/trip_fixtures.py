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
