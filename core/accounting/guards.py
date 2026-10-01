"""While an accounting system manages payments, TruckWys refuses manual ones.

A connection manages payments from the moment the cut-over date is chosen
(the initial sync adopts every receipt recorded before that into the
provider) until it is disconnected. NEEDS_REAUTH still counts: payments keep
being recorded in the provider and arrive once reconnected.

Invoices dated BEFORE the cut-over aren't part of the integration (they were
never pushed, so their payments can't flow back): they stay managed in
TruckWys, as before.
"""
from __future__ import annotations


def managing_connection(company):
    if company is None:
        return None
    from core.accounting.sync import live_connection
    conn = live_connection(company)
    if conn is None or not conn.cutover_date:
        return None
    return conn


def payments_managed_error(company, invoice=None):
    """None, or the 409 payload the API returns."""
    conn = managing_connection(company)
    if conn is None:
        return None
    # Decided by the date, every time (the cut-over can move earlier).
    if invoice is not None and invoice.issue_date and invoice.issue_date < conn.cutover_date:
        return None
    from core.accounting.registry import get_adapter
    name = conn.get_provider_display()
    url = None
    if invoice is not None:
        from core.models import ExternalLink
        link = ExternalLink.objects.filter(connection=conn, object_type='INVOICE', local_id=invoice.pk).first()
        if link and link.external_id:
            try:
                url = get_adapter(conn).web_url('INVOICE', link.external_id) or None
            except Exception:
                url = None
    return {
        'error': f'Payments are recorded in {name} while it is connected. Record this payment in {name}; '
                 'it will appear here automatically.',
        'code': 'payments_managed_by_accounting',
        'provider': conn.provider, 'provider_name': name, 'record_url': url,
    }
