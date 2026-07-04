"""Django signals for webhook dispatching, activity tracking, and audit logging."""

from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver


# ---------------------------------------------------------------------------
# pre_save: capture old status so post_save can detect transitions
# ---------------------------------------------------------------------------

@receiver(pre_save, sender='core.Load')
def load_pre_save(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_status = sender.objects.values_list('status', flat=True).get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(pre_save, sender='core.Quote')
def quote_pre_save(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_status = sender.objects.values_list('status', flat=True).get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(pre_save, sender='core.Invoice')
def invoice_pre_save(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_status = sender.objects.values_list('status', flat=True).get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


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
            company=instance.company,
            metadata={'load_number': instance.load_number, 'status': instance.status}
        )

        # Notify the whole company (persist + live WebSocket push), with a valid
        # deep-link to the bookings detail route.
        try:
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
        except Exception:
            pass
    else:
        # Fire load.status_changed event
        dispatch_webhook('load.status_changed', data)
        try:
            ActivityEvent.objects.create(
                event_type='load',
                title=f'Load {instance.load_number} status changed',
                description=f'Status: {instance.get_status_display()}',
                entity_id=instance.id,
                entity_type='Load',
                company=instance.company,
                metadata={'load_number': instance.load_number, 'status': instance.status}
            )
        except Exception:
            pass

        # Fire specific events for certain statuses
        if instance.status == 'DELIVERED':
            dispatch_webhook('load.delivered', data)
            _auto_invoice_on_delivery(instance)
            # Stamp actual delivery time (used for on-time rate computation)
            if not instance.actual_delivered_at:
                try:
                    from django.utils import timezone
                    from core.models import Load
                    Load.objects.filter(pk=instance.pk).update(actual_delivered_at=timezone.now())
                except Exception:
                    pass

        # Recompute vehicle + driver scores whenever a load is completed
        if instance.status in ('DELIVERED', 'INVOICED'):
            try:
                from core.tasks import compute_vehicle_scores, compute_driver_scores
                if instance.vehicle_id:
                    compute_vehicle_scores(instance.vehicle_id)
                if instance.driver_id:
                    compute_driver_scores(instance.driver_id)
            except Exception:
                pass  # never block the load save

        # Live notify on status transitions
        try:
            old = getattr(instance, '_old_status', None)
            if old != instance.status:
                from core.services.notify import notify_company
                cid = getattr(instance, 'company_id', None)
                num = instance.load_number or f'Load {instance.id}'
                cust = instance.customer.name if getattr(instance, 'customer', None) else ''
                detail = f'{num} · {cust}' if cust else num
                _LOAD_STATUS_NOTIFY = {
                    'ASSIGNED':   ('booking.assigned',   'Booking assigned',    'INFO'),
                    'IN_TRANSIT': ('booking.in_transit',  'Booking in transit',  'INFO'),
                    'DELIVERED':  ('booking.delivered',   'Booking delivered',   'SUCCESS'),
                    'CANCELLED':  ('booking.cancelled',   'Booking cancelled',   'ALERT'),
                }
                if instance.status in _LOAD_STATUS_NOTIFY:
                    event, title, ntype = _LOAD_STATUS_NOTIFY[instance.status]
                    notify_company(cid, ntype, title, detail, link=f'/bookings/{instance.id}', event=event)
        except Exception:
            pass


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
    from core.models import ActivityEvent

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
            company=getattr(instance, 'company', None),
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
            company=getattr(instance, 'company', None),
            metadata={'invoice_number': instance.invoice_number, 'status': instance.status, 'amount': str(amount)}
        )

    # Live notify on PAID transition
    if not created and instance.status == 'PAID' and getattr(instance, '_old_status', None) != 'PAID':
        try:
            from core.services.notify import notify_company
            amount = getattr(instance, 'total_amount', 0)
            cust = instance.customer.name if getattr(instance, 'customer', None) else ''
            detail = instance.invoice_number
            if cust:
                detail += f' · {cust}'
            if amount:
                detail += f' · R{float(amount):,.0f}'
            notify_company(
                getattr(instance, 'company_id', None),
                'SUCCESS',
                'Invoice paid',
                detail,
                link=f'/finance/invoices/{instance.id}',
                event='invoice.paid',
            )
        except Exception:
            pass

    # Live-push + persist notification for overdue invoices
    if not created and instance.status == 'OVERDUE':
        try:
            from core.services.notify import notify_company
            cust_name = instance.customer.name if getattr(instance, 'customer', None) else ''
            detail = instance.invoice_number
            if cust_name:
                detail += f' · {cust_name}'
            if getattr(instance, 'balance', None):
                detail += f' · R{float(instance.balance):,.0f}'
            notify_company(
                instance.company_id,
                'ALERT',
                'Invoice overdue',
                detail,
                link=f'/finance/invoices/{instance.id}',
                event='invoice.overdue',
            )
        except Exception:
            pass


@receiver(post_save, sender='core.Quote')
def quote_saved(sender, instance, created, **kwargs):
    """Fire webhook when quote is accepted."""
    from core.services.webhook_dispatcher import dispatch_webhook
    from core.models import ActivityEvent

    if created:
        try:
            origin = getattr(instance, 'origin', getattr(instance, 'pickup_city', 'N/A'))
            destination = getattr(instance, 'destination', getattr(instance, 'delivery_city', 'N/A'))
            ActivityEvent.objects.create(
                event_type='quote',
                title=f'New quote created: {instance.quote_number}',
                description=f'{origin} → {destination}',
                entity_id=instance.id,
                entity_type='Quote',
                company=getattr(instance, 'company', None),
                metadata={'quote_number': instance.quote_number, 'status': instance.status}
            )
        except Exception:
            pass
        try:
            from core.services.notify import notify_company
            cust = getattr(instance.customer, 'name', '') if getattr(instance, 'customer', None) else ''
            detail = instance.quote_number or f'Quote {instance.id}'
            if cust:
                detail += f' · {cust}'
            if getattr(instance, 'total_amount', None):
                detail += f' · R{float(instance.total_amount):,.0f}'
            notify_company(
                getattr(instance, 'company_id', None),
                'INFO',
                'New quote created',
                detail,
                link=f'/quotes/{instance.id}',
                event='quote.created',
            )
        except Exception:
            pass

    # Notify on other status transitions
    if not created:
        try:
            old = getattr(instance, '_old_status', None)
            if old != instance.status:
                from core.services.notify import notify_company
                cid = getattr(instance, 'company_id', None)
                cust = getattr(instance.customer, 'name', '') if getattr(instance, 'customer', None) else ''
                detail = instance.quote_number or f'Quote {instance.id}'
                if cust:
                    detail += f' · {cust}'
                _QUOTE_STATUS_NOTIFY = {
                    'SENT':      ('quote.sent',      'Quote sent to customer', 'INFO'),
                    'DECLINED':  ('quote.declined',  'Quote declined',         'ALERT'),
                    'COMPLETED': ('quote.completed', 'Quote completed',        'SUCCESS'),
                }
                if instance.status in _QUOTE_STATUS_NOTIFY:
                    event, title, ntype = _QUOTE_STATUS_NOTIFY[instance.status]
                    notify_company(cid, ntype, title, detail, link=f'/quotes/{instance.id}', event=event)
        except Exception:
            pass

    # Only fire on status change to ACCEPTED
    if not created and instance.status == 'ACCEPTED':
        try:
            dispatch_webhook('quote.accepted', {
                'id': instance.id,
                'quote_number': instance.quote_number,
                'customer_name': instance.customer_name if hasattr(instance, 'customer_name') else None,
                'total_amount': str(instance.total_amount) if instance.total_amount else '0',
                'status': instance.status,
            })
        except Exception:
            pass
        try:
            ActivityEvent.objects.create(
                event_type='quote',
                title=f'Quote accepted: {instance.quote_number}',
                description=f'Customer accepted quote for R{instance.total_amount}',
                entity_id=instance.id,
                entity_type='Quote',
                company=getattr(instance, 'company', None),
                metadata={'quote_number': instance.quote_number, 'status': instance.status}
            )
        except Exception:
            pass
        try:
            from core.services.notify import notify_company
            cust = getattr(instance.customer, 'name', '') if getattr(instance, 'customer', None) else ''
            detail = instance.quote_number or f'Quote {instance.id}'
            if cust:
                detail += f' · {cust}'
            if instance.total_amount:
                detail += f' · R{float(instance.total_amount):,.0f}'
            notify_company(
                getattr(instance, 'company_id', None),
                'SUCCESS',
                'Quote accepted',
                detail,
                link=f'/quotes/{instance.id}',
                event='quote.accepted',
            )
        except Exception:
            pass


@receiver(post_save, sender='core.Customer')
def customer_saved(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from core.services.notify import notify_company
        company_id = getattr(instance, 'company_id', None)
        detail = instance.name or f'Customer {instance.id}'
        if getattr(instance, 'email', None):
            detail += f' · {instance.email}'
        notify_company(
            company_id,
            'INFO',
            'New customer added',
            detail,
            link=f'/customers/{instance.id}',
            event='customer.created',
        )
    except Exception:
        pass


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
    from core.models import ActivityEvent

    if created:
        # Create activity event for new advance request
        ActivityEvent.objects.create(
            event_type='advance',
            title=f'Advance request created',
            description=f'Amount: R{instance.amount} - Invoice: {instance.invoice.invoice_number if instance.invoice else "N/A"}',
            entity_id=instance.id,
            entity_type='AdvanceRequest',
            company=getattr(instance, 'company', None) or getattr(instance.invoice, 'company', None) if instance.invoice else None,
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
            company=getattr(instance, 'company', None) or getattr(instance.invoice, 'company', None) if instance.invoice else None,
            metadata={'amount': str(instance.amount), 'status': instance.status}
        )

        # Live-push + persist notification for approved advance
        from core.services.notify import notify_company
        company_id = getattr(instance.invoice, 'company_id', None) if instance.invoice else None
        inv_num = instance.invoice.invoice_number if instance.invoice else ''
        notify_company(
            company_id,
            'SUCCESS',
            'Advance approved',
            f'R{float(instance.net_amount):,.0f} approved · {inv_num}',
            link=f'/capital/advances/{instance.id}',
            event='advance.approved',
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
            company=getattr(instance, 'company', None) or getattr(instance.invoice, 'company', None) if instance.invoice else None,
            metadata={'amount': str(instance.amount), 'status': instance.status}
        )

        # Live-push + persist notification for disbursed advance
        from core.services.notify import notify_company
        company_id = getattr(instance.invoice, 'company_id', None) if instance.invoice else None
        inv_num = instance.invoice.invoice_number if instance.invoice else ''
        notify_company(
            company_id,
            'SUCCESS',
            'Funds disbursed',
            f'R{float(instance.net_amount):,.0f} disbursed · {inv_num}',
            link=f'/capital/advances/{instance.id}',
            event='advance.disbursed',
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
def vehicle_scores_on_save(sender, instance, created, **kwargs):
    """Recompute scores whenever a vehicle record is saved (maintenance dates, fuel etc. may have changed)."""
    try:
        from core.tasks import compute_vehicle_scores
        compute_vehicle_scores(instance.pk)
    except Exception:
        pass  # never block the vehicle save


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
def driver_scores_on_save(sender, instance, created, **kwargs):
    """Recompute driver scores when violations/accidents/experience are updated."""
    try:
        from core.tasks import compute_driver_scores
        compute_driver_scores(instance.pk)
    except Exception:
        pass


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
