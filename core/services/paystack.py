"""
Paystack billing integration — replaces PayFast (dropped per team decision:
Paystack's local-bank-transfer support covers South African B2B payment
behaviour just as well, and its API is materially simpler to integrate).

Spec confirmed against Paystack's official docs repo (github.com/PaystackHQ/documentation,
recurring-payments/charging-returning-customers.md) and cross-checked against
common third-party integration guides for the webhook signature scheme,
since the hosted docs site is a JS app that doesn't expose raw text.

Auth is a plain `Authorization: Bearer <secret_key>` header on every request —
no per-request signature to build (unlike PayFast). Sandbox vs live is just
which secret key is configured (sk_test_... vs sk_live_...), not a different
host or query param.

Three primitives cover the whole product:
  - initialize_transaction — start the first (card-capturing) checkout
  - verify_transaction      — confirm it succeeded + pull out the authorization_code
  - charge_authorization    — charge any later amount against that authorization,
                               used for BOTH the flat monthly fee and the 0.25%
                               delivery take-rate (no separate Plan/Subscription
                               objects — see delivery_fee_billing.py / subscription_billing.py)
"""
import hashlib
import hmac
import logging
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

PAYSTACK_API_BASE = 'https://api.paystack.co'

# The one flat monthly plan (replaces PayFast's truck-count tiers).
MONTHLY_FEE = Decimal('4499.00')
MONTHLY_FEE_ITEM_NAME = 'TruckWys Fleet'


def _headers() -> dict:
    secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    return {'Authorization': f'Bearer {secret_key}', 'Content-Type': 'application/json'}


def _amount_to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal('1')))


def _request(method: str, path: str, **kwargs) -> dict:
    """Shared request/response handling. Never raises.

    Returns {'success': bool, 'data': dict|None, 'error': str|None, 'raw': dict|str|None}.
    Paystack always wraps its JSON body as {status: bool, message: str, data: {...}} —
    `status` (not the HTTP status code) is the real success signal.
    """
    url = f'{PAYSTACK_API_BASE}{path}'
    try:
        resp = requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text

        if isinstance(payload, dict) and payload.get('status') is True:
            return {'success': True, 'data': payload.get('data'), 'error': None, 'raw': payload}

        message = payload.get('message') if isinstance(payload, dict) else str(payload)
        logger.warning("Paystack %s %s failed: status=%s body=%r", method, path, resp.status_code, payload)
        return {'success': False, 'data': None, 'error': message or 'Unknown error', 'raw': payload}
    except requests.RequestException as e:
        logger.error("Paystack %s %s request failed: %s", method, path, e)
        return {'success': False, 'data': None, 'error': str(e), 'raw': None}


def initialize_transaction(email: str, amount: Decimal, callback_url: str, metadata: dict = None) -> dict:
    """Start a checkout — the customer is redirected to `data.authorization_url`
    to enter their card. This first charge both bills them AND captures a
    reusable authorization_code (see verify_transaction) for every future
    charge — there's no separate "just add a card" step.
    """
    body = {
        'email': email,
        'amount': _amount_to_cents(amount),
        'currency': 'ZAR',
        'callback_url': callback_url,
        'metadata': metadata or {},
    }
    return _request('POST', '/transaction/initialize', json=body)


def verify_transaction(reference: str) -> dict:
    """Confirm a transaction succeeded. On success, `data['authorization']`
    contains `authorization_code` (+ `reusable`, `last4`, `card_type`, `bank`,
    `signature`) and `data['customer']['customer_code']` — everything needed
    to charge this company again later.
    """
    return _request('GET', f'/transaction/verify/{reference}')


# Paystack error codes meaning the stored authorization itself is bad, not
# that this particular charge failed — no amount of retrying fixes them.
_DEAD_AUTHORIZATION_CODES = {'invalid_authorization_code'}


def _is_dead_authorization(payload) -> bool:
    return isinstance(payload, dict) and payload.get('code') in _DEAD_AUTHORIZATION_CODES


def charge_authorization(authorization_code: str, email: str, amount: Decimal, metadata: dict = None) -> dict:
    """Charge an arbitrary amount against an existing authorization — the
    card-on-file captured by initialize_transaction/verify_transaction.

    `email` must be the exact email the authorization was created with;
    Paystack rejects the charge otherwise. Never raises.
    """
    if not authorization_code:
        return {'success': False, 'data': None, 'error': 'No Paystack authorization on file', 'raw': None}

    body = {
        'authorization_code': authorization_code,
        'email': email,
        'amount': _amount_to_cents(amount),
        'currency': 'ZAR',
        'metadata': metadata or {},
    }
    result = _request('POST', '/transaction/charge_authorization', json=body)
    if not result['success']:
        if _is_dead_authorization(result.get('raw')):
            # Distinguish "this card was declined today" from "this token will
            # never work again". Retrying the latter is pointless and, hammered
            # daily against a live key, looks like card-testing to Paystack.
            # Callers should clear the stored authorization instead of retrying.
            result['dead_authorization'] = True
            logger.error(
                'Paystack authorization is permanently invalid (%s) — caller should '
                'clear the stored token and ask the customer to re-add their card',
                result.get('error'),
            )
        return result

    # A 2xx `status: true` envelope can still describe a declined charge —
    # the actual charge outcome is data.status ("success" | "failed" | ...).
    charge_status = (result['data'] or {}).get('status')
    if charge_status != 'success':
        gateway_response = (result['data'] or {}).get('gateway_response') or charge_status or 'Charge not successful'
        logger.warning("Paystack charge_authorization did not succeed: %s", gateway_response)
        return {'success': False, 'data': result['data'], 'error': gateway_response, 'raw': result['raw']}
    return result


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """Paystack signs every webhook body with HMAC-SHA512 of the raw request
    body, using the secret key, in the `x-paystack-signature` header."""
    if not signature:
        return False
    secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    expected = hmac.new(secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)
