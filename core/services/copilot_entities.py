"""Entity registry for the Copilot personal-agent tools.

One spec per business table the Copilot can touch. Everything the tool layer
needs is declared here: role permissions, guided-entry questions, queryable
fields, FK resolution hints, navigation routes, and per-entity hooks for the
handful of tables with bespoke create/delete logic.

The registry drives three things:
  * build_tool_schemas(user)    — the OpenAI function-calling tool definitions,
                                  with table enums filtered to the user's role
  * build_schema_reference(user)— a compact per-table field reference appended
                                  to the system prompt
  * the runtime handlers in copilot_tools.py

Ops per role are encoded as a compact string: r=read c=create u=update d=delete.
"""
import random
import uuid

from django.utils import timezone

from core.models import (
    Customer, Driver, Vehicle, VehicleType, Quote, Load,
    Invoice, Payment, Expense, Settlement,
)
from core.serializers import (
    CustomerSerializer, DriverSerializer, VehicleSerializer, VehicleTypeSerializer,
    QuoteSerializer, LoadSerializer, InvoiceSerializer, PaymentSerializer,
    ExpenseSerializer, SettlementSerializer,
)


class ToolError(Exception):
    """Friendly, LLM-readable tool failure (returned as {'error': str})."""


# ---------------------------------------------------------------------------
# Helpers used by hooks
# ---------------------------------------------------------------------------

def _unique_number(model, field, prefix):
    """PREFIX-YYYYMMDD-XXXX, unique for the given model field."""
    ts = timezone.now().strftime('%Y%m%d')
    for _ in range(25):
        num = f"{prefix}-{ts}-{random.randint(1000, 9999)}"
        if not model.objects.filter(**{field: num}).exists():
            return num
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:6]}"


def _resolve_or_create_driver_user(company, user, payload):
    """Driver.user is a required FK: resolve an existing account by email or
    create a DRIVER-role user. Returns (payload, warning)."""
    from django.contrib.auth import get_user_model
    User = get_user_model()

    if payload.get('user'):
        return payload, ''

    email = (payload.pop('email', '') or '').strip().lower()
    first_name = (payload.pop('first_name', '') or '').strip()
    last_name = (payload.pop('last_name', '') or '').strip()
    if not email:
        raise ToolError(
            "Driver accounts need an email address (a login user is created for the driver). "
            "Ask the user for the driver's email."
        )

    existing = User.objects.filter(email__iexact=email, company=company).first()
    if existing:
        if Driver.objects.filter(user=existing).exists():
            raise ToolError(f"{email} already has a driver profile.")
        payload['user'] = existing.id
        return payload, f"Will link to the existing account {email}."

    if User.objects.filter(email__iexact=email).exists():
        raise ToolError(
            f"An account with {email} already exists in another company — use a different email."
        )

    # Deferred: the User row is only created at execute time (after confirmation)
    payload['_new_user'] = {'email': email, 'first_name': first_name, 'last_name': last_name}
    return payload, f"A login account ({email}, role DRIVER) will be created for this driver."


def _driver_execute_create(company, user, payload):
    """Create the deferred User (if any) then the Driver via the serializer."""
    from django.contrib.auth import get_user_model
    User = get_user_model()

    payload = dict(payload)
    new_user = payload.pop('_new_user', None)
    if new_user:
        if User.objects.filter(email__iexact=new_user['email']).exists():
            raise ToolError(f"An account with {new_user['email']} was created in the meantime — link it explicitly.")
        account = User.objects.create_user(
            username=new_user['email'], email=new_user['email'],
            first_name=new_user['first_name'], last_name=new_user['last_name'],
            password=None,
        )
        account.role = 'DRIVER'
        account.company = company
        account.set_unusable_password()
        account.save()
        payload['user'] = account.id

    serializer = DriverSerializer(data=payload)
    if not serializer.is_valid():
        raise ToolError(_serializer_errors(serializer))
    return serializer.save(company=company)


