"""Django signals for webhook dispatching, activity tracking, and audit logging."""

from django.db.models.signals import post_save, post_delete, pre_delete, pre_save
from django.dispatch import receiver
from core.formatting import format_zar


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
    # QUOTE-RULES §11: the ONE send guard. Every path that moves a saved
    # quote to SENT passes here before the row is written (and before the
    # post_save email), so a blocked quote is never sent. Raises
    # QuoteSendBlocked (a DRF 400 with structured warnings).
    if instance.pk and instance.status == 'SENT' and instance._old_status not in (None, 'SENT') \
            and not getattr(instance, '_skip_send_guard', False):
        from core.services.quote_snapshot import enforce_send_guard
        instance._send_check = enforce_send_guard(instance)


@receiver(pre_save, sender='core.Invoice')
def invoice_pre_save(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_status = sender.objects.values_list('status', flat=True).get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(pre_save, sender='core.AdvanceRequest')
def advance_pre_save(sender, instance, **kwargs):
    if instance.pk:
        try:
            instance._old_status = sender.objects.values_list('status', flat=True).get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(pre_save, sender='core.Driver')
def driver_pre_save(sender, instance, **kwargs):
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
            from core.services.notify_copy import customer_name, load_route, join_parts
            detail = join_parts(
                instance.load_number or f'Load {instance.id}',
                customer_name(instance),
                load_route(instance),
            )
            notify_company(
                getattr(instance, 'company_id', None),
                'INFO',
                '📦 New booking created',
                detail,
                link=f'/bookings/{instance.id}',
                event='booking.created',
                exclude_user_id=getattr(instance, 'created_by_id', None),
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
                from core.services.notify_copy import customer_name, load_route, join_parts
                cid = getattr(instance, 'company_id', None)
                num = instance.load_number or f'Load {instance.id}'
                route = load_route(instance)
                cust = customer_name(instance)
                # Route matters most while a job is moving (assigned/in transit);
                # customer matters most once it's decided (delivered/cancelled).
                _LOAD_STATUS_NOTIFY = {
                    'ASSIGNED':   ('booking.assigned',   '🚚 Driver assigned',      'INFO',    join_parts(num, route)),
                    'IN_TRANSIT': ('booking.in_transit', '🚛 On the way',           'INFO',    join_parts(num, route)),
                    'DELIVERED':  ('booking.delivered',  '✅ Delivered!',            'SUCCESS', join_parts(num, cust, 'delivered successfully')),
                    'CANCELLED':  ('booking.cancelled',  'Booking cancelled',      'ALERT',   join_parts(num, cust)),
                }
                if instance.status in _LOAD_STATUS_NOTIFY:
                    event, title, ntype, detail = _LOAD_STATUS_NOTIFY[instance.status]
                    notify_company(cid, ntype, title, detail, link=f'/bookings/{instance.id}', event=event,
                                    exclude_user_id=getattr(instance, '_notify_actor_id', None))
        except Exception:
            pass


def _deliver_auto_invoice(load, invoice):
    """Email an auto-raised invoice when the company has auto_email_invoices
    on, and notify the team either way. Never raises."""
    import logging
    from core.services.notify import notify_company
    log = logging.getLogger(__name__)
    from core.models import Company
    # Read fresh: the load's cached company may predate a settings change.
    auto_email = bool(invoice.company_id and Company.objects.filter(
        pk=invoice.company_id).values_list('auto_email_invoices', flat=True).first())
    email = getattr(invoice.customer, 'email', '') or ''
    amount = format_zar(invoice.total_amount, 0)
    emailed = False
    if auto_email and email:
        try:
            from core.services.invoicing import email_invoice_to_customer
            emailed = email_invoice_to_customer(invoice)
        except Exception as exc:
            log.warning('auto-email of invoice %s failed: %s', invoice.invoice_number, exc)
    try:
        if emailed:
            title, body = 'Invoice emailed on delivery', f'{invoice.invoice_number} · {amount} · sent to {email}'
        elif auto_email:
            reason = 'the customer has no email address' if not email else "the email couldn't be sent"
            title, body = 'Invoice ready to send', f'{invoice.invoice_number} · {amount} · not emailed: {reason}'
        else:
            title, body = 'Invoice ready to send', f'{invoice.invoice_number} · {amount} · review it and send it to the customer'
        notify_company(getattr(load, 'company_id', None), 'SUCCESS', title, body,
                       link=f'/finance/invoices/{invoice.id}', event='invoice.auto_created')
    except Exception as exc:
        log.warning('auto-invoice notification failed: %s', exc)


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
        # A draft until it's really emailed: marking it SENT here (as before)
        # said the customer had an invoice nobody sent them, and started the
        # overdue clock from a date they never saw.
        invoice, created = create_invoice_for_load(load, mark_sent=False)
        if not (invoice and created):
            return
    except Exception as exc:  # never break the delivery save
        import logging
        logging.getLogger(__name__).warning('auto-invoice on delivery failed: %s', exc)
        return

    # After the delivery commits: email it if the company opted in, then tell
    # the team what happened. Never on the delivery save's own path.
    from django.db import transaction
    transaction.on_commit(lambda: _deliver_auto_invoice(load, invoice))

    # 0.25% delivery take-rate — charged the same moment the invoice is
    # auto-raised. Its own service never raises, but keep this defensive too:
    # a billing hiccup must never be able to undo the delivery/invoice save.
    try:
        from core.services.delivery_fee_billing import charge_delivery_fee_for_invoice
        charge_delivery_fee_for_invoice(invoice)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('delivery fee charge failed: %s', exc)


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
    elif instance.status == 'PAID' and getattr(instance, '_old_status', None) != 'PAID':
        # Fire invoice.paid event (on status update to PAID) — guarded so a
        # later unrelated save of an already-paid invoice doesn't re-dispatch
        # the webhook or log a second "Invoice paid" activity entry.
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
            from core.services.notify_copy import customer_name, money, join_parts
            detail = join_parts(instance.invoice_number, customer_name(instance),
                                 money(getattr(instance, 'total_amount', 0)))
            notify_company(
                getattr(instance, 'company_id', None),
                'SUCCESS',
                '💰 Invoice paid',
                detail,
                link=f'/finance/invoices/{instance.id}',
                event='invoice.paid',
                exclude_user_id=getattr(instance, '_notify_actor_id', None),
            )
        except Exception:
            pass

    # Live-push + persist notification for overdue invoices. Invoice.save()
    # re-sets status='OVERDUE' on every save while the invoice remains
    # overdue (not just the save that first made it so) — without this
    # old-status guard, any later touch of an overdue invoice (a note edit, a
    # dunning pass, anything) re-fired this notification every single time.
    if not created and instance.status == 'OVERDUE' and getattr(instance, '_old_status', None) != 'OVERDUE':
        try:
            from core.services.notify import notify_company
            from core.services.notify_copy import customer_name, money, join_parts
            balance = money(getattr(instance, 'balance', None))
            detail = join_parts(
                instance.invoice_number, customer_name(instance),
                f'{balance} outstanding' if balance else '',
            )
            notify_company(
                instance.company_id,
                'ALERT',
                '⚠️ Invoice overdue',
                detail,
                link=f'/finance/invoices/{instance.id}',
                event='invoice.overdue',
            )
        except Exception:
            pass


@receiver(post_save, sender='core.Quote')
def quote_pricing_decision_superseded(sender, instance, created, update_fields=None, **kwargs):
    """Pricing analysis: a quote whose price changed without a new analysis
    (an older client, the API, the admin) no longer matches its stored
    pricing decision. Mark that decision superseded so no screen restores or
    shows its price, margin or chance (core.services.pricing_decisions)."""
    if created or (update_fields is not None and 'total_amount' not in update_fields):
        return
    import logging
    from django.db import transaction
    try:
        # Own savepoint: a failure here can't break the caller's transaction.
        with transaction.atomic():
            from core.services.pricing_decisions import supersede_if_price_changed
            supersede_if_price_changed(instance)
    except Exception:
        logging.getLogger(__name__).exception('pricing decision supersede check failed for quote %s', instance.pk)


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
            from core.services.notify_copy import customer_name, quote_route, money, join_parts
            detail = join_parts(
                instance.quote_number or f'Quote {instance.id}',
                customer_name(instance), quote_route(instance),
                money(getattr(instance, 'total_amount', None)),
            )
            notify_company(
                getattr(instance, 'company_id', None),
                'INFO',
                'New quote drafted',
                detail,
                link=f'/bookings/quotes/{instance.id}',
                event='quote.created',
                exclude_user_id=getattr(instance, 'created_by_id', None),
            )
        except Exception:
            pass

    # Notify on other status transitions
    if not created:
        try:
            old = getattr(instance, '_old_status', None)
            if old != instance.status and instance.status != 'DECLINED':
                # DECLINED is handled separately below — it surfaces the
                # customer's typed reason (Quote.rejection_reason), which none
                # of these other transitions have an equivalent of.
                from core.services.notify import notify_company
                from core.services.notify_copy import customer_name, money, join_parts
                ident = instance.quote_number or f'Quote {instance.id}'
                cust = customer_name(instance)
                amount = money(getattr(instance, 'total_amount', None))
                _QUOTE_STATUS_NOTIFY = {
                    'SENT':      ('quote.sent',      'Quote sent',        'INFO',    join_parts(ident, cust, f'{amount} — awaiting response' if amount else 'awaiting response')),
                    'COMPLETED': ('quote.completed', '✅ Quote completed', 'SUCCESS', join_parts(ident, cust, 'delivered successfully')),
                    'EXPIRED':   ('quote.expired',   '⏳ Quote expired',   'WARNING', join_parts(ident, cust, f'{amount} opportunity lost' if amount else 'opportunity lost')),
                }
                if instance.status in _QUOTE_STATUS_NOTIFY:
                    event, title, ntype, detail = _QUOTE_STATUS_NOTIFY[instance.status]
                    notify_company(getattr(instance, 'company_id', None), ntype, title, detail,
                                    link=f'/bookings/quotes/{instance.id}', event=event,
                                    exclude_user_id=getattr(instance, '_notify_actor_id', None))
        except Exception:
            pass

    # Pricing analysis (additive): remember whether the quote was ever sent.
    # SENT (or leaving SENT) -> True; DRAFT straight to a decided status with
    # nothing known -> False (a never-sent quote is not market evidence).
    # A queryset update: no signals re-fired, no other field touched.
    if created and instance.status == 'SENT' and getattr(instance, 'was_sent', None) is not True:
        # Created straight as SENT: it was sent.
        if sender.objects.filter(pk=instance.pk).update(was_sent=True):
            instance.was_sent = True
    if not created:
        try:
            old = getattr(instance, '_old_status', None)
            flag = None
            if instance.status == 'SENT' or old == 'SENT':
                flag = True
            elif old == 'DRAFT' and instance.status in ('ACCEPTED', 'DECLINED', 'IT', 'COMPLETED') \
                    and getattr(instance, 'was_sent', None) is None:
                flag = False
            if flag is not None and getattr(instance, 'was_sent', None) is not flag:
                rows = sender.objects.filter(pk=instance.pk)
                if flag is False:
                    # Never overwrite a recorded send (checked in the DB, not
                    # on a possibly stale instance).
                    rows = rows.filter(was_sent__isnull=True)
                if rows.update(was_sent=flag):
                    instance.was_sent = flag
        except Exception:
            pass

    # SENT transition — the actual customer-facing side effect (share
    # token + email), not just the in-app notification above. Fires on
    # ANY path that lands a quote on SENT — the dedicated send_to_customer
    # action, a plain status PATCH from the detail page's dropdown, or a
    # Kanban drag on the quotes board — so they all behave identically and
    # a quote is never "Sent" in the UI without actually having been sent.
    # Created straight as SENT goes the same way (it was sent): the email is
    # queued on commit, so a create the send guard rolls back never emails.
    # Created straight as SENT emails only when the send guard passed on it
    # (QuoteSerializer.create sets _send_guard_passed after enforce_send_guard,
    # inside the same transaction). An insert anywhere else (the admin "add",
    # a seed or test-data script, loaddata) never runs the guard, so it never
    # emails a customer. Fixture loads (raw) never email.
    if instance.status == 'SENT' and not kwargs.get('raw') \
            and (created or getattr(instance, '_old_status', None) != 'SENT'):
        try:
            from django.db import transaction
            from core.services.quote_share import ensure_quote_token, send_quote_to_customer_email
            ensure_quote_token(instance)

            def _send_email():
                # Only once the save is committed: a save that is rolled back
                # (e.g. QuoteSerializer's transaction when the pricing decision
                # fails) must not have emailed the customer a quote that is
                # still a draft, and the database isn't held open during SMTP.
                # Outside a transaction this runs at once, so send_to_customer
                # still reads the result straight after quote.save().
                if created and not getattr(instance, '_send_guard_passed', False):
                    return
                try:
                    email_sent, recipient = send_quote_to_customer_email(instance)
                    instance._share_email_sent = email_sent
                    instance._share_recipient = recipient
                except Exception:
                    pass

            transaction.on_commit(_send_email)
        except Exception:
            pass

    # DECLINED transition — separate block so it can surface the customer's
    # typed reason without complicating the generic dict above.
    if not created and instance.status == 'DECLINED' and getattr(instance, '_old_status', None) != 'DECLINED':
        try:
            from core.services.notify import notify_company
            from core.services.notify_copy import quote_declined_copy
            title, detail = quote_declined_copy(instance)
            notify_company(getattr(instance, 'company_id', None), 'ALERT', title, detail,
                            link=f'/bookings/quotes/{instance.id}', event='quote.declined',
                            exclude_user_id=getattr(instance, '_notify_actor_id', None))
        except Exception:
            pass

    # Only fire on the transition INTO ACCEPTED — not on every subsequent save
    # of a quote that's already accepted. Unlike the block above, this used to
    # check only `instance.status == 'ACCEPTED'` with no old-value comparison,
    # so any later save of an already-accepted quote (an edit, an autosave,
    # anything) re-fired the notification, the ActivityEvent log entry, AND
    # the 'quote.accepted' webhook dispatch — every time.
    if not created and instance.status == 'ACCEPTED' and getattr(instance, '_old_status', None) != 'ACCEPTED':
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
        # Skip if the call site that changed the status (e.g. the authenticated
        # update_status action) already sent this exact notification itself —
        # without this guard, an authenticated accept fired it twice: once here
        # (from the save signal) and once from the view's own direct call.
        if not getattr(instance, '_notify_handled', False):
            try:
                from core.services.notify import notify_company
                from core.services.notify_copy import quote_accepted_copy
                title, detail = quote_accepted_copy(instance)
                notify_company(
                    getattr(instance, 'company_id', None),
                    'SUCCESS',
                    title,
                    detail,
                    link=f'/bookings/quotes/{instance.id}',
                    event='quote.accepted',
                    exclude_user_id=getattr(instance, '_notify_actor_id', None),
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
        from core.services.notify import acting_user_id
        notify_company(
            company_id,
            'INFO',
            'New customer added',
            detail,
            link=f'/customers/{instance.id}',
            event='customer.created',
            # Never toast the user who added it (API create, copilot, quote builder).
            exclude_user_id=getattr(instance, '_notify_actor_id', None) or acting_user_id(),
        )
    except Exception:
        pass


@receiver(post_save, sender='core.RiskScore')
def risk_score_saved(sender, instance, created, **kwargs):
    """Notify the company when a risk score is calculated."""
    if not created or not instance.company_id:
        return
    from core.services.notify import notify_company
    notify_company(
        instance.company_id, 'INFO', 'Risk Score Updated',
        f"Invoice {instance.invoice.invoice_number} scored {instance.total_score} ({instance.tier})",
        link=f"/finance/invoices/{instance.invoice.id}",
        event='risk_score.updated',
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

    old_status = getattr(instance, '_old_status', None)

    # Only fire on the transition INTO APPROVED — not on every later save of
    # an already-approved advance (e.g. the "notes" re-save right after
    # advance.approve() in the approve view action would otherwise re-fire
    # this every time).
    if not created and instance.status == 'APPROVED' and old_status != 'APPROVED':
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

        # The approve() view action sends its own, better-worded notification
        # right after calling advance.approve() — skip ours so the company
        # doesn't get "Advance approved" twice for one approval.
        if not getattr(instance, '_notify_handled', False):
            from core.services.notify import notify_company
            company_id = getattr(instance.invoice, 'company_id', None) if instance.invoice else None
            inv_num = instance.invoice.invoice_number if instance.invoice else ''
            notify_company(
                company_id,
                'SUCCESS',
                'Advance approved',
                f'{format_zar(instance.net_amount, 0)} approved · {inv_num}',
                link=f'/capital/advances/{instance.id}',
                event='advance.approved',
                exclude_user_id=getattr(instance, '_notify_actor_id', None),
            )

    elif not created and instance.status == 'DISBURSED' and old_status != 'DISBURSED':
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

        # Same dedup as APPROVED above — the disburse() view action already
        # sends its own notification for this exact event.
        if not getattr(instance, '_notify_handled', False):
            from core.services.notify import notify_company
            company_id = getattr(instance.invoice, 'company_id', None) if instance.invoice else None
            inv_num = instance.invoice.invoice_number if instance.invoice else ''
            notify_company(
                company_id,
                'SUCCESS',
                'Funds disbursed',
                f'{format_zar(instance.net_amount, 0)} disbursed · {inv_num}',
                link=f'/capital/advances/{instance.id}',
                event='advance.disbursed',
                exclude_user_id=getattr(instance, '_notify_actor_id', None),
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
def driver_status_notify(sender, instance, created, **kwargs):
    """Notify the company when a driver's status changes (category: driver_updates)."""
    if created:
        return
    try:
        old = getattr(instance, '_old_status', None)
        if old and old != instance.status:
            from core.services.notify import notify_company
            name = ''
            if getattr(instance, 'user', None):
                name = (f"{instance.user.first_name} {instance.user.last_name}".strip()
                        or instance.user.username)
            detail = f"{name or 'Driver'} is now {instance.status}" + (f" (was {old})" if old else '')
            notify_company(
                instance.company_id, 'INFO', 'Driver status updated', detail,
                link=f'/fleet/drivers/{instance.id}', event='driver.status_changed',
                exclude_user_id=getattr(instance, '_notify_actor_id', None),
            )
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


# ---------------------------------------------------------------------------
# Trip economics (2026-10): refresh a load's (and its return pair's) cached
# estimate and learning actuals when anything that feeds them changes. Runs
# after commit, idempotent (core.services.trip_economics.recompute).
# ---------------------------------------------------------------------------

def _recompute_after_commit(load_ids):
    ids = [i for i in load_ids if i]
    if not ids:
        return
    from django.db import transaction

    def run():
        from core.services.trip_economics import recompute
        recompute(ids)
    transaction.on_commit(run)


def _expense_load_ids(expense):
    ids = [getattr(expense, 'load_id', None)]
    trip_id = getattr(expense, 'trip_id', None)
    if trip_id:
        from core.models import Trip
        ids.append(Trip.objects.filter(pk=trip_id).values_list('load_id', flat=True).first())
    return ids


@receiver(post_save, sender='core.Expense')
@receiver(post_delete, sender='core.Expense')
def trip_economics_expense_changed(sender, instance, **kwargs):
    _recompute_after_commit(_expense_load_ids(instance))


@receiver(post_save, sender='core.Invoice')
@receiver(post_delete, sender='core.Invoice')
def trip_economics_invoice_changed(sender, instance, **kwargs):
    _recompute_after_commit(_expense_load_ids(instance))


@receiver(post_save, sender='core.CreditNote')
def trip_economics_credit_note_changed(sender, instance, **kwargs):
    from core.models import Invoice
    inv = Invoice.objects.filter(pk=instance.invoice_id).values('load_id', 'trip__load_id').first() or {}
    _recompute_after_commit([inv.get('load_id'), inv.get('trip__load_id')])


@receiver(post_save, sender='core.Load')
def trip_economics_load_changed(sender, instance, **kwargs):
    partner = getattr(instance, 'return_of_id', None)
    if instance.status == 'CANCELLED':
        # A cancelled leg no longer brings (or sends) a truck: unlink the
        # pair (audited) so neither leg drops its empty return for it.
        from core.models import Load
        from core.services.return_loads import unlink_return
        fresh = Load.objects.filter(pk=instance.pk).first()
        if fresh is not None:
            res = unlink_return(fresh, source='cancelled')
            if res:
                partner = res[1] if res[0] == instance.pk else res[0]
    _recompute_after_commit([instance.pk, partner])


@receiver(pre_delete, sender='core.Load')
def trip_economics_load_deleted(sender, instance, **kwargs):
    # The partner leaves the pair (return_of SET NULL): refresh it after.
    from core.models import Load
    # Read the link from the database: the instance may be stale (linked or
    # unlinked since it was loaded).
    own = Load.objects.filter(pk=instance.pk).values_list('return_of_id', flat=True).first()
    partners = [own, instance.return_of_id] + list(Load.objects.filter(return_of_id=instance.pk)
                                                   .values_list('pk', flat=True))
    _recompute_after_commit(partners)
