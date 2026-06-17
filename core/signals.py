"""Django signals for webhook dispatching, activity tracking, and audit logging."""

from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver


@receiver(post_save, sender='core.Load')
def load_saved(sender, instance, created, **kwargs):
    """Fire webhook when load is created or status changes."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.serializers import LoadSerializer
    from core.models import ActivityEvent, Notification, User

    # Serialize the load data
    data = LoadSerializer(instance).data

    if created:
        # Fire load.created event
        dispatch_webhook('load.created', data)
        # Create activity event
        ActivityEvent.objects.create(
            event_type='load',
            title=f'New load created: {instance.load_number}',
            description=f'{instance.pickup_city} → {instance.delivery_city}',
            entity_id=instance.id,
            entity_type='Load',
            metadata={'load_number': instance.load_number, 'status': instance.status}
        )

        # Notify the whole company (persist + live WebSocket push), with a valid
        # deep-link to the bookings detail route.
        from core.services.notify import notify_company
        cust = instance.customer.name if getattr(instance, 'customer', None) else ''
        route = (f'{instance.pickup_city} → {instance.delivery_city}'
                 if getattr(instance, 'pickup_city', None) else '')
        detail = f"{instance.load_number or ('Load ' + str(instance.id))}"
        if cust:
            detail += f' · {cust}'
        elif route:
            detail += f' · {route}'
        notify_company(
            getattr(instance, 'company_id', None),
            'INFO',
            'New booking created',
            detail,
            link=f'/bookings/{instance.id}',
            event='booking.created',
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
            _auto_invoice_on_delivery(instance)


def _auto_invoice_on_delivery(load):
    """Delivered → raise the invoice automatically and surface fast-pay.

    This is the spine of the carrier-finance flow: the moment a load is
    delivered, its receivable exists and becomes advance-eligible — no manual
    'convert to invoice' click. Guarded by AUTO_INVOICE_ON_DELIVERY and wrapped
    so a failure here can never block the load save.
    """
    from django.conf import settings
    if not getattr(settings, 'AUTO_INVOICE_ON_DELIVERY', True):
        return
    try:
        from core.services.invoicing import create_invoice_for_load
        invoice, created = create_invoice_for_load(load, mark_sent=True)
        if not (invoice and created):
            return
        from core.services.notify import notify_company
        notify_company(
            getattr(load, 'company_id', None),
            'SUCCESS',
            'Invoice auto-raised on delivery',
            f'{invoice.invoice_number} · R{float(invoice.total_amount):,.0f} · ready for fast-pay',
            link=f'/finance/invoices/{invoice.id}',
            event='invoice.auto_created',
        )
    except Exception as exc:  # never break the delivery save
        import logging
        logging.getLogger(__name__).warning('auto-invoice on delivery failed: %s', exc)


@receiver(post_save, sender='core.Invoice')
def invoice_saved(sender, instance, created, **kwargs):
    """Fire webhook when invoice is created or paid."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.serializers import InvoiceSerializer
    from core.models import ActivityEvent, Notification, User

    if created:
        # Fire invoice.created event
        data = InvoiceSerializer(instance).data
        dispatch_webhook('invoice.created', data)
        # Create activity event
        amount = getattr(instance, 'total_amount', getattr(instance, 'amount', 0))
        ActivityEvent.objects.create(
            event_type='invoice',
            title=f'New invoice created: {instance.invoice_number}',
            description=f'Customer: {instance.customer.name if instance.customer else "N/A"} - Amount: R{amount}',
            entity_id=instance.id,
            entity_type='Invoice',
            metadata={'invoice_number': instance.invoice_number, 'status': instance.status, 'amount': str(amount)}
        )
    elif instance.status == 'PAID':
        # Fire invoice.paid event (on status update to PAID)
        data = InvoiceSerializer(instance).data
        dispatch_webhook('invoice.paid', data)
        # Create activity event
        amount = getattr(instance, 'total_amount', getattr(instance, 'amount', 0))
        ActivityEvent.objects.create(
            event_type='invoice',
            title=f'Invoice paid: {instance.invoice_number}',
            description=f'Payment received for R{amount}',
            entity_id=instance.id,
            entity_type='Invoice',
            metadata={'invoice_number': instance.invoice_number, 'status': instance.status, 'amount': str(amount)}
        )

    # Create notification for overdue invoices
    if not created and instance.status == 'OVERDUE':
        # Check if notification already exists to avoid duplicates
        if not Notification.objects.filter(
            title="Invoice Overdue",
            message__contains=instance.invoice_number
        ).exists():
            # Get company admin user
            user = None
            if instance.company:
                user = User.objects.filter(company=instance.company, role='ADMIN', status='ACTIVE').first()

            if user:
                Notification.objects.create(
                    user=user,
                    type='ALERT',
                    title="Invoice Overdue",
                    message=f"Invoice {instance.invoice_number} for {instance.customer.name} is overdue (R{instance.balance})",
                    link=f"/invoices/{instance.id}"
                )