def _quote_pre_validate(company, user, payload):
    """Reuse the proven quote_agent guards: price from the user, customer by
    name (auto-created at execute), sensible defaults."""
    from core.services.quote_agent import _to_decimal, MAX_DECIMAL
    from datetime import date, timedelta

    payload = dict(payload)
    warning = ''

    price = _to_decimal(payload.get('total_amount') or payload.get('base_rate') or payload.get('price_zar'))
    if price is None or price <= 0:
        raise ToolError("A valid price (given by the user) is required — ask what price to quote.")
    if price > MAX_DECIMAL:
        raise ToolError("Price exceeds the maximum the system can store (R99,999,999.99).")
    payload['base_rate'] = str(payload.get('base_rate') or price)
    payload['total_amount'] = str(price)
    payload.pop('price_zar', None)

    if not payload.get('valid_until'):
        payload['valid_until'] = (date.today() + timedelta(days=30)).isoformat()
    payload.setdefault('status', 'DRAFT')

    # Customer may arrive as an id or a name; names resolve here, unknown names
    # defer creation to execute (after the user confirms).
    name = (payload.pop('customer_name', '') or '').strip()
    if name and not payload.get('customer'):
        existing = (Customer.objects.filter(company=company, name__iexact=name).order_by('id').first()
                    or Customer.objects.filter(company=company, name__icontains=name).order_by('id').first())
        if existing:
            payload['customer'] = existing.id
        else:
            payload['_new_customer'] = name
            warning = f"A new customer '{name}' will be created."
    if not payload.get('customer') and not payload.get('_new_customer'):
        raise ToolError("A customer (existing id or a name) is required for the quote.")
    return payload, warning


def _quote_execute_create(company, user, payload):
    from core.services.quote_agent import _resolve_customer, _gen_quote_number
    from decimal import Decimal

    payload = dict(payload)
    new_customer = payload.pop('_new_customer', None)
    if new_customer:
        customer, _created = _resolve_customer(company, new_customer)
        payload['customer'] = customer.id
    if not payload.get('quote_number'):
        payload['quote_number'] = _gen_quote_number()

    serializer = QuoteSerializer(data=payload)
    if not serializer.is_valid():
        raise ToolError(_serializer_errors(serializer))
    quote = serializer.save(company=company, created_by=user)

    # Snapshot the fuel price like QuoteViewSet.perform_create does.
    try:
        from core.services.fuel_price import current_fuel_price
        quote.fuel_price_at_creation = Decimal(str(current_fuel_price()))
        quote.save(update_fields=['fuel_price_at_creation'])
    except Exception:
        pass
    return quote


def _payment_pre_validate(company, user, payload):
    """Best-effort early checks; the authoritative locked check runs at execute."""
    from decimal import Decimal, InvalidOperation

    payload = dict(payload)
    try:
        amount = Decimal(str(payload.get('amount', '0')))
    except (InvalidOperation, ValueError, TypeError):
        raise ToolError("Payment amount is not a valid number.")
    if amount <= 0:
        raise ToolError("Payment amount must be greater than zero.")
    invoice = Invoice.objects.filter(id=payload.get('invoice'), company=company).first()
    if invoice is None:
        raise ToolError("Invoice not found — look it up with query_records('invoices', ...) first.")
    if amount > invoice.balance:
        raise ToolError(f"Payment of R {amount} exceeds the invoice balance (R {invoice.balance}).")
    payload.setdefault('payment_date', timezone.now().date().isoformat())
    return payload, ''


def _payment_execute_create(company, user, payload):
    from core.services.payments import record_payment, PaymentError
    try:
        serializer = record_payment(company, user, dict(payload))
    except PaymentError as e:
        raise ToolError(str(e))
    return serializer.instance


def _payment_execute_delete(company, user, instance):
    from core.services.payments import reverse_payment
    reverse_payment(company, instance)


def _vehicle_pre_validate(company, user, payload):
    payload = dict(payload)
    if not (payload.get('vin') or '').strip():
        payload.pop('vin', None)  # blank VINs collide on the unique index
    return payload, ''


def _vehicle_check_limit(company):
    from core.middleware.plan_limits import check_vehicle_limit
    allowed, message = check_vehicle_limit(company)
    if not allowed:
        raise ToolError(message)


