"""Webhook dispatcher service for outbound event notifications."""

import json
import requests
from datetime import datetime
from typing import Dict, Any


def dispatch_webhook(event_type: str, data: Dict[str, Any]) -> None:
    """
    Dispatch webhook notifications for the given event type.

    Args:
        event_type: Event type string (e.g., 'load.created', 'invoice.created')
        data: Event data dictionary to send in payload
    """
    from core.models import Webhook

    # Find all active webhooks subscribed to this event
    hooks = Webhook.objects.filter(active=True, events__contains=event_type)

    if not hooks.exists():
        return

    # Prepare payload
    payload = {
        'event': event_type,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'data': data,
    }
    payload_json = json.dumps(payload)

    # Fire webhooks
    for hook in hooks:
        try:
            # Generate signature
            signature = hook.sign_payload(payload_json)

            # Send POST request
            response = requests.post(
                hook.url,
                data=payload_json,
                headers={
                    'Content-Type': 'application/json',
                    'X-Truckwys-Signature': signature,
                    'X-Truckwys-Event': event_type,
                    'User-Agent': 'Truckwys-Webhook/1.0',
                },
                timeout=10
            )

            # Update webhook state based on response
            if response.status_code < 400:
                hook.failure_count = 0
                hook.last_fired_at = datetime.utcnow()
            else:
                hook.failure_count += 1
                if hook.failure_count >= 10:
                    hook.active = False

            hook.save(update_fields=['failure_count', 'last_fired_at', 'active'])

        except Exception as e:
            # Handle connection errors, timeouts, etc.
            hook.failure_count += 1
            if hook.failure_count >= 10:
                hook.active = False
            hook.save(update_fields=['failure_count', 'active'])
            print(f"Webhook dispatch error for {hook.url}: {str(e)}")
