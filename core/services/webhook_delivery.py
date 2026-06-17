"""Webhook delivery service for partner webhook subscriptions with retry logic."""

import json
import hmac
import hashlib
import time
import requests
from datetime import datetime
from typing import Dict, Any, Optional
from core.models import WebhookSubscription


class WebhookDeliveryService:
    """Service for delivering webhooks to partner subscriptions with retry logic."""

    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 5, 15]  # seconds (exponential backoff)

    @staticmethod
    def _generate_signature(payload_json: str, secret: str) -> str:
        """Generate HMAC-SHA256 signature for payload."""
        return 'sha256=' + hmac.new(
            secret.encode(),
            payload_json.encode(),
            hashlib.sha256
        ).hexdigest()

    @classmethod
    def deliver(
        cls,
        event_type: str,
        payload: Dict[str, Any],
        subscription: WebhookSubscription
    ) -> bool:
        """
        Deliver webhook event to a single subscription with retry logic.

        Args:
            event_type: Event type (e.g., 'invoice.created', 'load.delivered')
            payload: Event data dictionary
            subscription: WebhookSubscription instance to deliver to

        Returns:
            True if delivery succeeded, False otherwise
        """
        if not subscription.is_active:
            return False

        # Prepare full payload
        full_payload = {
            'event': event_type,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'data': payload,
        }
        payload_json = json.dumps(full_payload)

        # Generate signature
        signature = cls._generate_signature(payload_json, subscription.secret)

        # Attempt delivery with retries
        for attempt in range(cls.MAX_RETRIES):
            try:
                response = requests.post(
                    subscription.webhook_url,
                    data=payload_json,
                    headers={
                        'Content-Type': 'application/json',
                        'X-Webhook-Signature': signature,
                        'X-Webhook-Event': event_type,
                        'User-Agent': 'TruckWys-Webhook/1.0',
                    },
                    timeout=10
                )

                # Consider 2xx responses as success
                if 200 <= response.status_code < 300:
                    subscription.mark_delivery(success=True)
                    return True
                elif response.status_code >= 500:
                    # Server error - retry
                    if attempt < cls.MAX_RETRIES - 1:
                        time.sleep(cls.RETRY_DELAYS[attempt])
                        continue
                else:
                    # Client error (4xx) - don't retry
                    subscription.mark_delivery(success=False)
                    return False

            except (requests.RequestException, Exception) as e:
                # Network error or timeout - retry
                if attempt < cls.MAX_RETRIES - 1:
                    time.sleep(cls.RETRY_DELAYS[attempt])
                    continue
                else:
                    # Final attempt failed
                    subscription.mark_delivery(success=False)
                    print(f"Webhook delivery failed to {subscription.webhook_url}: {str(e)}")
                    return False

        # All retries exhausted
        subscription.mark_delivery(success=False)
        return False

    @classmethod
    def deliver_to_all(cls, event_type: str, payload: Dict[str, Any]) -> Dict[str, int]:
        """
        Deliver webhook event to all active subscriptions for this event type.

        Args:
            event_type: Event type (e.g., 'invoice.created')
            payload: Event data dictionary

        Returns:
            Dictionary with 'success' and 'failed' counts
        """
        # Find all active subscriptions for this event type. Filter membership in
        # Python so this works on SQLite too (JSONField __contains is Postgres-only).
        subscriptions = [
            s for s in WebhookSubscription.objects.filter(is_active=True)
            if event_type in (s.events or [])
        ]

        success_count = 0
        failed_count = 0

        for subscription in subscriptions:
            if cls.deliver(event_type, payload, subscription):
                success_count += 1
            else:
                failed_count += 1

        return {
            'success': success_count,
            'failed': failed_count,
            'total': success_count + failed_count
        }