def _invoice_pre_validate(company, user, payload):
    from decimal import Decimal, InvalidOperation
    payload = dict(payload)
    if not payload.get('invoice_number'):
        payload['invoice_number'] = _unique_number(Invoice, 'invoice_number', 'INV')
    # total_amount/balance are serializer-required but recomputed by Invoice.save()
    # (total = subtotal + 15% VAT - discount). Compute the SAME figure here so the
    # confirmation card shows the real total the user will be charged, not the
    # pre-VAT subtotal (which is what the old setdefault(subtotal) displayed).
    try:
        subtotal = Decimal(str(payload.get('subtotal') or '0'))
        discount = Decimal(str(payload.get('discount') or '0'))
    except (InvalidOperation, ValueError, TypeError):
        subtotal, discount = Decimal('0'), Decimal('0')
    vat = (subtotal * Decimal('0.15')).quantize(Decimal('0.01'))
    total = (subtotal + vat - discount).quantize(Decimal('0.01'))
    payload.setdefault('total_amount', str(total))
    payload.setdefault('balance', str(total))
    return payload, 'Total shown includes 15% VAT; final VAT/total/balance are confirmed on save.'


def _load_pre_validate(company, user, payload):
    payload = dict(payload)
    if not payload.get('load_number'):
        payload['load_number'] = _unique_number(Load, 'load_number', 'LOAD')
    if not payload.get('total_amount') and payload.get('rate'):
        payload['total_amount'] = payload['rate']
    return payload, ''


def _settlement_pre_validate(company, user, payload):
    payload = dict(payload)
    if not payload.get('settlement_number'):
        payload['settlement_number'] = _unique_number(Settlement, 'settlement_number', 'STL')
    return payload, ''


def _driver_scope(user, qs):
    return qs.filter(user=user)


def _load_scope(user, qs):
    return qs.filter(driver__user=user)


def _settlement_scope(user, qs):
    return qs.filter(driver__user=user)


def _serializer_errors(serializer) -> str:
    parts = []
    for field, msgs in serializer.errors.items():
        msg = msgs[0] if isinstance(msgs, (list, tuple)) else msgs
        parts.append(f"{field}: {msg}")
    return '; '.join(str(p) for p in parts) or 'invalid data'


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

# Server-side fields never accepted from the model/user.
GLOBAL_EXCLUDED_FIELDS = {'id', 'company', 'created_by', 'created_at', 'updated_at', 'token', 'view_token'}