@receiver(post_save, sender='core.Quote')
def quote_saved(sender, instance, created, **kwargs):
    """Fire webhook when quote is accepted."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.models import ActivityEvent

    if created:
        # Create activity event for new quote
        origin = getattr(instance, 'origin', getattr(instance, 'pickup_city', 'N/A'))
        destination = getattr(instance, 'destination', getattr(instance, 'delivery_city', 'N/A'))
        ActivityEvent.objects.create(
            event_type='quote',
            title=f'New quote created: {instance.quote_number}',
            description=f'{origin} → {destination}',
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


@receiver(post_save, sender='core.RiskScore')
def risk_score_saved(sender, instance, created, **kwargs):
    """Create notification when risk score is calculated."""
    from core.models import Notification, User

    if created:
        # Create notification for new risk score
        user = None
        if instance.company:
            user = User.objects.filter(company=instance.company, role='ADMIN', status='ACTIVE').first()

        if user:
            Notification.objects.create(
                user=user,
                type='INFO',
                title="Risk Score Updated",
                message=f"Invoice {instance.invoice.invoice_number} scored {instance.total_score} ({instance.tier})",
                link=f"/invoices/{instance.invoice.id}"
            )


@receiver(post_save, sender='core.AdvanceRequest')
def advance_saved(sender, instance, created, **kwargs):
    """Fire webhook when advance is approved."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.models import ActivityEvent, Notification, User

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

        # Create notification for approved advance
        user = None
        if instance.invoice and instance.invoice.company:
            user = User.objects.filter(company=instance.invoice.company, role='ADMIN', status='ACTIVE').first()

        if user:
            Notification.objects.create(
                user=user,
                type='SUCCESS',
                title="Advance Approved",
                message=f"Advance of R{instance.net_amount} approved for {instance.invoice.invoice_number}",
                link=f"/capital/advances/{instance.id}"
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

        # Create notification for disbursed advance
        user = None
        if instance.invoice and instance.invoice.company:
            user = User.objects.filter(company=instance.invoice.company, role='ADMIN', status='ACTIVE').first()

        if user:
            Notification.objects.create(
                user=user,
                type='SUCCESS',
                title="Funds Disbursed",
                message=f"R{instance.net_amount} disbursed for {instance.invoice.invoice_number}",
                link=f"/capital/advances/{instance.id}"
            )


# ============================================================================
# AUDIT LOGGING SIGNALS
# ============================================================================

@receiver(post_save, sender='core.Load')
def audit_load_save(sender, instance, created, **kwargs):
    """Log Load creation and updates to audit log."""
    from core.models import AuditLog

    user = getattr(instance, 'created_by', None) or getattr(instance, '_request_user', None)

    if created:
        AuditLog.log_create(instance, user=user, details={
            'load_number': instance.load_number,
            'status': instance.status,
        })
    else:
        AuditLog.log_update(instance, user=user, details={
            'load_number': instance.load_number,
            'status': instance.status,
        })


@receiver(post_delete, sender='core.Load')
def audit_load_delete(sender, instance, **kwargs):
    """Log Load deletion to audit log."""
    from core.models import AuditLog

    user = getattr(instance, '_request_user', None)
    AuditLog.log_delete(instance, user=user, details={
        'load_number': instance.load_number,
    })


@receiver(post_save, sender='core.Invoice')
def audit_invoice_save(sender, instance, created, **kwargs):
    """Log Invoice creation and updates to audit log."""
    from core.models import AuditLog

    user = getattr(instance, 'created_by', None) or getattr(instance, '_request_user', None)

    if created:
        AuditLog.log_create(instance, user=user, details={
            'invoice_number': instance.invoice_number,
            'status': instance.status,
            'amount': str(getattr(instance, 'total_amount', 0)),
        })
    else:
        AuditLog.log_update(instance, user=user, details={
            'invoice_number': instance.invoice_number,
            'status': instance.status,
            'amount': str(getattr(instance, 'total_amount', 0)),
        })


@receiver(post_delete, sender='core.Invoice')
def audit_invoice_delete(sender, instance, **kwargs):
    """Log Invoice deletion to audit log."""
    from core.models import AuditLog

    user = getattr(instance, '_request_user', None)
    AuditLog.log_delete(instance, user=user, details={
        'invoice_number': instance.invoice_number,
    })


@receiver(post_save, sender='core.AdvanceRequest')
def audit_advance_save(sender, instance, created, **kwargs):
    """Log AdvanceRequest creation and updates to audit log."""
    from core.models import AuditLog

    user = getattr(instance, 'created_by', None) or getattr(instance, '_request_user', None)

    if created:
        AuditLog.log_create(instance, user=user, details={
            'amount': str(instance.amount),
            'status': instance.status,
        })
    else:
        AuditLog.log_update(instance, user=user, details={
            'amount': str(instance.amount),
            'status': instance.status,
        })


@receiver(post_delete, sender='core.AdvanceRequest')
def audit_advance_delete(sender, instance, **kwargs):
    """Log AdvanceRequest deletion to audit log."""
    from core.models import AuditLog

    user = getattr(instance, '_request_user', None)
    AuditLog.log_delete(instance, user=user, details={
        'amount': str(instance.amount),
    })


@receiver(post_save, sender='core.Vehicle')
def audit_vehicle_save(sender, instance, created, **kwargs):
    """Log Vehicle creation and updates to audit log."""
    from core.models import AuditLog

    user = getattr(instance, '_request_user', None)

    if created:
        AuditLog.log_create(instance, user=user, details={
            'plate': instance.plate,
            'vin': instance.vin,
            'status': instance.status,
        })
    else:
        AuditLog.log_update(instance, user=user, details={
            'plate': instance.plate,
            'status': instance.status,
        })


@receiver(post_delete, sender='core.Vehicle')
def audit_vehicle_delete(sender, instance, **kwargs):
    """Log Vehicle deletion to audit log."""
    from core.models import AuditLog

    user = getattr(instance, '_request_user', None)
    AuditLog.log_delete(instance, user=user, details={
        'plate': instance.plate,
        'vin': instance.vin,
    })


@receiver(post_save, sender='core.Driver')
def audit_driver_save(sender, instance, created, **kwargs):
    """Log Driver creation and updates to audit log."""
    from core.models import AuditLog

    user = getattr(instance, '_request_user', None)

    if created:
        AuditLog.log_create(instance, user=user, details={
            'driver_name': getattr(instance, 'driver_name', ''),
            'driver_id': getattr(instance, 'driver_id', ''),
        })
    else:
        AuditLog.log_update(instance, user=user, details={
            'driver_name': getattr(instance, 'driver_name', ''),
        })


@receiver(post_delete, sender='core.Driver')
def audit_driver_delete(sender, instance, **kwargs):
    """Log Driver deletion to audit log."""
    from core.models import AuditLog

    user = getattr(instance, '_request_user', None)
    AuditLog.log_delete(instance, user=user, details={
        'driver_name': getattr(instance, 'driver_name', ''),
        'driver_id': getattr(instance, 'driver_id', ''),
    })
