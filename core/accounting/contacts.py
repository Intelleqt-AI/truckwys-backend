"""Linking TruckWys customers and suppliers to provider contacts.

Order of evidence on first link (approved design):
    1. an existing link (external id)
    2. VAT number           (exact, normalised)
    3. registration number  (exact, normalised CIPC)
    4. e-mail address       (exact, case-insensitive)
    5. normalised legal name -> only a SUGGESTION; a person confirms it

1-4 link automatically when exactly one provider contact matches and it
isn't linked to another TruckWys record already; otherwise the candidates
become a suggestion. No match at all -> CREATE (the contact is created in
the provider on first push). Nothing is ever created in TruckWys from the
provider's contact list (the old prototype did, across tenants).
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.accounting.base import Contact, PermanentError
from core.accounting.events import log_event
from core.accounting.registry import get_adapter
from core.services.identity import legal_name_key, normalise_registration_number, normalise_vat_number

KINDS = {'CUSTOMER': 'CONTACT_CUSTOMER', 'SUPPLIER': 'CONTACT_SUPPLIER'}
KIND_OF = {v: k for k, v in KINDS.items()}
MATCHED_STATUSES = ('SYNCED',)


class ContactBlocked(Exception):
    """The document can't be pushed until a person resolves its contact."""


def _model(object_type):
    from core.models import Customer, Supplier
    return Customer if object_type == 'CONTACT_CUSTOMER' else Supplier


def local_identity(obj) -> dict:
    vat = getattr(obj, 'vat_number', '') or ''
    reg = getattr(obj, 'registration_number', '') or ''
    country = getattr(obj, 'country', 'ZA') or 'ZA'
    try:
        vat = normalise_vat_number(vat, country) if vat else ''
    except Exception:
        vat = ''.join(ch for ch in vat if ch.isdigit())
    try:
        reg = normalise_registration_number(reg, country) if reg else ''
    except Exception:
        reg = reg.strip()
    name = getattr(obj, 'company_name', '') or getattr(obj, 'name', '')
    return {'vat': vat, 'reg': reg, 'email': (getattr(obj, 'email', '') or '').strip().lower(),
            'name': name or getattr(obj, 'name', ''), 'name_key': legal_name_key(name or getattr(obj, 'name', ''))}


def remote_identity(c: Contact) -> dict:
    try:
        vat = normalise_vat_number(c.vat_number, 'ZA') if c.vat_number else ''
    except Exception:
        vat = ''.join(ch for ch in (c.vat_number or '') if ch.isdigit())
    try:
        reg = normalise_registration_number(c.registration_number, 'ZA') if c.registration_number else ''
    except Exception:
        reg = (c.registration_number or '').strip()
    return {'vat': vat, 'reg': reg, 'email': (c.email or '').strip().lower(), 'name_key': legal_name_key(c.name)}


def _candidate(c: Contact, method: str) -> dict:
    return {'external_id': c.external_id, 'name': c.name, 'vat': c.vat_number, 'email': c.email, 'method': method}


def get_link(connection, object_type, local_id):
    from core.models import ExternalLink
    return ExternalLink.objects.filter(connection=connection, object_type=object_type, local_id=local_id).first()


def _taken(connection, object_type, external_id, local_id) -> bool:
    from core.models import ExternalLink
    return ExternalLink.objects.filter(connection=connection, object_type=object_type,
                                       external_id=external_id).exclude(local_id=local_id).exists()


def decide(connection, object_type, obj, contacts: list[Contact]):
    """Pure decision over a candidate list -> (status, method, contact|None, candidates)."""
    me = local_identity(obj)
    by = [('vat', me['vat']), ('registration', me['reg']), ('email', me['email'])]
    remote = [(c, remote_identity(c)) for c in contacts]
    for method, value in by:
        if not value:
            continue
        field = {'vat': 'vat', 'registration': 'reg', 'email': 'email'}[method]
        hits = [c for c, r in remote if r[field] and r[field] == value]
        free = [c for c in hits if not _taken(connection, object_type, c.external_id, obj.pk)]
        if len(free) == 1:
            return 'SYNCED', method, free[0], []
        if hits:
            return 'SUGGESTED', method, None, [_candidate(c, method) for c in hits][:10]
    if me['name_key']:
        hits = [c for c, r in remote if r['name_key'] and r['name_key'] == me['name_key']]
        if hits:
            return 'SUGGESTED', 'name', None, [_candidate(c, 'name') for c in hits][:10]
    return 'CREATE', '', None, []


