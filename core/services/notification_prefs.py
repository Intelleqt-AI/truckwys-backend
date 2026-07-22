"""Canonical notification preference schema + the send-time gate.

Single source of truth for what the Notification Settings screen offers and
what the dispatch layer (notify_company, email senders, web push) consults.
The schema deliberately matches the frontend's — the stored JSON may contain
legacy keys from the pre-2026-07 backend defaults; get_prefs() ignores them.

Semantics: preferences gate DELIVERY (toast, browser push, email). Bell rows
(the in-app notification center) are always written for every active company
user so the history stays complete.
"""

NOTIFICATION_DEFAULTS = {
    "email": {
        "quotes": True,
        "invoices": True,
        "payments": True,
        "fleet_alerts": True,
        "weekly_reports": False,
    },
    "push": {
        "new_bookings": True,
        "payment_received": True,
        "maintenance_due": True,
        "driver_updates": False,
    },
    # SMS is visible-but-disabled in the UI; kept in the schema so stored
    # values round-trip, but no sender consults it yet.
    "sms": {
        "critical_alerts": False,
        "payment_confirmations": False,
    },
}

# event name (or prefix, via _category_for) -> per-channel category.
# Events absent from this map are uncategorized company activity: toasts show
# (client-side default-on) and no notification email is sent.
EVENT_CATEGORY = {
    "booking.created": {"push": "new_bookings"},
    "booking.assigned": {"push": "new_bookings"},
    "booking.in_transit": {"push": "new_bookings"},
    "booking.delivered": {"push": "new_bookings"},
    "booking.cancelled": {"push": "new_bookings"},
    "booking.status": {"push": "new_bookings"},
    "quote.created": {"email": "quotes"},
    "quote.sent": {"email": "quotes"},
    "quote.accepted": {"email": "quotes"},
    "quote.declined": {"email": "quotes"},
    "quote.completed": {"email": "quotes"},
    "quote.expired": {"email": "quotes"},
    "invoice.created": {"email": "invoices"},
    "invoice.auto_created": {"email": "invoices"},
    "invoice.overdue": {"email": "invoices"},
    "invoice.paid": {"email": "payments", "push": "payment_received"},
    "payment.received": {"email": "payments", "push": "payment_received"},
    "maintenance.due": {"email": "fleet_alerts", "push": "maintenance_due"},
    "driver.status_changed": {"push": "driver_updates"},
}


def get_prefs(user):
    """The user's effective preferences: canonical defaults with the user's
    stored choices layered per channel. Unknown stored keys are dropped."""
    stored = user.notification_settings or {}
    prefs = {}
    for channel, defaults in NOTIFICATION_DEFAULTS.items():
        chan_stored = stored.get(channel)
        if not isinstance(chan_stored, dict):
            chan_stored = {}
        prefs[channel] = {
            key: bool(chan_stored.get(key, default))
            for key, default in defaults.items()
        }
    return prefs


def category_for(event, channel):
    """Category key for an event on a channel, or None if unmapped."""
    return EVENT_CATEGORY.get(event or "", {}).get(channel)


def should_notify(user, channel, category):
    """Does this user want delivery on this channel for this category?

    Unmapped/None categories: email defaults to False (never email without an
    explicit category), push defaults to True (uncategorized activity toasts).
    """
    if category is None:
        return channel == "push"
    defaults = NOTIFICATION_DEFAULTS.get(channel, {})
    if category not in defaults:
        return False
    return get_prefs(user)[channel][category]