ENTITY_REGISTRY = {
    'customers': {
        'model': Customer, 'serializer': CustomerSerializer, 'label': 'Customer',
        'id_display': 'name', 'route': '/customers/{id}',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcu', 'DISPATCHER': 'rcu',
                  'VIEWER': 'r', 'DRIVER': ''},
        'required': [
            ('name', "What is the customer's name?"),
            ('email', "What is the customer's email address?"),
        ],
        'search': ['name', 'email', 'city'],
        'filter': ['name', 'email', 'city', 'status', 'is_active', 'payment_terms_default',
                   'credit_limit', 'avg_days_to_pay', 'created_at'],
        'order': ['name', 'created_at', 'avg_days_to_pay'],
        'agg': ['credit_limit', 'avg_days_to_pay'],
        'display': ['id', 'name', 'email', 'phone', 'city', 'status'],
    },
    'drivers': {
        'model': Driver, 'serializer': DriverSerializer, 'label': 'Driver',
        'id_display': 'license_number', 'route': '/fleet/drivers/{id}',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcu', 'DISPATCHER': 'ru',
                  'VIEWER': 'r', 'DRIVER': ''},
        'required': [
            ('first_name', "What is the driver's first name?"),
            ('last_name', "What is the driver's last name?"),
            ('email', "What is the driver's email? (a DRIVER login account is created/linked)"),
            ('license_number', "What is their license number?"),
            ('license_expiry', "When does the license expire? (YYYY-MM-DD)"),
            ('license_state', "Which province/state issued the license?"),
            ('hire_date', "What is the hire date? (YYYY-MM-DD)"),
        ],
        'virtual_create_fields': ['first_name', 'last_name', 'email'],
        'search': ['license_number', 'user__first_name', 'user__last_name', 'user__email'],
        'filter': ['status', 'license_number', 'license_expiry', 'hire_date'],
        'order': ['hire_date', 'license_expiry'],
        'agg': [],
        'display': ['id', 'license_number', 'status', 'license_expiry', 'hire_date'],
        'display_extra': {
            'name': lambda d: (f"{d.user.first_name} {d.user.last_name}".strip()
                               or d.user.username) if d.user_id else '',
            'email': lambda d: d.user.email if d.user_id else '',
        },
        'hooks': {'pre_validate': _resolve_or_create_driver_user,
                  'execute_create': _driver_execute_create},
    },
    'vehicles': {
        'model': Vehicle, 'serializer': VehicleSerializer, 'label': 'Vehicle',
        'id_display': 'plate', 'route': '/fleet/vehicles/{id}',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcu', 'DISPATCHER': 'ru',
                  'VIEWER': 'r', 'DRIVER': ''},
        'required': [
            ('make', "What make is the vehicle (e.g. TATA, Volvo)?"),
            ('model', "What model?"),
            ('year', "What year?"),
            ('plate', "What is the license plate?"),
            ('capacity', "What is the load capacity (tons)?"),
            ('fuel_type', "What fuel type (DIESEL/PETROL)?"),
        ],
        'search': ['plate', 'make', 'model', 'vin'],
        'filter': ['status', 'make', 'model', 'year', 'fuel_type', 'insurance_expiry',
                   'registration_expiry', 'mileage'],
        'order': ['year', 'mileage', 'insurance_expiry', 'registration_expiry'],
        'agg': ['mileage', 'capacity'],
        'display': ['id', 'plate', 'make', 'model', 'year', 'status', 'insurance_expiry'],
        'hooks': {'pre_validate': _vehicle_pre_validate,
                  'pre_execute_create': _vehicle_check_limit},
    },
    'vehicle_types': {
        'model': VehicleType, 'serializer': VehicleTypeSerializer, 'label': 'Vehicle Type',
        'id_display': 'name', 'route': '/fleet/vehicles',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcu', 'DISPATCHER': 'r',
                  'VIEWER': 'r', 'DRIVER': ''},
        'required': [('name', "What is the vehicle type called (e.g. Flatbed, Tautliner)?")],
        'search': ['name'],
        'filter': ['name'],
        'order': ['name'],
        'agg': [],
        'display': ['id', 'name', 'fuel_consumption_l_per_100km'],
    },
    'quotes': {
        'model': Quote, 'serializer': QuoteSerializer, 'label': 'Quote',
        'id_display': 'quote_number', 'route': '/bookings/quotes/{id}',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcud', 'DISPATCHER': 'rcu',
                  'VIEWER': 'r', 'DRIVER': ''},
        'required': [
            ('customer', "Which customer is this quote for? (existing name or a new one)"),
            ('pickup_location', "Where is the pickup?"),
            ('delivery_location', "Where is the delivery?"),
            ('cargo_description', "What cargo is being moved?"),
            ('weight', "What is the cargo weight (kg)?"),
            ('total_amount', "What price should be quoted (ZAR)? — must come from the user"),
        ],
        'virtual_create_fields': ['customer_name'],
        'fk': {'customer': ('customers', 'name')},
        'search': ['quote_number', 'pickup_location', 'delivery_location', 'cargo_description',
                   'customer__name'],
        'filter': ['status', 'quote_number', 'customer', 'vehicle_type', 'valid_until',
                   'total_amount', 'created_at', 'outcome', 'trip_type'],
        'order': ['created_at', 'valid_until', 'total_amount'],
        'agg': ['total_amount', 'base_rate', 'weight', 'distance'],
        'display': ['id', 'quote_number', 'pickup_location', 'delivery_location',
                    'total_amount', 'status', 'valid_until'],
        'display_extra': {'customer': lambda q: q.customer.name if q.customer_id else ''},
        'hooks': {'pre_validate': _quote_pre_validate,
                  'execute_create': _quote_execute_create},
        'field_notes': {
            'base_rate': 'the pre-surcharge rate — NOT the quoted "total"/"price"',
            'total_amount': 'the full quoted price the customer sees — use for "total"/"price"/"value"',
        },
    },
    'loads': {
        'model': Load, 'serializer': LoadSerializer, 'label': 'Load',
        'id_display': 'load_number', 'route': '/bookings/{id}',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcud', 'DISPATCHER': 'rcud',
                  'VIEWER': 'r', 'DRIVER': 'r'},
        'required': [
            ('customer', "Which customer is this load for?"),
            ('pickup_location', "Pickup address?"),
            ('pickup_city', "Pickup city?"),
            ('pickup_state', "Pickup province?"),
            ('pickup_zip', "Pickup postal code?"),
            ('pickup_date', "Pickup date? (YYYY-MM-DD)"),
            ('delivery_location', "Delivery address?"),
            ('delivery_city', "Delivery city?"),
            ('delivery_state', "Delivery province?"),
            ('delivery_zip', "Delivery postal code?"),
            ('delivery_date', "Delivery date? (YYYY-MM-DD)"),
            ('cargo_description', "What cargo?"),
            ('weight', "Cargo weight (kg)?"),
            ('rate', "What rate (ZAR)? — must come from the user"),
        ],
        'fk': {'customer': ('customers', 'name'), 'driver': ('drivers', 'license_number'),
               'vehicle': ('vehicles', 'plate')},
        'search': ['load_number', 'pickup_city', 'delivery_city', 'cargo_description',
                   'customer__name'],
        'filter': ['status', 'load_number', 'customer', 'driver', 'vehicle', 'pickup_date',
                   'delivery_date', 'total_amount', 'created_at'],
        'order': ['created_at', 'pickup_date', 'delivery_date', 'total_amount'],
        'agg': ['total_amount', 'rate', 'weight', 'distance'],
        'display': ['id', 'load_number', 'pickup_city', 'delivery_city', 'status',
                    'pickup_date', 'total_amount'],
        'scope': _load_scope,
        'hooks': {'pre_validate': _load_pre_validate},
    },
    'invoices': {
        'model': Invoice, 'serializer': InvoiceSerializer, 'label': 'Invoice',
        'id_display': 'invoice_number', 'route': '/finance/invoices/{id}',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcu', 'DISPATCHER': 'rcu',
                  'VIEWER': '', 'DRIVER': ''},
        'required': [
            ('customer', "Which customer is being invoiced?"),
            ('subtotal', "What is the subtotal (ZAR, excl. VAT)?"),
            ('due_date', "When is payment due? (YYYY-MM-DD)"),
        ],
        'fk': {'customer': ('customers', 'name')},
        # Money-integrity fields the AI must never set directly: totals/VAT are
        # recomputed by Invoice.save(); paid_amount/balance and status only move
        # through the locked payment flow (record_payment) and lifecycle events.
        # Letting the LLM write them would fabricate cash outside any audit trail.
        'protected_fields': ['paid_amount', 'balance', 'total_amount',
                             'vat_amount', 'tax_amount', 'status'],
        'search': ['invoice_number', 'customer__name'],
        'filter': ['status', 'invoice_number', 'customer', 'due_date', 'issue_date',
                   'total_amount', 'balance', 'paid_amount'],
        'order': ['due_date', 'issue_date', 'total_amount', 'balance'],
        'agg': ['total_amount', 'balance', 'paid_amount', 'subtotal'],
        'display': ['id', 'invoice_number', 'total_amount', 'balance', 'status', 'due_date'],
        'display_extra': {'customer': lambda i: i.customer.name if i.customer_id else ''},
        'hooks': {'pre_validate': _invoice_pre_validate},
        # Disambiguate near-synonym money fields — a live test showed the model
        # aggregating 'subtotal' when asked for "total balance" or even literally
        # "total_amount", silently undercounting by the VAT portion.
        'field_notes': {
            'subtotal': 'pre-VAT amount — NOT what "total"/"balance"/"total_amount" means',
            'total_amount': 'full invoiced amount INCLUDING VAT — use for "total_amount"/"invoice total"',
            'balance': 'amount STILL OWED — use for "balance"/"outstanding"/"amount owed"',
            'paid_amount': 'amount already paid on this invoice',
            'status': 'exact DB value (e.g. PARTIALLY_PAID, not "Partially Paid"); note '
                      'the company snapshot\'s overdue_count/overdue_total are computed '
                      'by due_date, NOT this field — for a "status is OVERDUE" question, '
                      'filter status=OVERDUE explicitly rather than reusing the snapshot',
        },
    },
    'payments': {
        'model': Payment, 'serializer': PaymentSerializer, 'label': 'Payment',
        'id_display': 'payment_number', 'route': '/finance/invoices/{invoice_id}',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rc', 'DISPATCHER': 'rc',
                  'VIEWER': '', 'DRIVER': ''},
        'required': [
            ('invoice', "Which invoice is being paid? (invoice number or id)"),
            ('amount', "What amount was paid (ZAR)?"),
            ('payment_method', "How was it paid (BANK_TRANSFER/CASH/CHEQUE/EFT/CREDIT_CARD)?"),
        ],
        'fk': {'invoice': ('invoices', 'invoice_number'), 'customer': ('customers', 'name')},
        # amount/invoice/customer deliberately NOT updatable — reverse and re-record instead.
        'update_fields': ['payment_method', 'reference_number', 'payment_date', 'notes'],
        'search': ['payment_number', 'reference_number', 'invoice__invoice_number'],
        'filter': ['payment_method', 'payment_date', 'amount', 'invoice', 'customer'],
        'order': ['payment_date', 'amount'],
        'agg': ['amount'],
        'display': ['id', 'payment_number', 'amount', 'payment_method', 'payment_date'],
        'hooks': {'pre_validate': _payment_pre_validate,
                  'execute_create': _payment_execute_create,
                  'execute_delete': _payment_execute_delete},
    },
    'expenses': {
        'model': Expense, 'serializer': ExpenseSerializer, 'label': 'Expense',
        'id_display': 'expense_number', 'route': '/finance/expenses',
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcud', 'DISPATCHER': 'rcu',
                  'VIEWER': '', 'DRIVER': ''},
        'required': [
            ('category', "What category (FUEL/TOLLS/MAINTENANCE/DRIVER_COST/INSURANCE/OVERHEAD/OTHER)?"),
            ('description', "What is the expense for?"),
            ('amount', "What amount (ZAR)?"),
            ('expense_date', "What date was it incurred? (YYYY-MM-DD)"),
        ],
        'fk': {'vehicle': ('vehicles', 'plate'), 'driver': ('drivers', 'license_number')},
        'search': ['expense_number', 'description'],
        'filter': ['category', 'status', 'expense_date', 'amount', 'vehicle', 'driver'],
        'order': ['expense_date', 'amount'],
        'agg': ['amount'],
        'display': ['id', 'expense_number', 'category', 'description', 'amount', 'expense_date', 'status'],
    },
    'settlements': {
        'model': Settlement, 'serializer': SettlementSerializer, 'label': 'Settlement',
        'id_display': 'settlement_number', 'route': None,
        'perms': {'ADMIN': 'rcud', 'MANAGER': 'rcud', 'OPERATOR': 'rcu', 'DISPATCHER': 'r',
                  'VIEWER': '', 'DRIVER': 'r'},
        'required': [
            ('driver', "Which driver is being settled?"),
            ('start_date', "Settlement period start? (YYYY-MM-DD)"),
            ('end_date', "Settlement period end? (YYYY-MM-DD)"),
            ('driver_pay', "Gross driver pay (ZAR)?"),
            ('net_pay', "Net pay after deductions (ZAR)?"),
        ],
        'fk': {'driver': ('drivers', 'license_number')},
        'search': ['settlement_number'],
        'filter': ['status', 'driver', 'start_date', 'end_date', 'net_pay'],
        'order': ['start_date', 'net_pay'],
        'agg': ['driver_pay', 'net_pay'],
        'display': ['id', 'settlement_number', 'start_date', 'end_date', 'net_pay', 'status'],
        'scope': _settlement_scope,
        'hooks': {'pre_validate': _settlement_pre_validate},
    },
}

