"""Runtime for the Copilot's database tools.

Four LLM-facing handlers (query_records, propose_create, propose_update,
propose_delete) plus execute_proposal, which the confirm endpoint calls.
Every handler returns a JSON-safe dict; errors come back as {'error': str}
so the model can read them and correct course. Nothing here ever raises to
the tool loop.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db import models as dj_models
from django.db.models import Avg, Count, Max, Min, Sum
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from core.services.copilot_entities import (
    ENTITY_REGISTRY, GLOBAL_EXCLUDED_FIELDS, ToolError,
    delete_related_counts, role_can, scoped_queryset, writable_fields,
)

logger = logging.getLogger(__name__)

PROPOSAL_TTL = timedelta(minutes=30)

_ORM_OPS = {
    'eq': '{f}', 'neq': '{f}', 'lt': '{f}__lt', 'lte': '{f}__lte',
    'gt': '{f}__gt', 'gte': '{f}__gte', 'contains': '{f}__icontains',
    'in': '{f}__in', 'isnull': '{f}__isnull',
}
_AGG_FUNCS = {'count': Count, 'sum': Sum, 'avg': Avg, 'min': Min, 'max': Max}


def _json_safe(v):
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    return v


def _row(spec, obj):
    row = {f: _json_safe(getattr(obj, f, None)) for f in spec['display']}
    for name, fn in spec.get('display_extra', {}).items():
        try:
            row[name] = fn(obj)
        except Exception:
            row[name] = None
    return row


# ---------------------------------------------------------------------------
# query_records
# ---------------------------------------------------------------------------

def query_records(company, user, conversation, args):
    table = args.get('table')
    spec = ENTITY_REGISTRY.get(table)
    if spec is None or not role_can(user, table, 'read'):
        return {'error': f"You don't have access to table '{table}'."}

    qs = scoped_queryset(user, company, table)

    for flt in args.get('filters') or []:
        field, op, value = flt.get('field'), flt.get('op'), flt.get('value')
        if field not in spec['filter']:
            return {'error': f"'{field}' is not filterable on {table}. Filterable: {', '.join(spec['filter'])}"}
        if op not in _ORM_OPS:
            return {'error': f"Unknown filter op '{op}'."}
        lookup = _ORM_OPS[op].format(f=field)
        try:
            if op == 'neq':
                qs = qs.exclude(**{lookup: value})
            else:
                qs = qs.filter(**{lookup: value})
        except (ValueError, TypeError, dj_models.FieldError) as e:
            return {'error': f"Bad filter {field} {op} {value!r}: {e}"}

    search = (args.get('search') or '').strip()
    if search:
        q = dj_models.Q()
        for f in spec['search']:
            q |= dj_models.Q(**{f"{f}__icontains": search})
        qs = qs.filter(q)

    agg = args.get('aggregate')
    if agg:
        func_name = agg.get('func')
        func = _AGG_FUNCS.get(func_name)
        if func is None:
            return {'error': f"Unknown aggregate '{func_name}'."}
        field = agg.get('field') or 'id'
        if func_name != 'count' and field not in spec['agg']:
            return {'error': f"'{field}' is not aggregatable on {table}. Allowed: {', '.join(spec['agg'])}"}
        group_by = agg.get('group_by')
        if group_by:
            if group_by not in spec['filter'] and group_by not in spec['display']:
                return {'error': f"Cannot group {table} by '{group_by}'."}
            rows = list(
                qs.values(group_by).annotate(value=func(field)).order_by('-value')[:50]
            )
            return {'groups': [{**{k: _json_safe(v) for k, v in r.items()}} for r in rows]}
        value = qs.aggregate(value=func(field))['value']
        return {'value': _json_safe(value), 'func': func_name, 'field': field}

    order_by = args.get('order_by')
    if order_by:
        if order_by.lstrip('-') not in spec['order']:
            return {'error': f"Cannot order {table} by '{order_by}'. Orderable: {', '.join(spec['order'])}"}
        qs = qs.order_by(order_by)

    try:
        limit = max(1, min(int(args.get('limit') or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    total = qs.count()
    rows = [_row(spec, obj) for obj in qs[:limit]]
    return {'rows': rows, 'count': total, 'truncated': total > limit}


# ---------------------------------------------------------------------------
# propose_* — validate, persist a PENDING proposal, return a summary
# ---------------------------------------------------------------------------

def _label_for(field_name):
    return field_name.replace('_', ' ').title()


def _display_rows(spec, payload, instance=None):
    """[{label, value, old_value?}] — FKs rendered via their registry display."""
    rows = []
    fk = spec.get('fk', {})
    for field, value in payload.items():
        if field.startswith('_') or field in GLOBAL_EXCLUDED_FIELDS:
            continue
        shown = value
        if field in fk and value is not None:
            ref_table, ref_attr = fk[field]
            ref_spec = ENTITY_REGISTRY[ref_table]
            ref = ref_spec['model'].objects.filter(pk=value).first()
            if ref is not None:
                shown = f"{getattr(ref, ref_attr, value)} (#{value})"
        row = {'label': _label_for(field), 'value': _json_safe(shown)}
        if instance is not None:
            old = getattr(instance, f"{field}_id", None) if field in fk else getattr(instance, field, None)
            if field in fk and old is not None:
                ref_table, ref_attr = fk[field]
                ref = ENTITY_REGISTRY[ref_table]['model'].objects.filter(pk=old).first()
                old = f"{getattr(ref, ref_attr, old)} (#{old})" if ref else old
            row['old_value'] = _json_safe(old)
        rows.append(row)
    # Deferred-creation extras get their own rows so the card is honest.
    if payload.get('_new_customer'):
        rows.insert(0, {'label': 'Customer', 'value': f"{payload['_new_customer']} (new)"})
    if payload.get('_new_user'):
        nu = payload['_new_user']
        rows.insert(0, {'label': 'Driver Account', 'value': f"{nu['first_name']} {nu['last_name']} <{nu['email']}> (new)"})
    return rows


def _one_proposal_guard(conversation):
    from core.models import CopilotProposal
    if conversation is None:
        return None
    recent = CopilotProposal.objects.filter(
        conversation=conversation, status='PENDING',
        created_at__gte=timezone.now() - timedelta(seconds=90),
    ).first()
    return recent


def _save_proposal(company, user, conversation, table, operation, *,
                   target_id='', payload=None, display=None, warning='', analysis_summary=''):
    from core.models import CopilotProposal
    return CopilotProposal.objects.create(
        company=company, user=user, conversation=conversation,
        table=table, operation=operation, target_id=str(target_id or ''),
        payload=payload or {}, display=display or [], warning=warning[:300],
        analysis_summary=(analysis_summary or '')[:4000],
        expires_at=timezone.now() + PROPOSAL_TTL,
    )


def _filter_writable(table, fields, *, for_update=False):
    allowed = set(writable_fields(table, for_update=for_update))
    clean, dropped = {}, []
    for k, v in (fields or {}).items():
        if k in allowed:
            clean[k] = v
        else:
            dropped.append(k)
    return clean, dropped


def propose_create(company, user, conversation, args):
    table = args.get('table')
    spec = ENTITY_REGISTRY.get(table)
    if spec is None or not role_can(user, table, 'create'):
        return {'error': f"Your role does not allow creating {table}."}
    if _one_proposal_guard(conversation):
        return {'error': 'A proposal is already awaiting the user\'s confirmation — wait for it to be resolved.'}

    payload, dropped = _filter_writable(table, args.get('fields'))

    try:
        pre = spec.get('hooks', {}).get('pre_validate')
        warning = ''
        if pre:
            payload, warning = pre(company, user, payload)
        if not spec.get('hooks', {}).get('execute_create'):
            serializer = spec['serializer'](data=payload)
            if not serializer.is_valid():
                return {'error': _serializer_error_text(serializer),
                        'hint': 'Ask the user for the missing/invalid values, then propose again.'}
    except ToolError as e:
        return {'error': str(e)}

    display = _display_rows(spec, payload)
    proposal = _save_proposal(company, user, conversation, table, 'CREATE',
                              payload=payload, display=display, warning=warning)
    if dropped:
        logger.info("copilot propose_create %s dropped fields: %s", table, dropped)
    return {
        'proposal_id': proposal.id,
        'summary': f"Prepared new {spec['label']} for confirmation.",
        'needs_confirmation': True,
        'ignored_fields': dropped or None,
    }


def propose_update(company, user, conversation, args):
    table = args.get('table')
    spec = ENTITY_REGISTRY.get(table)
    if spec is None or not role_can(user, table, 'update'):
        return {'error': f"Your role does not allow updating {table}."}
    if _one_proposal_guard(conversation):
        return {'error': 'A proposal is already awaiting the user\'s confirmation — wait for it to be resolved.'}

    instance = scoped_queryset(user, company, table).filter(pk=args.get('record_id')).first()
    if instance is None:
        return {'error': f"No {spec['label']} with id {args.get('record_id')} — resolve the id via query_records first."}

    payload, dropped = _filter_writable(table, args.get('fields'), for_update=True)
    # Keep only actual changes so the card shows a real diff.
    changed = {}
    for k, v in payload.items():
        current = getattr(instance, f"{k}_id", None) if k in spec.get('fk', {}) else getattr(instance, k, None)
        if _json_safe(current) != _json_safe(v):
            changed[k] = v
    if not changed:
        return {'error': 'No changes detected — every supplied value matches the current record.'}

    serializer = spec['serializer'](instance, data=changed, partial=True)
    if not serializer.is_valid():
        return {'error': _serializer_error_text(serializer)}

    display = _display_rows(spec, changed, instance=instance)
    proposal = _save_proposal(company, user, conversation, table, 'UPDATE',
                              target_id=instance.pk, payload=changed, display=display)
    return {
        'proposal_id': proposal.id,
        'summary': f"Prepared update to {spec['label']} {getattr(instance, spec['id_display'], instance.pk)}.",
        'needs_confirmation': True,
        'ignored_fields': dropped or None,
    }


def propose_delete(company, user, conversation, args):
    table = args.get('table')
    spec = ENTITY_REGISTRY.get(table)
    if spec is None or not role_can(user, table, 'delete'):
        return {'error': f"Your role does not allow deleting {table}."}
    if _one_proposal_guard(conversation):
        return {'error': 'A proposal is already awaiting the user\'s confirmation — wait for it to be resolved.'}

    instance = scoped_queryset(user, company, table).filter(pk=args.get('record_id')).first()
    if instance is None:
        return {'error': f"No {spec['label']} with id {args.get('record_id')} — resolve the id via query_records first."}

    warning = ''
    related = delete_related_counts(instance)
    if related:
        pieces = ', '.join(f"{n} {label}" for label, n in related.items())
        warning = f"This {spec['label'].lower()} is referenced by {pieces} — deletion will be blocked."

    display = [{'label': _label_for(f), 'value': _json_safe(getattr(instance, f, None))}
               for f in spec['display']]
    proposal = _save_proposal(company, user, conversation, table, 'DELETE',
                              target_id=instance.pk, display=display, warning=warning)
    return {
        'proposal_id': proposal.id,
        'summary': f"Prepared deletion of {spec['label']} {getattr(instance, spec['id_display'], instance.pk)}.",
        'needs_confirmation': True,
        'blocked_warning': warning or None,
    }


EMAIL_DAILY_LIMIT = 20
_MIN_SUBJECT, _MAX_SUBJECT = 3, 150
_MIN_BODY, _MAX_BODY = 10, 4000


def _resolve_email_recipient(company, recipient_type, recipient_id):
    """Resolve (name, email) strictly server-side, company-scoped. Never trust
    an AI-supplied address — this is the only source of truth for who gets
    emailed. Returns (obj, name, email) with obj=None on any failure."""
    if recipient_type == 'customer':
        from core.models import Customer
        obj = Customer.objects.filter(pk=recipient_id, company=company).first()
        if obj is None:
            return None, None, None
        return obj, obj.name, obj.email
    if recipient_type == 'driver':
        from core.models import Driver
        obj = Driver.objects.filter(pk=recipient_id, company=company).select_related('user').first()
        if obj is None:
            return None, None, None
        email = obj.user.email if obj.user_id else ''
        name = (f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username) if obj.user_id else ''
        return obj, name, email
    return None, None, None


def propose_send_email(company, user, conversation, args):
    from core.services.copilot_entities import can_send_email

    if not can_send_email(user):
        return {'error': 'Your role does not allow sending emails.'}
    if _one_proposal_guard(conversation):
        return {'error': 'A proposal is already awaiting the user\'s confirmation — wait for it to be resolved.'}

    recipient_type = args.get('recipient_type')
    recipient_id = args.get('recipient_id')
    if recipient_type not in ('customer', 'driver'):
        return {'error': "recipient_type must be 'customer' or 'driver'."}

    obj, name, email = _resolve_email_recipient(company, recipient_type, recipient_id)
    if obj is None:
        return {'error': f"No {recipient_type} with id {recipient_id} in your company — "
                          "resolve the id via query_records first."}
    if not email:
        return {'error': f"This {recipient_type} has no email address on file — cannot send."}

    subject = (args.get('subject') or '').strip()
    body = (args.get('body') or '').strip()
    if not (_MIN_SUBJECT <= len(subject) <= _MAX_SUBJECT):
        return {'error': f"Subject must be {_MIN_SUBJECT}-{_MAX_SUBJECT} characters."}
    if not (_MIN_BODY <= len(body) <= _MAX_BODY):
        return {'error': f"Message must be {_MIN_BODY}-{_MAX_BODY} characters."}

    from core.models import CopilotProposal
    sent_today = CopilotProposal.objects.filter(
        user=user, table='email', status='EXECUTED',
        executed_at__gte=timezone.now() - timedelta(hours=24),
    ).count()
    if sent_today >= EMAIL_DAILY_LIMIT:
        return {'error': f'Daily email limit reached ({EMAIL_DAILY_LIMIT}/day) — ask an admin if you need more.'}

    display = [
        {'label': 'To', 'value': f"{name} <{email}>"},
        {'label': 'Subject', 'value': subject},
        {'label': 'Message', 'value': body},
    ]
    payload = {'recipient_type': recipient_type, 'recipient_id': recipient_id,
               'subject': subject, 'body': body}
    proposal = _save_proposal(
        company, user, conversation, 'email', 'SEND',
        payload=payload, display=display,
        analysis_summary=(args.get('analysis_summary') or '').strip(),
    )
    return {
        'proposal_id': proposal.id,
        'summary': f"Prepared an email to {name} for confirmation.",
        'needs_confirmation': True,
    }


TOOL_HANDLERS = {
    'query_records': query_records,
    'propose_create': propose_create,
    'propose_update': propose_update,
    'propose_delete': propose_delete,
    'propose_send_email': propose_send_email,
}


def _serializer_error_text(serializer):
    parts = []
    for field, msgs in serializer.errors.items():
        msg = msgs[0] if isinstance(msgs, (list, tuple)) else msgs
        parts.append(f"{field}: {msg}")
    return '; '.join(str(p) for p in parts) or 'invalid data'


_CONFIRM_TEXT = {'DELETE': 'Delete', 'SEND': 'Send email'}


def proposal_public(proposal) -> dict:
    """The proposal as the chat envelope / conversation-detail GET exposes it."""
    if proposal.table == 'email':
        label = 'Send Email'
        confirm_text = 'Send email'
    else:
        spec = ENTITY_REGISTRY.get(proposal.table, {})
        label = f"{proposal.operation.title()} {spec.get('label', proposal.table)}"
        confirm_text = f"{_CONFIRM_TEXT.get(proposal.operation, 'Save')} {spec.get('label', '')}".strip()
    return {
        'id': proposal.id,
        'table': proposal.table,
        'operation': proposal.operation,
        'label': label,
        'fields': proposal.display,
        'warning': proposal.warning,
        'analysis_summary': proposal.analysis_summary or None,
        'confirm_text': confirm_text,
        'status': proposal.status.lower(),
        'result': proposal.result or None,
    }


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

# Models whose post_save/post_delete signals already write AuditLog rows.
_SIGNAL_AUDITED = {'loads', 'invoices', 'vehicles', 'drivers'}


def _audit(operation, instance, user, proposal):
    if proposal.table in _SIGNAL_AUDITED:
        return
    try:
        from core.models import AuditLog
        fn = {'CREATE': AuditLog.log_create, 'UPDATE': AuditLog.log_update,
              'DELETE': AuditLog.log_delete}[operation]
        fn(instance, user=user, details={'source': 'copilot', 'proposal_id': proposal.id})
    except Exception:
        logger.exception("copilot audit log failed for proposal %s", proposal.id)


def _nav_route(spec, instance):
    route = spec.get('route')
    if not route:
        return None
    if '{invoice_id}' in route:
        return route.replace('{invoice_id}', str(getattr(instance, 'invoice_id', '') or ''))
    return route.replace('{id}', str(instance.pk))


def _execute_email_proposal(proposal, request_user, company):
    """Send a confirmed AI-composed email. Re-resolves the recipient fresh from
    the DB — never trusts the cached name/email in the proposal payload."""
    from core.services.copilot_entities import can_send_email

    if not can_send_email(request_user):
        msg = 'Your role no longer allows sending emails.'
        proposal.status = 'FAILED'
        proposal.result = {'error': msg}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': msg}

    if proposal.expires_at and proposal.expires_at < timezone.now():
        proposal.status = 'EXPIRED'
        proposal.save(update_fields=['status'])
        return False, {'error': 'This proposal has expired — ask the copilot to prepare it again.'}

    payload = proposal.payload
    obj, name, email = _resolve_email_recipient(company, payload.get('recipient_type'), payload.get('recipient_id'))
    if obj is None or not email:
        msg = 'The recipient no longer exists or has no email address.'
        proposal.status = 'FAILED'
        proposal.result = {'error': msg}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': msg}

    from core.services.email_service import send_agent_composed_email
    ok = send_agent_composed_email(email, name, payload['subject'], payload['body'], company)
    if not ok:
        msg = 'Email could not be sent — please try again shortly.'
        proposal.status = 'FAILED'
        proposal.result = {'error': msg}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': msg}

    try:
        from core.models import AuditLog
        AuditLog.log_action(
            action='EMAIL', resource_type=payload['recipient_type'].capitalize(),
            resource_id=payload['recipient_id'], user=request_user,
            details={'source': 'copilot', 'proposal_id': proposal.id, 'subject': payload['subject']},
        )
    except Exception:
        logger.exception("copilot email audit log failed for proposal %s", proposal.id)

    proposal.status = 'EXECUTED'
    proposal.executed_at = timezone.now()
    proposal.result = {'to': email}
    proposal.save(update_fields=['status', 'executed_at', 'result'])
    return True, {'result': proposal.result, 'message': f"Email sent to {name} ({email}).", 'action': None}


def execute_proposal(proposal, request_user, company):
    """Perform the confirmed write. Returns (ok, payload_for_response).

    Re-checks everything: status, expiry, ownership, company, and RBAC — the
    user's role may have changed since the proposal was created.
    """
    from core.models import CopilotProposal

    if proposal.table == 'email':
        return _execute_email_proposal(proposal, request_user, company)

    spec = ENTITY_REGISTRY.get(proposal.table)
    op_name = {'CREATE': 'create', 'UPDATE': 'update', 'DELETE': 'delete'}[proposal.operation]
    if spec is None or not role_can(request_user, proposal.table, op_name):
        msg = f"Your role no longer allows this {op_name}."
        proposal.status = 'FAILED'
        proposal.result = {'error': msg}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': msg}

    if proposal.expires_at and proposal.expires_at < timezone.now():
        proposal.status = 'EXPIRED'
        proposal.save(update_fields=['status'])
        return False, {'error': 'This proposal has expired — ask the copilot to prepare it again.'}

    hooks = spec.get('hooks', {})
    label = spec['label']
    try:
        with transaction.atomic():
            if proposal.operation == 'CREATE':
                pre_exec = hooks.get('pre_execute_create')
                if pre_exec:
                    pre_exec(company)
                exec_create = hooks.get('execute_create')
                if exec_create:
                    instance = exec_create(company, request_user, proposal.payload)
                else:
                    serializer = spec['serializer'](data=proposal.payload)
                    if not serializer.is_valid():
                        raise ToolError(_serializer_error_text(serializer))
                    save_kwargs = {}
                    model_fields = {f.name for f in spec['model']._meta.get_fields()}
                    if 'company' in model_fields:
                        save_kwargs['company'] = company
                    if 'created_by' in model_fields:
                        save_kwargs['created_by'] = request_user
                    instance = serializer.save(**save_kwargs)
                instance._request_user = request_user
                _audit('CREATE', instance, request_user, proposal)

            elif proposal.operation == 'UPDATE':
                instance = scoped_queryset(request_user, company, proposal.table).filter(
                    pk=proposal.target_id).first()
                if instance is None:
                    raise ToolError(f"The {label} no longer exists.")
                serializer = spec['serializer'](instance, data=proposal.payload, partial=True)
                if not serializer.is_valid():
                    raise ToolError(_serializer_error_text(serializer))
                instance._request_user = request_user
                instance = serializer.save()
                _audit('UPDATE', instance, request_user, proposal)

            else:  # DELETE
                instance = scoped_queryset(request_user, company, proposal.table).filter(
                    pk=proposal.target_id).first()
                if instance is None:
                    raise ToolError(f"The {label} no longer exists.")
                _audit('DELETE', instance, request_user, proposal)
                exec_delete = hooks.get('execute_delete')
                instance._request_user = request_user
                if exec_delete:
                    exec_delete(company, request_user, instance)
                else:
                    instance.delete()

    except ToolError as e:
        proposal.status = 'FAILED'
        proposal.result = {'error': str(e)}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': str(e)}
    except ProtectedError as e:
        related = ', '.join(sorted({o._meta.verbose_name_plural.lower() for o in e.protected_objects})) or 'records'
        msg = f"Cannot delete this {label.lower()}: existing {related} reference it."
        proposal.status = 'FAILED'
        proposal.result = {'error': msg}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': msg}
    except IntegrityError as e:
        msg = f"Save failed — a record with a conflicting unique value already exists. ({str(e)[:120]})"
        proposal.status = 'FAILED'
        proposal.result = {'error': msg}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': msg}
    except Exception as e:
        logger.exception("copilot execute_proposal %s failed", proposal.id)
        proposal.status = 'FAILED'
        proposal.result = {'error': f'Unexpected error: {str(e)[:200]}'}
        proposal.save(update_fields=['status', 'result'])
        return False, {'error': proposal.result['error']}

    number = getattr(instance, spec['id_display'], None) if proposal.operation != 'DELETE' else proposal.target_id
    route = _nav_route(spec, instance) if proposal.operation != 'DELETE' else None
    verbed = {'CREATE': 'Created', 'UPDATE': 'Updated', 'DELETE': 'Deleted'}[proposal.operation]
    display_name = number or (instance.pk if proposal.operation != 'DELETE' else proposal.target_id)

    proposal.status = 'EXECUTED'
    proposal.executed_at = timezone.now()
    proposal.result = {
        'id': instance.pk if proposal.operation != 'DELETE' else None,
        'number': str(number) if number else None,
        'route': route,
    }
    proposal.save(update_fields=['status', 'executed_at', 'result'])

    message = f"{verbed} {label} {display_name}."
    action = {'label': f"Open {label} {display_name}", 'route': route} if route else None
    return True, {'result': proposal.result, 'message': message, 'action': action}
