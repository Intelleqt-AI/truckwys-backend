"""
Management command: seed_cross_border_data

SA's neighbours (Zimbabwe, Botswana, Namibia, Lesotho, Eswatini,
Mozambique) and Zambia/Malawi via Zimbabwe are NOT seeded here any more:
they are priced from the sourced, per-component schedule in
core/services/border_schedule.py (each charge with its own source, as-of
date, verified flag and currency). Migration 0165 retired their old rand
totals.

What remains are the further multi-hop corridors (Zambia→Tanzania,
Tanzania→Kenya) and those countries' per-km rates: industry ESTIMATES with
no source, shown on a quote as estimates. No weighbridge fees: no country
charges a compliant truck for being weighed.

Usage:
    python manage.py seed_cross_border_data
    python manage.py seed_cross_border_data --force   # overwrite existing records
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

_BORDER_FEES = [
    {'from_country': 'ZM', 'to_country': 'TZ', 'fee_zar': Decimal('1200.00'),
     'notes': 'Nakonde / Tunduma — industry estimate, no source'},
    {'from_country': 'TZ', 'to_country': 'KE', 'fee_zar': Decimal('1100.00'),
     'notes': 'Namanga / Lunga Lunga — industry estimate, no source'},
]

_COUNTRY_RATES = [
    {'country_code': 'TZ', 'country_name': 'Tanzania', 'weighbridge_fee_zar': Decimal('0.00'),
     'toll_rate_per_km': Decimal('0.600'), 'sa_border_distance_km': Decimal('580.0')},
    {'country_code': 'KE', 'country_name': 'Kenya', 'weighbridge_fee_zar': Decimal('0.00'),
     'toll_rate_per_km': Decimal('0.650'), 'sa_border_distance_km': Decimal('580.0')},
]


class Command(BaseCommand):
    help = 'Seed the multi-hop (ZM-TZ, TZ-KE) border estimates. SA neighbours are priced by core/services/border_schedule.py.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Overwrite existing records')

    def handle(self, *args, **options):
        from core.models.border_crossing_fee import BorderCrossingFee
        from core.models.country_transit_rate import CountryTransitRate

        force = options['force']
        fee_created = fee_updated = rate_created = rate_updated = 0

        for item in _BORDER_FEES:
            # These figures are the heavy (>20,000kg) band — the one every
            # corridor has since 0120 — so that is the row they key on.
            key = {'from_country': item['from_country'], 'to_country': item['to_country'],
                   'min_weight_kg': 20_001}
            if force:
                _, created = BorderCrossingFee.objects.update_or_create(defaults=item, **key)
            else:
                _, created = BorderCrossingFee.objects.get_or_create(defaults=item, **key)
            if created:
                fee_created += 1
            else:
                fee_updated += 1

        for item in _COUNTRY_RATES:
            key = {'country_code': item['country_code']}
            if force:
                _, created = CountryTransitRate.objects.update_or_create(defaults=item, **key)
            else:
                _, created = CountryTransitRate.objects.get_or_create(defaults=item, **key)
            if created:
                rate_created += 1
            else:
                rate_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'BorderCrossingFee: {fee_created} created, {fee_updated} skipped.\n'
            f'CountryTransitRate: {rate_created} created, {rate_updated} skipped.\n'
            f'SA-neighbour charges come from core/services/border_schedule.py (not seeded).\n'
            f'Re-run with --force to overwrite existing records.'
        ))
