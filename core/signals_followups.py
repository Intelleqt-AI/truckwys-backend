"""Signals for the quote follow-up features (kept apart from core/signals.py).

- Company created -> QuoteAutomationSettings (fuel clause ON, no prompt).
- Company pricing inputs changed by a save -> pricing_setup marks them set.
- Quote moves into SENT -> a new follow-up cycle (sent_at) and the fuel
  clause stamped as it went out. Runs on commit, after the serializer's
  pricing snapshot, from a fresh read of the quote.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='core.Company')
def company_automation_settings(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return
    try:
        from core.models import QuoteAutomationSettings
        with transaction.atomic():
            QuoteAutomationSettings.objects.get_or_create(company=instance)
    except Exception:
        logger.exception('creating quote automation settings for company %s failed', instance.pk)


@receiver(pre_save, sender='core.Company')
def company_pricing_inputs_changed(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not instance.pk:
        return
    from core.services.quote_automation import COMPANY_FIELD_TO_KEY
    fields = list(COMPANY_FIELD_TO_KEY)
    if update_fields is not None:
        fields = [f for f in fields if f in update_fields]
        if not fields:
            return
    try:
        old = sender.objects.filter(pk=instance.pk).values(*fields).first()
    except Exception:
        return
    if not old:
        return
    changed = []
    for f in fields:
        new = getattr(instance, f, None)
        before = old.get(f)
        try:
            same = (before == new) or (before is not None and new is not None and float(before) == float(new))
        except (TypeError, ValueError):
            same = before == new
        if not same:
            changed.append(COMPANY_FIELD_TO_KEY[f])
    if changed:
        instance._pricing_inputs_changed = changed


@receiver(post_save, sender='core.Company')
def company_pricing_inputs_record(sender, instance, created, raw=False, **kwargs):
    changed = getattr(instance, '_pricing_inputs_changed', None)
    if created or raw or not changed:
        return
    instance._pricing_inputs_changed = None
    from core.services.quote_automation import record_pricing_changes
    record_pricing_changes(instance.pk, changed)


@receiver(post_save, sender='core.Quote')
def quote_follow_up_cycle(sender, instance, created, raw=False, **kwargs):
    if raw or instance.status != 'SENT':
        return
    if not created and getattr(instance, '_old_status', None) == 'SENT':
        return
    quote_id = instance.pk

    def _on_commit():
        try:
            from core.models import Quote
            from core.services.fuel_surcharge import stamp_clause
            from core.services.quote_followups import record_sent
            q = Quote.objects.select_related('company').filter(pk=quote_id).first()
            if q is None or q.status != 'SENT':
                return
            record_sent(q)
            if q.company_id:
                stamp_clause(q)
        except Exception:
            logger.exception('quote %s: follow-up cycle on send failed', quote_id)

    transaction.on_commit(_on_commit)
