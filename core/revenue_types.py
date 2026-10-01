"""Revenue types: what an invoice line charges for.

Each type is mapped to an income account per accounting connection
(core.accounting.mapping). Pure Python, shared by models, services,
serializers and migrations.
"""
FREIGHT = 'FREIGHT'
FUEL_SURCHARGE = 'FUEL_SURCHARGE'
TOLLS = 'TOLLS'
EXTRA_KM = 'EXTRA_KM'
WAITING_TIME = 'WAITING_TIME'
OTHER = 'OTHER'

REVENUE_TYPE_CHOICES = [
    (FREIGHT, 'Freight'),
    (FUEL_SURCHARGE, 'Fuel surcharge'),
    (TOLLS, 'Tolls recharged'),
    (EXTRA_KM, 'Extra km'),
    (WAITING_TIME, 'Waiting time'),
    (OTHER, 'Other'),
]
REVENUE_TYPES = [c for c, _ in REVENUE_TYPE_CHOICES]

# Exact prefixes of the descriptions TruckWys' own generators write
# (core.services.invoice_generator). Used once, by migration 0139, to type
# lines that existed before revenue_type did; it is not a guess about free
# text a user typed (those stay FREIGHT, the default, and can be changed on a
# draft).
GENERATED_PREFIXES = (
    ('Fuel Surcharge', FUEL_SURCHARGE),
    ('Toll Charges', TOLLS),
    ('Extra Distance (', EXTRA_KM),
    ('Driver Premium (', OTHER),
)


def type_for_generated_description(description: str) -> str:
    for prefix, kind in GENERATED_PREFIXES:
        if (description or '').startswith(prefix):
            return kind
    return FREIGHT
