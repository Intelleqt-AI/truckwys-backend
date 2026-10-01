"""Append-only Fast Pay ledger: writing entries, deriving balances, reconciling.

Every capacity change in ``core.services.facility_ledger`` (reserve, release,
disburse, settle, write-off, buy-back) posts here inside the same transaction
and under the same row locks, so the ledger and the cached
``Facility.outstanding``/``reserved`` cannot drift unless something bypasses
both. ``reconcile()`` proves they agree (nightly job + ``manage.py
capital_reconcile``), and also checks each advance's own ledger balance against
its status.

Balances are always sums: ``reserved = Σ reserved_delta``,
``outstanding = Σ outstanding_delta``, ``committed = reserved + outstanding``.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Q, Sum

from core.models import CapitalLedgerEntry

ZERO = Decimal('0.00')
E = CapitalLedgerEntry


def _q(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal('0.01'))


def _actor_fields(actor, actor_label: str):
    if actor is not None and getattr(actor, 'pk', None) and getattr(actor, 'is_authenticated', True) \
            and type(actor).__name__ != 'LenderUser':
        return actor, actor_label or getattr(actor, 'username', '')
    return None, actor_label or (str(actor) if actor is not None else 'system')


def post(entry_type: str, *, amount, reserved_delta=ZERO, outstanding_delta=ZERO, advance=None,
         facility=None, funder=None, company=None, debtor=None, invoice=None, actor=None,
         actor_label: str = '', reference: str = '', memo: str = '') -> CapitalLedgerEntry:
    """Write one ledger row. Context (facility, funder, company, debtor,
    invoice) is filled from ``advance`` when not given."""
    if advance is not None:
        facility = facility or getattr(advance, 'facility', None)
        invoice = invoice or getattr(advance, 'invoice', None)
        funder = funder or getattr(advance, 'funder', None)
        if debtor is None and getattr(advance, 'debtor_id', None):
            debtor = advance.debtor
    if facility is not None:
        funder = funder or getattr(facility, 'funder', None)
        company = company or getattr(facility, 'company', None)
    if invoice is not None:
        company = company or getattr(invoice, 'company', None)
        if debtor is None:
            customer = getattr(invoice, 'customer', None)
            if customer is not None and getattr(customer, 'debtor_identity_id', None):
                debtor = customer.debtor_identity
    user, label = _actor_fields(actor, actor_label)
    return E.objects.create(
        entry_type=entry_type, amount=_q(abs(Decimal(str(amount)))), reserved_delta=_q(reserved_delta),
        outstanding_delta=_q(outstanding_delta), advance=advance, facility=facility, funder=funder,
        company=company, debtor=debtor, invoice=invoice, actor=user, actor_label=label[:120],
        reference=(reference or '')[:200], memo=memo or '',
    )


def balances(qs=None, **filters) -> dict:
    """Derived balances over ledger rows matching ``filters``."""
    qs = (qs if qs is not None else E.objects.all()).filter(**filters)
    agg = qs.aggregate(r=Sum('reserved_delta'), o=Sum('outstanding_delta'))
    reserved, outstanding = _q(agg['r']), _q(agg['o'])
    return {'reserved': reserved, 'outstanding': outstanding, 'committed': reserved + outstanding}


def committed_by(funder, field: str) -> dict:
    """{key: committed} for the funder, grouped by a ledger field path
    (``debtor``, ``company``, ``debtor__sector``...). Zero rows dropped."""
    rows = (E.objects.filter(funder=funder).values(field)
            .annotate(r=Sum('reserved_delta'), o=Sum('outstanding_delta')))
    out = {}
    for row in rows:
        total = _q(row['r']) + _q(row['o'])
        if total != 0:
            out[row[field]] = total
    return out


def committed_by_pair(funder) -> dict:
    rows = (E.objects.filter(funder=funder).values('company', 'debtor')
            .annotate(r=Sum('reserved_delta'), o=Sum('outstanding_delta')))
    return {(r['company'], r['debtor']): _q(r['r']) + _q(r['o']) for r in rows
            if _q(r['r']) + _q(r['o']) != 0}


def advance_balance(advance) -> dict:
    return balances(advance=advance)


def reconcile(funder=None) -> dict:
    """Compare ledger-derived balances with the cached facility figures and
    with each advance's status. Returns ``{ok, breaks, checked}``; never writes."""
    from core.models import AdvanceRequest, Facility
    breaks = []
    facilities = Facility.objects.select_related('company')
    if funder is not None:
        facilities = facilities.filter(funder=funder)
    sums = {r['facility']: r for r in E.objects.filter(facility__in=facilities).values('facility')
            .annotate(r=Sum('reserved_delta'), o=Sum('outstanding_delta'))}
    for fac in facilities:
        row = sums.get(fac.pk, {'r': ZERO, 'o': ZERO})
        for field, ledger_value, cached in (('reserved', _q(row['r']), _q(fac.reserved)),
                                            ('outstanding', _q(row['o']), _q(fac.outstanding))):
            if ledger_value != cached:
                breaks.append({'kind': 'facility', 'facility_id': fac.pk,
                               'company': getattr(fac.company, 'company_name', ''),
                               'field': field, 'ledger': str(ledger_value), 'cached': str(cached)})

    advances = AdvanceRequest.objects.all()
    if funder is not None:
        advances = advances.filter(Q(funder=funder) | Q(facility__funder=funder))
    adv_sums = {r['advance']: r for r in E.objects.filter(advance__in=advances).values('advance')
                .annotate(r=Sum('reserved_delta'), o=Sum('outstanding_delta'))}
    for adv in advances.only('id', 'status', 'amount', 'capacity_reserved'):
        row = adv_sums.get(adv.pk)
        if row is None:
            continue  # legacy advance never touched by the ledger (e.g. long settled)
        r, o = _q(row['r']), _q(row['o'])
        expect_o = _q(adv.amount) if adv.status == 'DISBURSED' else ZERO
        expect_r = _q(adv.capacity_reserved) if adv.status in ('ELIGIBLE', 'QUEUED', 'REQUESTED',
                                                                   'SCORING', 'APPROVED') else ZERO
        if o != expect_o or r != expect_r:
            breaks.append({'kind': 'advance', 'advance_id': adv.pk, 'status': adv.status,
                           'ledger_reserved': str(r), 'expected_reserved': str(expect_r),
                           'ledger_outstanding': str(o), 'expected_outstanding': str(expect_o)})
    if funder is not None:
        pot = balances(funder=funder)
        if pot['reserved'] < 0 or pot['outstanding'] < 0:
            breaks.append({'kind': 'funder', 'funder_id': funder.pk, 'field': 'negative balance',
                           'ledger': str(pot['committed']), 'cached': ''})
    return {'ok': not breaks, 'breaks': breaks, 'checked': facilities.count()}