_OP_CODES = {'read': 'r', 'create': 'c', 'update': 'u', 'delete': 'd'}


def role_can(user, table, op) -> bool:
    """Server-side permission check: does this user's role allow op on table?"""
    spec = ENTITY_REGISTRY.get(table)
    if spec is None:
        return False
    role = (getattr(user, 'role', '') or '').upper()
    if getattr(user, 'is_superuser', False):
        return True
    return _OP_CODES[op] in spec['perms'].get(role, '')


def allowed_tables(user, op):
    return [t for t in ENTITY_REGISTRY if role_can(user, t, op)]


# Sending an email isn't CRUD on any one table, so it's gated by its own role
# allowlist rather than the entity r/c/u/d strings. VIEWER is read-only by
# design; DRIVER shouldn't be emailing customers on the company's behalf.
EMAIL_SEND_ROLES = {'ADMIN', 'MANAGER', 'OPERATOR', 'DISPATCHER'}


def can_send_email(user) -> bool:
    if getattr(user, 'is_superuser', False):
        return True
    return (getattr(user, 'role', '') or '').upper() in EMAIL_SEND_ROLES


def scoped_queryset(user, company, table):
    spec = ENTITY_REGISTRY[table]
    qs = spec['model'].objects.all()
    if hasattr(spec['model'], 'company_id'):
        qs = qs.filter(company=company)
    role = (getattr(user, 'role', '') or '').upper()
    scope = spec.get('scope')
    if scope and role == 'DRIVER':
        qs = scope(user, qs)
    return qs


