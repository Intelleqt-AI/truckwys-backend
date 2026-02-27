"""Django signals for webhook dispatching and activity tracking."""

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='core.Load')
def load_saved(sender, instance, created, **kwargs):
    """Fire webhook when load is created or status changes."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.serializers import LoadSerializer
    from core.models import ActivityEvent

    # Serialize the load data
    data = LoadSerializer(instance).data

    if created:
        # Fire load.created event
        dispatch_webhook('load.created', data)
        # Create activity event
        ActivityEvent.objects.create(
            event_type='load',
            title=f'New load created: {instance.load_number}',
            description=f'{instance.origin} → {instance.destination}',
            entity_id=instance.id,
            entity_type='Load',
            metadata={'load_number': instance.load_number, 'status': instance.status}
        )
    else:
        # Fire load.status_changed event
        dispatch_webhook('load.status_changed', data)
        # Create activity event for status change
        ActivityEvent.objects.create(
            event_type='load',
            title=f'Load {instance.load_number} status changed',
            description=f'Status: {instance.get_status_display()}',
            entity_id=instance.id,
            entity_type='Load',
            metadata={'load_number': instance.load_number, 'status': instance.status}
        )

        # Fire specific events for certain statuses
        if instance.status == 'DELIVERED':
            dispatch_webhook('load.delivered', data)


@receiver(post_save, sender='core.Invoice')
def invoice_saved(sender, instance, created, **kwargs):
    """Fire webhook when invoice is created or paid."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.serializers import InvoiceSerializer
    from core.models import ActivityEvent

    if created:
        # Fire invoice.created event
        data = InvoiceSerializer(instance).data
        dispatch_webhook('invoice.created', data)
        # Create activity event
        ActivityEvent.objects.create(
            event_type='invoice',
            title=f'New invoice created: {instance.invoice_number}',
            description=f'Customer: {instance.customer.name if instance.customer else "N/A"} - Amount: R{instance.amount}',
            entity_id=instance.id,
            entity_type='Invoice',
            metadata={'invoice_number': instance.invoice_number, 'status': instance.status, 'amount': str(instance.amount)}
        )
    elif instance.status == 'PAID':
        # Fire invoice.paid event (on status update to PAID)
        data = InvoiceSerializer(instance).data
        dispatch_webhook('invoice.paid', data)
        # Create activity event
        ActivityEvent.objects.create(
            event_type='invoice',
            title=f'Invoice paid: {instance.invoice_number}',
            description=f'Payment received for R{instance.amount}',
            entity_id=instance.id,
            entity_type='Invoice',
            metadata={'invoice_number': instance.invoice_number, 'status': instance.status, 'amount': str(instance.amount)}
        )


@receiver(post_save, sender='core.Quote')
def quote_saved(sender, instance, created, **kwargs):
    """Fire webhook when quote is accepted."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.models import ActivityEvent

    if created:
        # Create activity event for new quote
        ActivityEvent.objects.create(
            event_type='quote',
            title=f'New quote created: {instance.quote_number}',
            description=f'{instance.origin} → {instance.destination}',
            entity_id=instance.id,
            entity_type='Quote',
            metadata={'quote_number': instance.quote_number, 'status': instance.status}
        )

    # Only fire on status change to ACCEPTED
    if not created and instance.status == 'ACCEPTED':
        dispatch_webhook('quote.accepted', {
            'id': instance.id,
            'quote_number': instance.quote_number,
            'customer_name': instance.customer_name if hasattr(instance, 'customer_name') else None,
            'total_amount': str(instance.total_amount) if instance.total_amount else '0',
            'status': instance.status,
        })
        # Create activity event
        ActivityEvent.objects.create(
            event_type='quote',
            title=f'Quote accepted: {instance.quote_number}',
            description=f'Customer accepted quote for R{instance.total_amount}',
            entity_id=instance.id,
            entity_type='Quote',
            metadata={'quote_number': instance.quote_number, 'status': instance.status}
        )


@receiver(post_save, sender='core.AdvanceRequest')
def advance_saved(sender, instance, created, **kwargs):
    """Fire webhook when advance is approved."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.models import ActivityEvent

    if created:
        # Create activity event for new advance request
        ActivityEvent.objects.create(
            event_type='advance',
            title=f'Advance request created',
            description=f'Amount: R{instance.amount} - Invoice: {instance.invoice.invoice_number if instance.invoice else "N/A"}',
            entity_id=instance.id,
            entity_type='AdvanceRequest',
            metadata={'amount': str(instance.amount), 'status': instance.status}
        )

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
        # Create activity event
        ActivityEvent.objects.create(
            event_type='advance',
            title=f'Advance approved',
            description=f'Amount: R{instance.amount} - Net: R{instance.net_amount}',
            entity_id=instance.id,
            entity_type='AdvanceRequest',
            metadata={'amount': str(instance.amount), 'status': instance.status}
        )
    elif not created and instance.status == 'DISBURSED':
        dispatch_webhook('advance.disbursed', {
            'id': instance.id,
            'invoice_id': instance.invoice.id if instance.invoice else None,
            'invoice_number': instance.invoice.invoice_number if instance.invoice else None,
            'amount': str(instance.amount) if instance.amount else '0',
            'net_amount': str(instance.net_amount) if instance.net_amount else '0',
            'status': instance.status,
        })
        # Create activity event
        ActivityEvent.objects.create(
            event_type='advance',
            title=f'Advance disbursed',
            description=f'Funds disbursed: R{instance.net_amount}',
            entity_id=instance.id,
            entity_type='AdvanceRequest',
            metadata={'amount': str(instance.amount), 'status': instance.status}
        )
