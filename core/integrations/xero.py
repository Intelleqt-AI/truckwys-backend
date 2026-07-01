"""
Xero Integration Module
Handles OAuth 2.0 authentication and API operations with Xero accounting software.
"""
import re
import requests
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode, quote
from django.conf import settings
from django.utils import timezone
from core.models import Company, Invoice, Customer


class XeroClient:
    """
    Xero API client for OAuth 2.0 and accounting operations.

    Handles:
    - OAuth authorization code flow
    - Token management (access/refresh)
    - Invoice creation and syncing
    - Payment syncing
    - Contact (customer) syncing
    """

    # Xero OAuth endpoints
    AUTHORIZATION_URL = "https://login.xero.com/identity/connect/authorize"
    TOKEN_URL = "https://identity.xero.com/connect/token"
    CONNECTIONS_URL = "https://api.xero.com/connections"
    API_BASE_URL = "https://api.xero.com/api.xro/2.0"

    def __init__(self, company: Company):
        """
        Initialize Xero client for a specific company.

        Args:
            company: Company instance to manage Xero integration for
        """
        self.company = company

        # Xero app credentials come from the environment (settings bridges .env).
        # Drop XERO_CLIENT_ID / XERO_CLIENT_SECRET into .env to take the integration live.
        self.client_id = getattr(settings, 'XERO_CLIENT_ID', '')
        self.client_secret = getattr(settings, 'XERO_CLIENT_SECRET', '')
        self.redirect_uri = getattr(settings, 'XERO_REDIRECT_URI', 'http://localhost:8000/api/v1/integrations/xero/callback/')

    @property
    def is_configured(self) -> bool:
        """True once a real Xero app's client id + secret are present in the env."""
        return bool(self.client_id and self.client_secret)

    def get_authorization_url(self, state: Optional[str] = None) -> str:
        """
        Generate Xero OAuth authorization URL.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL to redirect user to
        """
        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            # Granular scopes. Xero apps created on/after 2026-03-02 have NO access to the
            # broad `accounting.transactions` scope — requesting it returns `invalid_scope`.
            # Use the granular equivalents the integration actually calls: invoices (push),
            # payments (reconcile), contacts (customer sync); offline_access = refresh token.
            'scope': 'offline_access accounting.contacts accounting.invoices accounting.payments',
            'state': state or '',
        }
        # Encode spaces in `scope` as %20, NOT `+`. Xero's identity server does not treat
        # `+` as a space in the scope param, so quote_plus's `+` yields `invalid_scope`.
        return f"{self.AUTHORIZATION_URL}?{urlencode(params, quote_via=quote)}"

    def handle_callback(self, code: str) -> Dict[str, Any]:
        """
        Exchange authorization code for access/refresh tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Token response data

        Raises:
            requests.HTTPError: If token exchange fails
        """
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.redirect_uri,
        }

        response = requests.post(
            self.TOKEN_URL,
            data=data,
            auth=(self.client_id, self.client_secret),
        )
        response.raise_for_status()

        token_data = response.json()

        # Get tenant ID (organization)
        tenant_id = self._get_tenant_id(token_data['access_token'])

        # Store tokens encrypted at rest (decrypted only in-memory when used).
        from core.utils.crypto import encrypt_secret
        self.company.xero_access_token = encrypt_secret(token_data['access_token'])
        self.company.xero_refresh_token = encrypt_secret(token_data['refresh_token'])
        self.company.xero_tenant_id = tenant_id
        self.company.xero_connected_at = timezone.now()
        self.company.xero_token_expires_at = timezone.now() + timedelta(seconds=token_data.get('expires_in', 1800))
        self.company.save()

        return token_data

    def _get_tenant_id(self, access_token: str) -> str:
        """
        Get Xero tenant/organization ID.

        Args:
            access_token: Valid Xero access token

        Returns:
            Tenant ID
        """
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }

        response = requests.get(self.CONNECTIONS_URL, headers=headers)
        response.raise_for_status()

        connections = response.json()
        if connections:
            return connections[0]['tenantId']

        raise ValueError("No Xero organizations found")

    def refresh_token(self) -> Dict[str, Any]:
        """
        Refresh expired access token using refresh token.

        Returns:
            New token data

        Raises:
            ValueError: If no refresh token available
            requests.HTTPError: If token refresh fails
        """
        if not self.company.xero_refresh_token:
            raise ValueError("No refresh token available")

        from core.utils.crypto import encrypt_secret, decrypt_secret
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': decrypt_secret(self.company.xero_refresh_token),
        }

        response = requests.post(
            self.TOKEN_URL,
            data=data,
            auth=(self.client_id, self.client_secret),
        )
        response.raise_for_status()

        token_data = response.json()

        # Update stored tokens (encrypted at rest)
        self.company.xero_access_token = encrypt_secret(token_data['access_token'])
        self.company.xero_refresh_token = encrypt_secret(token_data['refresh_token'])
        self.company.xero_token_expires_at = timezone.now() + timedelta(seconds=token_data.get('expires_in', 1800))
        self.company.save()

        return token_data

    def _get_valid_token(self) -> str:
        """
        Get valid access token, refreshing if necessary.

        Returns:
            Valid access token
        """
        # Check if token is expired or about to expire (5 min buffer)
        if self.company.xero_token_expires_at:
            expires_soon = timezone.now() + timedelta(minutes=5)
            if self.company.xero_token_expires_at <= expires_soon:
                self.refresh_token()

        from core.utils.crypto import decrypt_secret
        return decrypt_secret(self.company.xero_access_token)

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Make authenticated API request to Xero.

        Args:
            method: HTTP method (GET, POST, PUT, etc.)
            endpoint: API endpoint (e.g., '/Invoices')
            data: Request payload

        Returns:
            Response data
        """
        token = self._get_valid_token()

        headers = {
            'Authorization': f'Bearer {token}',
            'Xero-tenant-id': self.company.xero_tenant_id,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        url = f"{self.API_BASE_URL}{endpoint}"

        response = requests.request(
            method,
            url,
            headers=headers,
            json=data,
        )
        response.raise_for_status()

        return response.json()

    def _sales_tax_type(self) -> Optional[str]:
        """
        Pick a revenue tax type that's valid in the connected Xero org.

        Xero tax codes are region-specific — South Africa's 'OUTPUT2' (15% VAT) does
        not exist in, e.g., a Bangladesh org, so a hard-coded code triggers a Xero
        ValidationException. Instead we read the org's own tax rates and use the
        highest-rate ACTIVE tax that can apply to revenue (its standard sales tax/VAT).
        Cached per client. Returns None if undeterminable, in which case push_invoice
        omits TaxType and Xero falls back to the line account's default tax rate.
        """
        cached = getattr(self, '_sales_tax_type_cache', False)
        if cached is not False:
            return cached
        tax_type = None
        try:
            data = self._make_request('GET', '/TaxRates')
            revenue = [r for r in data.get('TaxRates', [])
                       if r.get('Status') == 'ACTIVE' and r.get('CanApplyToRevenue')]
            revenue.sort(key=lambda r: r.get('EffectiveRate') or 0, reverse=True)
            if revenue:
                tax_type = revenue[0].get('TaxType')
        except Exception:
            tax_type = None
        self._sales_tax_type_cache = tax_type
        return tax_type

    def push_invoice(self, invoice: Invoice) -> Dict[str, Any]:
        """
        Create or update invoice in Xero.

        Args:
            invoice: TruckWys Invoice instance

        Returns:
            Xero invoice response
        """
        # Build Xero invoice payload
        xero_invoice = {
            'Type': 'ACCREC',  # Accounts Receivable (sales invoice)
            'Contact': {
                # NOTE: customer.company is the tenant FK (a Company object, not JSON-
                # serializable). The business name string is customer.company_name.
                'Name': invoice.customer.company_name or invoice.customer.name,
            },
            'LineItems': [],
            'Date': invoice.issue_date.strftime('%Y-%m-%d'),
            'DueDate': invoice.due_date.strftime('%Y-%m-%d'),
            'InvoiceNumber': invoice.invoice_number,
            'Reference': f'TruckWys Invoice {invoice.invoice_number}',
            'Status': 'AUTHORISED',  # Approved and ready for payment
        }

        # Region-agnostic tax: use a tax type valid in THIS org (not a hard-coded SA
        # code). None => omit TaxType so Xero applies the account's default rate.
        tax_type = self._sales_tax_type()

        def _line(description, quantity, unit_amount):
            li = {
                'Description': description,
                'Quantity': quantity,
                'UnitAmount': unit_amount,
                'AccountCode': '200',  # Sales
            }
            if tax_type:
                li['TaxType'] = tax_type
            return li

        # Add line items
        if invoice.line_items:
            for item in invoice.line_items:
                xero_invoice['LineItems'].append(_line(
                    item.get('description', ''),
                    item.get('quantity', 1),
                    float(item.get('unit_price', 0)),
                ))
        else:
            # Fallback if no line items
            xero_invoice['LineItems'].append(_line(
                f'Transportation services - Invoice {invoice.invoice_number}',
                1,
                float(invoice.subtotal),
            ))

        # Create invoice in Xero
        response = self._make_request('POST', '/Invoices', {'Invoices': [xero_invoice]})

        return response

    def _parse_xero_date(self, value):
        """
        Parse a Xero date into a datetime.date.

        Xero's Accounting API returns dates in Microsoft JSON format, e.g.
        '/Date(1782000000000+0000)/' — NOT ISO 'YYYY-MM-DD'. Slicing [:10] yielded
        '/Date(178' and broke Payment.payment_date. Falls back to ISO, then today.
        """
        s = str(value or '')
        m = re.search(r'/Date\((-?\d+)', s)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt_timezone.utc).date()
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        except ValueError:
            return timezone.now().date()

    def sync_payments(self) -> List[Dict[str, Any]]:
        """
        Pull payments from Xero and reconcile them against TruckWys invoices.

        For each Xero payment that maps to one of this company's invoices we record
        a Payment row (idempotently, keyed on the Xero PaymentID) and recompute the
        invoice's paid_amount from the sum of its payments — so the invoice balance
        and status (PARTIALLY_PAID / PAID) reflect Xero reality and the billing
        short-pay agent stops chasing money that's actually arrived.

        Returns:
            List of per-payment reconciliation results.
        """
        from decimal import Decimal
        from django.db.models import Sum
        from core.models import Payment

        # Get payments from Xero (last 90 days). Xero's `where` filter needs
        # DateTime(yyyy,mm,dd) with COMMAS — a hyphenated DateTime(2026-04-02) makes
        # the Xero API itself return HTTP 500.
        from_date = (timezone.now() - timedelta(days=90)).strftime('%Y,%m,%d')
        endpoint = f'/Payments?where=Date>=DateTime({from_date})'

        response = self._make_request('GET', endpoint)

        synced_payments: List[Dict[str, Any]] = []

        for xero_payment in response.get('Payments', []):
            # Xero marks reversed payments DELETED — skip them.
            if (xero_payment.get('Status') or '').upper() == 'DELETED':
                continue

            invoice_number = (xero_payment.get('Invoice') or {}).get('InvoiceNumber')
            if not invoice_number:
                continue

            amount = xero_payment.get('Amount') or 0
            payment_date = self._parse_xero_date(xero_payment.get('Date'))
            date_str = payment_date.isoformat()
            xero_id = xero_payment.get('PaymentID') or ''

            # Scope strictly to THIS company's invoices (multi-tenant safe).
            try:
                invoice = Invoice.objects.get(
                    invoice_number=invoice_number, company=self.company
                )
            except Invoice.DoesNotExist:
                synced_payments.append({'invoice_number': invoice_number, 'status': 'not_found'})
                continue
            except Invoice.MultipleObjectsReturned:
                invoice = Invoice.objects.filter(
                    invoice_number=invoice_number, company=self.company
                ).first()

            # Payment FK to customer is non-null — can't record without one.
            if not invoice.customer:
                synced_payments.append({'invoice_number': invoice_number, 'status': 'no_customer'})
                continue

            # Idempotency: never double-record the same Xero payment.
            ref = f'XERO:{xero_id}' if xero_id else f'XERO:{invoice_number}:{date_str}:{amount}'
            if Payment.objects.filter(invoice=invoice, reference_number=ref).exists():
                synced_payments.append({
                    'invoice_number': invoice_number,
                    'amount': float(amount),
                    'status': 'already_synced',
                })
                continue

            Payment.objects.create(
                company=self.company,
                payment_number=self._unique_payment_number(),
                invoice=invoice,
                customer=invoice.customer,
                amount=Decimal(str(amount)),
                payment_date=payment_date,
                payment_method='EFT',
                reference_number=ref,
                notes='Imported from Xero',
            )

            # Recompute paid_amount from all payments; invoice.save() recalcs balance + status.
            total_paid = invoice.payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
            invoice.paid_amount = total_paid
            invoice.save()

            synced_payments.append({
                'invoice_number': invoice_number,
                'amount': float(amount),
                'date': date_str,
                'status': 'recorded',
            })

        return synced_payments

    def _unique_payment_number(self) -> str:
        """Generate a collision-free payment number for a Xero-imported payment."""
        import random
        from core.models import Payment
        ts = timezone.now().strftime('%Y%m%d')
        num = f'XPAY-{ts}-{random.randint(1000, 9999)}'
        while Payment.objects.filter(payment_number=num).exists():
            num = f'XPAY-{ts}-{random.randint(1000, 9999)}'
        return num

    def sync_contacts(self) -> Dict[str, Any]:
        """
        Sync Xero contacts with TruckWys customers.

        Returns:
            Sync statistics
        """
        response = self._make_request('GET', '/Contacts')

        stats = {
            'total': 0,
            'created': 0,
            'updated': 0,
            'skipped': 0,
        }

        for xero_contact in response.get('Contacts', []):
            stats['total'] += 1

            # Skip if no email
            email = xero_contact.get('EmailAddress')
            if not email:
                stats['skipped'] += 1
                continue

            # Get or create customer
            customer, created = Customer.objects.get_or_create(
                email=email,
                defaults={
                    'name': xero_contact.get('Name', ''),
                    'company': xero_contact.get('Name', ''),
                    'phone': xero_contact.get('Phones', [{}])[0].get('PhoneNumber', ''),
                    'address': xero_contact.get('Addresses', [{}])[0].get('AddressLine1', ''),
                    'city': xero_contact.get('Addresses', [{}])[0].get('City', ''),
                    'state': xero_contact.get('Addresses', [{}])[0].get('Region', ''),
                    'zip_code': xero_contact.get('Addresses', [{}])[0].get('PostalCode', ''),
                }
            )

            if created:
                stats['created'] += 1
            else:
                stats['updated'] += 1

        return stats

    def disconnect(self) -> None:
        """
        Disconnect Xero integration by clearing stored tokens.
        """
        self.company.xero_access_token = None
        self.company.xero_refresh_token = None
        self.company.xero_tenant_id = None
        self.company.xero_connected_at = None
        self.company.xero_token_expires_at = None
        self.company.save()

    @property
    def is_connected(self) -> bool:
        """Check if Xero is connected and tokens are valid."""
        return bool(
            self.company.xero_access_token and
            self.company.xero_refresh_token and
            self.company.xero_tenant_id
        )