def writable_fields(table, *, for_update=False):
    """Field names the AI may supply, derived from the serializer minus
    server-side fields. Payments restrict updates to safe fields; entities may
    also declare `protected_fields` (a blocklist) for money-integrity columns the
    AI must never set on create OR update (e.g. invoice totals/paid_amount)."""
    spec = ENTITY_REGISTRY[table]
    protected = set(spec.get('protected_fields', ()))
    if for_update and spec.get('update_fields'):
        return [f for f in spec['update_fields'] if f not in protected]
    serializer = spec['serializer']()
    names = []
    for name, field in serializer.get_fields().items():
        if field.read_only or name in GLOBAL_EXCLUDED_FIELDS or name in protected:
            continue
        if name == 'user' and table == 'drivers':
            continue  # handled by the driver-user hook
        names.append(name)
    if not for_update:
        names += spec.get('virtual_create_fields', [])
    return names


def delete_related_counts(instance) -> dict:
    """{related_label: count} for every PROTECT relation pointing at instance."""
    from django.db.models import ProtectedError  # noqa: F401  (documentation)
    from django.db import models as dj_models

    counts = {}
    for rel in instance._meta.related_objects:
        on_delete = getattr(rel, 'on_delete', None) or getattr(rel.field.remote_field, 'on_delete', None)
        if on_delete is not dj_models.PROTECT:
            continue
        n = rel.related_model.objects.filter(**{rel.field.name: instance}).count()
        if n:
            counts[rel.related_model._meta.verbose_name_plural.lower()] = n
    return counts


