"""Builders for Fast Pay tests: a funder, a transporter line, and an invoice that passes eligibility.

``make_fundable(...)`` turns the minimal fixtures older tests use (company,
customer, facility, delivered load) into one the decision engine will fund:
the line sits under a funder, the customer is a CIPC-identified debtor, the
transporter has an approved application with consents and GIT insurance, and
the load has an in-app camera POD (tier V2).
"""
from __future__ import annotations

import hashlib
import itertools
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

_seq = itertools.count(1)


def make_funder(code=None, *, pot='10000000', status='ACTIVE', staff_may_approve=True, **kw):
    from core.models import Funder
    code = code or f'funder-{next(_seq)}'
    return Funder.objects.create(name=kw.pop('name', f'Test funder {code}'), code=code, pot_limit=Decimal(pot),
                                 status=status, staff_may_approve=staff_may_approve, **kw)


def approve_application(company, *, git_days=365):
    from core.capital.policy import CONSENT_TEXT_VERSION, REQUIRED_CONSENTS
    from core.models import CapitalApplication
    now = timezone.now().isoformat()
    app, _ = CapitalApplication.objects.get_or_create(company=company)
    app.status = 'APPROVED'
    app.juristic_person = True
    app.git_insurer = 'Test Insurer'
    app.git_insurance_expiry = date.today() + timedelta(days=git_days)
    app.consents = [{'purpose': c, 'text_version': CONSENT_TEXT_VERSION, 'granted_at': now}
                    for c in REQUIRED_CONSENTS]
    app.save()
    return app


def identify_customer(customer, *, reg=None, sector='RETAIL_FMCG', **debtor_fields):
    from core.services.identity import link_debtor_identity
    n = next(_seq)
    customer.registration_number = reg or f'2010/{100000 + n:06d}/07'
    customer.save(update_fields=['registration_number'])
    debtor = link_debtor_identity(customer)
    customer.debtor_identity = debtor
    customer.save(update_fields=['debtor_identity'])
    debtor.sector = sector
    debtor.legal_name = debtor_fields.pop('legal_name', customer.name)
    for k, v in debtor_fields.items():
        setattr(debtor, k, v)
    debtor.save()
    return debtor


def camera_pod(load, *, delivered_days_ago=2):
    from core.models import Load
    when = timezone.now() - timedelta(days=delivered_days_ago)
    sha = hashlib.sha256(f'pod-{load.pk}-{next(_seq)}'.encode()).hexdigest()
    Load.objects.filter(pk=load.pk).update(
        status='DELIVERED', actual_delivered_at=when, pod_source='CAMERA', pod_captured_at=when.replace(hour=12),
        pod_latitude=Decimal('-26.100000'), pod_longitude=Decimal('28.050000'),
        delivery_lat=Decimal('-26.1000000'), delivery_lng=Decimal('28.0500000'),
        pod_file_sha256=sha, pod_document=f'pod/test-{load.pk}.jpg',
        pod_signature=load.pod_signature or 'Signed: receiving clerk')
    load.refresh_from_db()
    return load


def make_fundable(company, customer, facility, load=None, *, funder=None, sector='RETAIL_FMCG', reg=None):
    """Make (company, customer, facility, load) pass every eligibility rule. Returns the funder."""
    if funder is None:
        funder = facility.funder or make_funder()
    if facility.funder_id != funder.pk:
        type(facility).objects.filter(pk=facility.pk).update(funder=funder)
        facility.refresh_from_db()
    approve_application(company)
    if customer.debtor_identity_id is None:
        identify_customer(customer, reg=reg, sector=sector)
    if load is not None:
        camera_pod(load)
    return funder
