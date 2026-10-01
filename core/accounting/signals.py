"""Queue pushes when TruckWys documents change.

Cheap when nothing is connected (one indexed query per save, cached on the
instance's company for the request). Never raises into the caller: a sync
problem must not stop an invoice being issued.

  Invoice issued (left DRAFT) / voided        -> INVOICE link
  CreditNote issued / voided (MANUAL only)    -> CREDIT_NOTE link
  Expense saved or deleted (with a supplier)  -> BILL link
"""
import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _connection(company_id):
    from core.accounting.sync import live_connection
    from core.models import Company
    if not company_id:
        return None
    conn = live_connection(Company(pk=company_id))
    if conn is None or not conn.cutover_date:
        return None   # nothing syncs until the initial sync has a cut-over date
    return conn


def _queue(company_id, object_type, local_id, on_date=None, *, needs_link=False):
    try:
        conn = _connection(company_id)
        if conn is None:
            return
        from core.accounting.sync import enqueue, in_scope
        from core.models import ExternalLink
        linked = ExternalLink.objects.filter(connection=conn, object_type=object_type, local_id=local_id).exists()
        if needs_link and not linked:
            return
        if not linked and on_date is not None and not in_scope(conn, on_date):
            return
        enqueue(conn, object_type, local_id)
    except Exception:
        logger.exception('could not queue %s %s for accounting sync', object_type, local_id)


@receiver(post_save, sender='core.Invoice', dispatch_uid='accounting_invoice_saved')
def invoice_saved(sender, instance, created, **kwargs):
    if instance.status == 'DRAFT':
        return
    if instance.status == 'CANCELLED':
        _queue(instance.company_id, 'INVOICE', instance.pk, needs_link=True)
        return
    # Issued: queue once (later saves of an issued invoice are payments/status
    # changes; the invoice itself is immutable).
    try:
        conn = _connection(instance.company_id)
        if conn is None:
            return
        from core.models import ExternalLink
        if ExternalLink.objects.filter(connection=conn, object_type='INVOICE', local_id=instance.pk).exists():
            return
    except Exception:
        logger.exception('accounting signal failed')
        return
    _queue(instance.company_id, 'INVOICE', instance.pk, instance.issue_date)


@receiver(post_save, sender='core.CreditNote', dispatch_uid='accounting_credit_note_saved')
def credit_note_saved(sender, instance, created, **kwargs):
    if instance.source != 'MANUAL':
        return
    if instance.status == 'VOID':
        _queue(instance.company_id, 'CREDIT_NOTE', instance.pk, needs_link=True)
    elif created:
        _queue(instance.company_id, 'CREDIT_NOTE', instance.pk, instance.issue_date)


@receiver(post_save, sender='core.Expense', dispatch_uid='accounting_expense_saved')
def expense_saved(sender, instance, created, **kwargs):
    if instance.supplier_id is None or instance.status == 'REJECTED':
        _queue(instance.company_id, 'BILL', instance.pk, needs_link=True)
        return
    _queue(instance.company_id, 'BILL', instance.pk, instance.expense_date)


@receiver(post_delete, sender='core.Expense', dispatch_uid='accounting_expense_deleted')
def expense_deleted(sender, instance, **kwargs):
    _queue(instance.company_id, 'BILL', instance.pk, needs_link=True)
