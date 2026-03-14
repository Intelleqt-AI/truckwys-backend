"""
PayFast Billing Integration Service
Handles subscription creation, ITN validation, and signature generation.
PayFast docs: https://developers.payfast.co.za/docs
"""
import hashlib
import urllib.parse
import uuid
import logging
import socket
import requests
from decimal import Decimal
from django.conf import settings

logger = logging.getLogger(__name__)

PLAN_PRICING = {
    'pro': {
        'item_name': 'TruckWys Pro',
        'amount': Decimal('4999.00'),
        'frequency': 3,
        'cycles': 0,
    },
}


def _get_payfast_url():
    """Return sandbox or live PayFast process URL."""
    if getattr(settings, 'PAYFAST_SANDBOX', True):
        return 'https://sandbox.payfast.co.za/eng/process'
    return 'https://www.payfast.co.za/eng/process'


def _generate_signature(data_dict, passphrase=None):
    """
    Generate PayFast MD5 signature.
    1. Build URL-encoded param string (in insertion order, skip blanks + 'signature')
    2. Append passphrase if provided
    3. Return MD5 hex digest
    """
    filtered = {k: v for k, v in data_dict.items() if v not in (None, '') and k != 'signature'}
    param_string = urllib.parse.urlencode(filtered)
    if passphrase:
        param_string += f'&passphrase={urllib.parse.quote_plus(passphrase)}'
    return hashlib.md5(param_string.encode()).hexdigest()


def build_payment_data(plan, company_id, user_email, first_name='', last_name='',
                       notify_url='', return_url='', cancel_url=''):
    """
    Build form data dict + PayFast URL for a subscription checkout.
    Returns {'payfast_url': str, 'form_data': dict}.
    """
    if plan not in PLAN_PRICING:
        raise ValueError(f"Unknown plan: {plan}. Must be one of {list(PLAN_PRICING.keys())}")

    plan_info = PLAN_PRICING[plan]
    merchant_id = getattr(settings, 'PAYFAST_MERCHANT_ID', '')
    merchant_key = getattr(settings, 'PAYFAST_MERCHANT_KEY', '')
    passphrase = getattr(settings, 'PAYFAST_PASSPHRASE', '')
    m_payment_id = str(uuid.uuid4())

    form_data = {
        # Merchant
        'merchant_id': merchant_id,
        'merchant_key': merchant_key,
        # URLs
        'return_url': return_url or getattr(settings, 'PAYFAST_RETURN_URL', ''),
        'cancel_url': cancel_url or getattr(settings, 'PAYFAST_CANCEL_URL', ''),
        'notify_url': notify_url or getattr(settings, 'PAYFAST_NOTIFY_URL', ''),
        # Buyer
        'name_first': first_name,
        'name_last': last_name,
        'email_address': user_email,
        # Transaction
        'm_payment_id': m_payment_id,
        'amount': str(plan_info['amount']),
        'item_name': plan_info['item_name'],
        'item_description': f"{plan_info['item_name']} Monthly Subscription",
        # Custom fields (used in ITN to identify company + plan)
        'custom_str1': str(company_id),
        'custom_str2': plan,
        # Subscription
        'subscription_type': '1',
        'frequency': str(plan_info['frequency']),
        'cycles': str(plan_info['cycles']),
    }

    form_data['signature'] = _generate_signature(form_data, passphrase)

    return {
        'payfast_url': _get_payfast_url(),
        'form_data': form_data,
    }


def validate_itn(post_data, source_ip=''):
    """
    Validate an incoming ITN callback.
    1. Verify signature
    2. Verify source IP (production only)
    Returns True if valid.
    """
    passphrase = getattr(settings, 'PAYFAST_PASSPHRASE', '')

    # 1. Signature check
    received_sig = post_data.get('signature', '')
    expected_sig = _generate_signature(post_data, passphrase)
    if received_sig != expected_sig:
        logger.warning("PayFast ITN signature mismatch")
        return False

    # 2. Source IP check (skip in sandbox)
    if not getattr(settings, 'PAYFAST_SANDBOX', True):
        valid_hosts = [
            'www.payfast.co.za',
            'sandbox.payfast.co.za',
            'w1w.payfast.co.za',
            'w2w.payfast.co.za',
        ]
        ip_valid = False
        for host in valid_hosts:
            try:
                if source_ip == socket.gethostbyname(host):
                    ip_valid = True
                    break
            except socket.gaierror:
                continue
        if not ip_valid:
            logger.warning(f"PayFast ITN source IP invalid: {source_ip}")
            return False

    return True


def confirm_payment_with_payfast(post_data):
    """
    Server-to-server confirmation with PayFast.
    Returns True if PayFast responds with 'VALID'.
    """
    if getattr(settings, 'PAYFAST_SANDBOX', True):
        url = 'https://sandbox.payfast.co.za/eng/query/validate'
    else:
        url = 'https://www.payfast.co.za/eng/query/validate'

    try:
        resp = requests.post(url, data=post_data, timeout=30)
        return resp.text.strip() == 'VALID'
    except requests.RequestException as e:
        logger.error(f"PayFast confirmation failed: {e}")
        return False