# ---------------------------------------------------------------------------
# Tool schema + prompt generation
# ---------------------------------------------------------------------------

_FILTER_OPS = ['eq', 'neq', 'lt', 'lte', 'gt', 'gte', 'contains', 'in', 'isnull']


def build_tool_schemas(user):
    """OpenAI tool definitions with table enums filtered to the user's role.
    Tools with an empty enum are omitted entirely."""
    tools = []
    read_tables = allowed_tables(user, 'read')
    if read_tables:
        tools.append({
            "type": "function",
            "function": {
                "name": "query_records",
                "description": (
                    "Query the company's database precisely. Use this for lists, lookups, "
                    "counts and sums the snapshot doesn't answer, and to resolve names to ids "
                    "before proposing a create/update/delete. For ANY total, sum, count or "
                    "average, ALWAYS pass `aggregate` — NEVER add up the returned `rows` "
                    "yourself, since results may be truncated (see the `truncated`/`note` "
                    "fields in the response) and a manual sum over a truncated page silently "
                    "undercounts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "enum": read_tables},
                        "filters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "op": {"type": "string", "enum": _FILTER_OPS},
                                    "value": {},
                                },
                                "required": ["field", "op", "value"],
                            },
                        },
                        "search": {"type": "string", "description": "Free-text search over the table's search fields."},
                        "order_by": {"type": "string", "description": "Field name; prefix with - for descending."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "aggregate": {
                            "type": "object",
                            "properties": {
                                "func": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
                                "field": {"type": "string"},
                                "group_by": {"type": "string"},
                            },
                            "required": ["func"],
                        },
                    },
                    "required": ["table"],
                },
            },
        })

    def _write_tool(name, op, description, extra_props=None, extra_required=None):
        tables = allowed_tables(user, op)
        if not tables:
            return None
        props = {"table": {"type": "string", "enum": tables}}
        props.update(extra_props or {})
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": ["table"] + (extra_required or []),
                },
            },
        }

    fields_prop = {"fields": {
        "type": "object",
        "description": "Field values per the schema reference. FKs take numeric ids (resolve names via query_records first).",
        "additionalProperties": True,
    }}
    t = _write_tool(
        "propose_create", "create",
        "Prepare a NEW record for user confirmation. Nothing is saved until the user confirms "
        "the card in the UI. Call only when every required field has been provided by the user.",
        fields_prop, ["fields"],
    )
    if t:
        tools.append(t)
    t = _write_tool(
        "propose_update", "update",
        "Prepare changes to an EXISTING record for user confirmation. Provide only the fields "
        "that change. Resolve the record id via query_records first.",
        {**fields_prop, "record_id": {"type": "integer"}}, ["record_id", "fields"],
    )
    if t:
        tools.append(t)
    t = _write_tool(
        "propose_delete", "delete",
        "Prepare deletion of an EXISTING record for user confirmation. Resolve the record id "
        "via query_records first.",
        {"record_id": {"type": "integer"}}, ["record_id"],
    )
    if t:
        tools.append(t)

    if can_send_email(user):
        tools.append({
            "type": "function",
            "function": {
                "name": "propose_send_email",
                "description": (
                    "Prepare an email to a known Customer or Driver contact for user confirmation. "
                    "Nothing is sent until the user confirms the card. Resolve the recipient's id via "
                    "query_records first — never invent or accept an email address from the user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "recipient_type": {"type": "string", "enum": ["customer", "driver"]},
                        "recipient_id": {"type": "integer"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "analysis_summary": {
                            "type": "string",
                            "description": "Brief summary of the data you researched to inform this email "
                                           "(e.g. invoice history, overdue days) — shown to the user on the card.",
                        },
                    },
                    "required": ["recipient_type", "recipient_id", "subject", "body"],
                },
            },
        })
    return tools