def _save_decision(connection, object_type, obj, status, method, contact, candidates):
    from core.models import ExternalLink
    link = get_link(connection, object_type, obj.pk)
    if link and ((link.status == 'SYNCED' and link.external_id) or link.status == 'SKIPPED'):
        return link   # never overwrite a confirmed/explicit decision
    if link is None:
        link = ExternalLink(company_id=connection.company_id, connection=connection, provider=connection.provider,
                            object_type=object_type, local_id=obj.pk)
    link.status = status
    link.match_method = method
    link.candidates = candidates
    link.last_error = ''
    if contact is not None:
        link.external_id = contact.external_id
        link.external_number = contact.name[:100]
        link.last_synced_at = timezone.now()
    try:
        with transaction.atomic():
            link.save()
    except IntegrityError:
        # Someone linked that provider contact to another record meanwhile.
        link.status, link.external_id, link.match_method = 'SUGGESTED', '', method
        link.candidates = candidates or ([_candidate(contact, method)] if contact else [])
        link.save()
    return link


def match_one(connection, object_type, obj, adapter=None):
    """Targeted match for one record (push time): provider searches by each
    identifier in order, then by name."""
    adapter = adapter or get_adapter(connection)
    me = local_identity(obj)
    pool = []
    # Name search uses the normalised key ('acme mining' for 'Acme Mining
    # (Pty) Ltd.'): provider searches are substring matches, so the shorter
    # key finds 'ACME MINING' where the full legal name wouldn't.
    for kw in ({'vat_number': me['vat']}, {'registration_number': me['reg']}, {'email': me['email']},
               {'name': me['name_key'] or me['name']}):
        val = next(iter(kw.values()))
        if not val:
            continue
        pool = adapter.find_contacts(**kw, kind=KIND_OF[object_type])
        status, method, contact, cands = decide(connection, object_type, obj, pool)
        if status != 'CREATE':
            return _save_decision(connection, object_type, obj, status, method, contact, cands)
    return _save_decision(connection, object_type, obj, 'CREATE', '', None, [])


def candidates_queryset(connection, kind):
    """Records the wizard covers: customers with issued invoices, suppliers
    with expenses, and anything already linked."""
    from core.models import Customer, ExternalLink, Supplier
    company = connection.company
    linked = ExternalLink.objects.filter(connection=connection, object_type=KINDS[kind]).values('local_id')
    if kind == 'CUSTOMER':
        from core.models import Invoice
        with_docs = Invoice.objects.filter(company=company, status__in=Invoice.ISSUED_STATUSES).values('customer_id')
        return Customer.objects.filter(company=company).filter(
            models_q(pk__in=with_docs) | models_q(pk__in=linked)).distinct()
    from core.models import Expense
    with_docs = Expense.objects.filter(company=company, supplier__isnull=False).values('supplier_id')
    return Supplier.objects.filter(company=company).filter(
        models_q(pk__in=with_docs) | models_q(pk__in=linked)).distinct()


def models_q(**kw):
    from django.db.models import Q
    return Q(**kw)


def run_matching(connection) -> dict:
    """Bulk matching for the wizard: read the provider's contacts once and
    match every customer and supplier locally."""
    adapter = get_adapter(connection)
    pools, seen = {}, 0
    for kind, object_type in KINDS.items():
        contacts = pools.get(kind)
        if contacts is None and adapter.shared_contact_list and pools:
            contacts = next(iter(pools.values()))
        if contacts is None:
            contacts = list(adapter.list_contacts(kind=kind))
            seen += len(contacts)
            pools[kind] = contacts
        for obj in candidates_queryset(connection, kind):
            link = get_link(connection, object_type, obj.pk)
            if link and (link.status == 'SKIPPED' or (link.status == 'SYNCED' and link.external_id)):
                continue
            if link and link.status == 'CREATE' and link.match_method == 'manual':
                continue   # the user chose "create new"
            status, method, contact, cands = decide(connection, object_type, obj, contacts)
            _save_decision(connection, object_type, obj, status, method, contact, cands)
    log_event(connection, 'contacts', f'Contact matching ran against {seen} contacts')
    return summary(connection)


def summary(connection) -> dict:
    from django.db.models import Count
    from core.models import ExternalLink
    rows = (ExternalLink.objects.filter(connection=connection, object_type__in=KINDS.values())
            .values('status').annotate(n=Count('id')))
    out = {'MATCHED': 0, 'SUGGESTED': 0, 'UNMATCHED': 0, 'CREATE': 0, 'SKIPPED': 0}
    for r in rows:
        key = 'MATCHED' if r['status'] == 'SYNCED' else r['status']
        if key in out:
            out[key] += r['n']
    return out


