"""Small read shapes shared by serializers and views."""
from __future__ import annotations


def accounting_sync(obj, object_type, context=None):
    """The `accounting_sync` block on invoices / credit notes, or None."""
    from core.models import AccountingConnection, ExternalLink
    company_id = getattr(obj, 'company_id', None)
    if not company_id or not getattr(obj, 'pk', None):
        return None
    cache = context.setdefault('_accounting_conn', {}) if isinstance(context, dict) else {}
    if company_id not in cache:
        cache[company_id] = (AccountingConnection.objects
                             .filter(company_id=company_id, status__in=('ACTIVE', 'NEEDS_REAUTH'))
                             .order_by('-connected_at').first())
    conn = cache[company_id]
    if conn is None:
        return None
    link = ExternalLink.objects.filter(connection=conn, object_type=object_type, local_id=obj.pk).first()
    if link is None:
        return None
    from core.accounting.registry import get_adapter
    try:
        url = get_adapter(conn).web_url(object_type, link.external_id) if link.external_id else ''
    except Exception:
        url = ''
    return {
        'provider': conn.provider, 'provider_name': conn.get_provider_display(),
        'status': link.status, 'external_number': link.external_number or None,
        'url': url or None, 'last_error': link.last_error, 'last_synced_at': link.last_synced_at,
    }