def build_schema_reference(user) -> str:
    """Compact per-table field reference for the system prompt (read tables only).

    Lists the UNION of writable fields and queryable fields (agg/filter) — not
    just writable_fields() — because fields the AI may never SET (e.g. an
    invoice's `balance`/`total_amount`/`status`, blocked via `protected_fields`)
    are still exactly what it needs to READ/aggregate/filter correctly. Showing
    only writable fields silently hid the very fields whose disambiguating
    `field_notes` mattered most (a live test showed the model substituting
    `subtotal` for `balance`/`total_amount` because those two never appeared in
    this reference at all)."""
    lines = []
    for table in allowed_tables(user, 'read'):
        spec = ENTITY_REGISTRY[table]
        required = {f for f, _q in spec['required']}
        field_notes = spec.get('field_notes', {})
        ops = ''.join(c for c in 'rcud'
                      if c in spec['perms'].get((getattr(user, 'role', '') or '').upper(), '')
                      or getattr(user, 'is_superuser', False))
        writable = writable_fields(table)
        writable_set = set(writable)
        queryable_only = [f for f in (spec.get('agg', []) + spec.get('filter', []))
                          if f not in writable_set and '__' not in f]
        # dict.fromkeys dedupes while preserving first-seen order.
        all_fields = list(dict.fromkeys(writable + queryable_only))
        fields = []
        for name in all_fields:
            label = f"{name}*" if name in required else name
            if name not in writable_set:
                label += ' [read/query only]'
            if name in field_notes:
                label += f" ({field_notes[name]})"
            fields.append(label)
        fk_notes = ', '.join(f"{f}→{t}.{lbl}" for f, (t, lbl) in spec.get('fk', {}).items())
        line = f"- {table} [{ops}] fields: {', '.join(fields)}"
        if fk_notes:
            line += f" | FKs: {fk_notes}"
        if spec.get('update_fields'):
            line += f" | updatable: {', '.join(spec['update_fields'])}"
        lines.append(line)
    return '\n'.join(lines)


def guided_questions(table) -> str:
    spec = ENTITY_REGISTRY[table]
    return '; '.join(f"{f}: {q}" for f, q in spec['required'])


def validate_registry():
    """Dev/test guard: every declared field must exist on its model (or be virtual)."""
    problems = []
    for table, spec in ENTITY_REGISTRY.items():
        model = spec['model']
        model_fields = {f.name for f in model._meta.get_fields()}
        virtual = set(spec.get('virtual_create_fields', []))
        for group in ('filter', 'order', 'agg', 'display'):
            for f in spec.get(group, []):
                base = f.split('__')[0]
                if base not in model_fields:
                    problems.append(f"{table}.{group}: unknown field '{f}'")
        for f in spec.get('search', []):
            if f.split('__')[0] not in model_fields:
                problems.append(f"{table}.search: unknown field '{f}'")
        for f, _q in spec['required']:
            if f not in model_fields and f not in virtual:
                problems.append(f"{table}.required: unknown field '{f}'")
    return problems