def confirm(connection, link, *, external_id=None, action=None, user=None):
    """The wizard's decision on one row."""
    from core.models import ExternalLink
    if action == 'skip':
        link.status, link.match_method, link.external_id = 'SKIPPED', 'manual', ''
        link.save()
    elif action == 'create':
        link.status, link.match_method, link.external_id = 'CREATE', 'manual', ''
        link.save()
    elif external_id:
        if ExternalLink.objects.filter(connection=connection, object_type=link.object_type,
                                       external_id=external_id).exclude(pk=link.pk).exists():
            raise PermanentError('That contact is already linked to another TruckWys record.')
        found = get_adapter(connection).find_contacts(external_id=external_id, kind=KIND_OF[link.object_type])
        if not found:
            raise PermanentError(f'That contact doesn\'t exist in {connection.get_provider_display()}.')
        link.status, link.match_method = 'SYNCED', 'manual' if link.match_method != 'name' else 'name'
        link.external_id = external_id
        link.external_number = found[0].name[:100]
        link.candidates = []
        link.last_synced_at = timezone.now()
        link.save()
    else:
        raise PermanentError('Choose a contact, "create" or "skip".')
    log_event(connection, 'contacts', f'Contact {link.status.lower()} by {getattr(user, "username", "system")}',
              object_type=link.object_type, local_id=link.local_id)
    from core.accounting.sync import requeue_blocked
    requeue_blocked(connection)
    return link


def ensure_contact(connection, object_type, obj, adapter=None) -> str:
    """Provider contact id for `obj`, matching or creating as needed. Raises
    ContactBlocked when a person must decide first."""
    adapter = adapter or get_adapter(connection)
    link = get_link(connection, object_type, obj.pk)
    if link is None or link.status in ('UNMATCHED', 'PENDING', 'ERROR'):
        link = match_one(connection, object_type, obj, adapter)
    if link.status == 'SYNCED' and link.external_id:
        return link.external_id
    if link.status == 'SKIPPED':
        raise ContactBlocked(f'{_label(obj)} is set to "don\'t sync"; link or create the contact to sync its documents.')
    if link.status == 'SUGGESTED':
        raise ContactBlocked(f'Confirm which {connection.get_provider_display()} contact {_label(obj)} is '
                             f'(Integrations → Contacts).')
    # CREATE
    me = local_identity(obj)
    prefix = 'TW-C' if object_type == 'CONTACT_CUSTOMER' else 'TW-S'
    try:
        created = adapter.upsert_contact(Contact(
            name=me['name'] or str(obj), email=getattr(obj, 'email', '') or '', vat_number=me['vat'],
            registration_number=me['reg'], reference=f'{prefix}-{obj.pk}'), kind=KIND_OF[object_type])
    except PermanentError as exc:
        # Typically "name already exists": that is a match to confirm, not an error.
        found = adapter.find_contacts(name=me['name_key'] or me['name'], kind=KIND_OF[object_type])
        cands = [_candidate(c, 'name') for c in found if remote_identity(c)['name_key'] == me['name_key']] or \
            [_candidate(c, 'name') for c in found]
        link.status, link.match_method, link.candidates = 'SUGGESTED', 'name', cands[:10]
        link.last_error = str(exc)[:2000]
        link.save()
        raise ContactBlocked(f'{connection.get_provider_display()} already has a contact named like {_label(obj)}; '
                             f'confirm the match (Integrations → Contacts).')
    link.external_id = created.external_id
    link.external_number = created.name[:100]
    link.status = 'SYNCED'
    link.match_method = link.match_method or 'created'
    link.last_synced_at = timezone.now()
    try:
        with transaction.atomic():
            link.save()
    except IntegrityError:
        raise PermanentError('The created contact is already linked to another record.')
    log_event(connection, 'create_contact', f'Created contact {created.name}', object_type=object_type,
              local_id=obj.pk, label=created.name)
    return link.external_id


def _label(obj):
    return getattr(obj, 'company_name', '') or getattr(obj, 'name', '') or str(obj)


def serialize_row(link) -> dict:
    Model = _model(link.object_type)
    obj = Model.objects.filter(pk=link.local_id).first()
    me = local_identity(obj) if obj else {}
    return {
        'id': link.pk, 'kind': 'CUSTOMER' if link.object_type == 'CONTACT_CUSTOMER' else 'SUPPLIER',
        'local_id': link.local_id, 'local_name': _label(obj) if obj else '(deleted)',
        'local_vat': me.get('vat', ''), 'local_registration': me.get('reg', ''), 'local_email': me.get('email', ''),
        'status': 'MATCHED' if link.status == 'SYNCED' else link.status,
        'method': link.match_method or None, 'external_id': link.external_id or None,
        'external_name': link.external_number or None, 'candidates': link.candidates or [],
        'last_error': link.last_error,
    }
