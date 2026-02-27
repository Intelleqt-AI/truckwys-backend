"""Django signals for webhook dispatching."""

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='core.Load')
def load_saved(sender, instance, created, **kwargs):
    """Fire webhook when load is created or status changes."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.serializers import LoadSerializer

    # Serialize the load data
    data = LoadSerializer(instance).data

    if created:
        # Fire load.created event
        dispatch_webhook('load.created', data)
    else:
        # Fire load.status_changed event
        dispatch_webhook('load.status_changed', data)

        # Fire specific events for certain statuses
        if instance.status == 'DELIVERED':
            dispatch_webhook('load.delivered', data)


@receiver(post_save, sender='core.Invoice')
def invoice_saved(sender, instance, created, **kwargs):
    """Fire webhook when invoice is created or paid."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.serializers import InvoiceSerializer

    if created:
        # Fire invoice.created event
        data = InvoiceSerializer(instance).data
        dispatch_webhook('invoice.created', data)
    elif instance.status == 'PAID':
        # Fire invoice.paid event (on status update to PAID)
        data = InvoiceSerializer(instance).data
        dispatch_webhook('invoice.paid', data)


@receiver(post_save, sender='core.Quote')
def quote_saved(sender, instance, created, **kwargs):
    """Fire webhook when quote is accepted."""
    from core.services.webhook_dispatcher import dispatch_webhook

    # Only fire on status change to ACCEPTED
    if not created and instance.status == 'ACCEPTED':
        dispatch_webhook('quote.accepted', {
            'id': instance.id,
            'quote_number': instance.quote_number,
            'customer_name': instance.customer_name if hasattr(instance, 'customer_name') else None,
            'total_amount': str(instance.total_amount) if instance.total_amount else '0',
            'status': instance.status,
        })


@receiver(post_save, sender='core.AdvanceRequest')
def advance_saved(sender, instance, created, **kwargs):
    """Fire webhook when advance is approved."""
    from core.services.webhook_dispatcher import dispatch_webhook

    # Only fire on status change to APPROVED
    if not created and instance.status == 'APPROVED':
        dispatch_webhook('advance.approved', {
            'id': instance.id,
            'invoice_id': instance.invoice.id if instance.invoice else None,
            'invoice_number': instance.invoice.invoice_number if instance.invoice else None,
            'amount': str(instance.amount) if instance.amount else '0',
            'net_amount': str(instance.net_amount) if instance.net_amount else '0',
            'status': instance.status,
        })
