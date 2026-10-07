import json
from decimal import Decimal
import re
from rest_framework import serializers
from django.db.models import Avg, Q  # ADD THIS IMPORT
from .models import (
    User, Customer, Driver, Vehicle, VehicleLog, VehicleType, Load,
    Quote, Invoice, Payment, Expense, Settlement, Notification, Company, ActivityEvent,
    InvoiceLine, CreditNote, CreditNoteLine, Supplier,
)
from .serializers_billing import DeliveryFeeChargeSerializer

class CompanyScopedRelationsMixin:
    """Tenant isolation for writable relation fields (2026-09).

    `fields='__all__'` gives every FK a PrimaryKeyRelatedField over the model's
    whole table, so a caller could attach another tenant's customer / load /
    trip / vehicle / driver / quote / invoice by id (and the response then
    echoed that tenant's data back). This narrows each listed field's queryset
    to the record's own company:

      * update  -> the instance's company (falls back to the caller's company
                   for a legacy row with no company);
      * create  -> context['company'] if the caller passed one (internal
                   services), else request.user.company.

    A foreign id then fails validation exactly like a missing one ("Invalid pk
    ... object does not exist"). An authenticated non-superuser with no company
    gets an empty queryset (fail closed). A superuser with no company, and
    internal callers that pass neither request nor company, keep the previous
    unscoped behaviour.

    Legacy-data safety: the value a relation ALREADY holds on the instance
    being updated stays valid (so re-saving an unchanged record whose related
    row predates company backfills never starts failing), as do ids a trusted
    server-side caller lists in context['allow_relation_ids'].
    """

    # field name -> ORM lookup from the related model to its Company
    company_scoped_relations = {}

    _UNSCOPED = object()

    def _relation_company(self):
        instance = self.instance
        if instance is not None and not hasattr(instance, '__iter__'):
            company_id = getattr(instance, 'company_id', None)
            if company_id is not None:
                return company_id
        if 'company' in self.context:
            company = self.context['company']
            return getattr(company, 'pk', company)
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return self._UNSCOPED
        company_id = getattr(user, 'company_id', None)
        if company_id is None and getattr(user, 'is_superuser', False):
            return self._UNSCOPED
        return company_id

    def get_fields(self):
        fields = super().get_fields()
        company_id = self._relation_company()
        if company_id is self._UNSCOPED:
            return fields
        instance = self.instance if (
            self.instance is not None and not hasattr(self.instance, '__iter__')
        ) else None
        trusted = self.context.get('allow_relation_ids') or {}
        for name, lookup in self.company_scoped_relations.items():
            field = fields.get(name)
            if field is None or field.read_only:
                continue
            queryset = getattr(field, 'queryset', None)
            if queryset is None:
                continue
            allowed = Q(**{lookup: company_id}) if company_id is not None else Q(pk__in=[])
            keep = [pk for pk in (
                getattr(instance, f'{name}_id', None) if instance is not None else None,
                trusted.get(name),
            ) if pk is not None]
            if keep:
                allowed |= Q(pk__in=keep)
            field.queryset = queryset.filter(allowed)
        return fields


# User Serializer
class UserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='get_full_name', read_only=True)
    last_active = serializers.DateTimeField(source='last_login', read_only=True)
    # Declared as CharField (not the model ChoiceField) so we can normalise the
    # UI's lower-case role values to the model's upper-case choices.
    role = serializers.CharField(required=False)
    # Diagnostic visibility only — there was previously no way, even for the
    # user themselves, to see their own tenant binding or superuser status from
    # the app (had to go through Django admin). company_name is a method field
    # since company can be null (legacy/seed accounts with no company bound).
    company_id = serializers.IntegerField(read_only=True)
    company_name = serializers.SerializerMethodField()
    # Lets the frontend know upfront (from the same /auth/me/ call it already
    # makes on every load) whether quoting/invoicing are blocked, instead of
    # only discovering it reactively when PlanLimitsMiddleware 402s an action.
    subscription_status = serializers.SerializerMethodField()
    # A company keeps full access (subscription_status stays 'active'/
    # 'grace_period') for the rest of its paid period after cancelling — this
    # is the only signal that a cancellation is pending, so the header badge
    # can show "Cancelling" instead of silently staying "Online" until the
    # daily sweep finalises subscription_status to 'cancelled'.
    cancel_at_period_end = serializers.SerializerMethodField()
    # Lets the frontend block the demo's one-quote-per-session cap
    # client-side (same /auth/me/ call above) instead of only discovering it
    # reactively when the quote-create endpoint rejects it server-side.
    is_demo = serializers.SerializerMethodField()
    # Per-session, not per-company: every demo visitor shares the same
    # demo@truckwys.com login, so a company-wide flag would mean the first
    # visitor anywhere locks out every other visitor. This reads the current
    # request's own UserSession (core.models.UserSession, set by
    # core.auth.session_auth.UserSessionTokenAuthentication as request.auth)
    # — see QuoteViewSet.create for where it's actually enforced.
    demo_quote_used = serializers.SerializerMethodField()

    def get_company_name(self, obj):
        return obj.company.company_name if obj.company_id else None

    def get_subscription_status(self, obj):
        return obj.company.subscription_status if obj.company_id else None

    def get_cancel_at_period_end(self, obj):
        return obj.company.cancel_at_period_end if obj.company_id else False

    def get_is_demo(self, obj):
        return obj.company.is_demo if obj.company_id else False

    def get_demo_quote_used(self, obj):
        from core.models import UserSession
        request = self.context.get('request')
        session = getattr(request, 'auth', None) if request else None
        return bool(isinstance(session, UserSession) and session.demo_quote_used)

    def validate_role(self, value):
        if not isinstance(value, str):
            return value
        normalized = value.upper()
        valid = {c[0] for c in User.ROLE_CHOICES}
        if normalized not in valid:
            raise serializers.ValidationError(f'"{value}" is not a valid role.')
        return normalized

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'password', 'first_name', 'last_name', 'name', 'job_title',
                  'role', 'status', 'phone', 'address', 'timezone', 'language', 'date_format',
                  'notification_settings', 'avatar', 'last_active', 'is_active', 'created_at', 'updated_at',
                  'is_superuser', 'company_id', 'company_name', 'subscription_status', 'cancel_at_period_end',
                  'is_demo', 'demo_quote_used']
        # notification_settings is read-only here: the validated
        # NotificationSettingsView is the single write path for preferences.
        read_only_fields = ['id', 'created_at', 'updated_at', 'last_active',
                           'is_superuser', 'company_id', 'company_name',
                           'notification_settings']
        extra_kwargs = {'password': {'write_only': True, 'required': False}}

    def validate_email(self, value):
        # Login and password reset treat an email as one identity across all
        # accounts, so API-created users (e.g. drivers) must not reuse one.
        if not value:
            return value
        qs = User.objects.filter(email__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def create(self, validated_data):
        # Without this, password was silently dropped, leaving every API-created
        # user (e.g. drivers) unable to log in.
        password = validated_data.pop('password', None)
        user = User.objects.create_user(**validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=['password'])
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=['password'])
        return user



class SelfProfileSerializer(UserSerializer):
    """Serializer for a user editing THEMSELVES via /auth/me/.

    UserSerializer is also the admin user-management serializer, where role,
    status, is_active and username are legitimately writable (UserViewSet is
    IsAdmin-only). /auth/me/ is open to every authenticated user, so those
    authorisation fields must be read-only here — otherwise any DRIVER /
    DISPATCHER / VIEWER could PATCH {"role": "ADMIN"} and take over the company.

    Sending a protected field with its CURRENT value is accepted (clients that
    round-trip the GET payload keep working); sending a different value is a
    400 with a per-field error rather than a silent ignore.
    """
    PROTECTED_FIELDS = ('role', 'status', 'is_active', 'username')

    role = serializers.CharField(read_only=True)

    class Meta(UserSerializer.Meta):
        read_only_fields = UserSerializer.Meta.read_only_fields + [
            'status', 'is_active', 'username',
        ]

    @staticmethod
    def _normalise(field, value):
        if field == 'is_active':
            try:
                return serializers.BooleanField().to_internal_value(value)
            except serializers.ValidationError:
                return value
        if field in ('role', 'status') and isinstance(value, str):
            return value.strip().upper()
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        data = getattr(self, 'initial_data', None) or {}
        errors = {}
        for field in self.PROTECTED_FIELDS:
            if field not in data:
                continue
            sent = self._normalise(field, data.get(field))
            current = self._normalise(field, getattr(self.instance, field))
            if sent != current:
                errors[field] = [
                    f'You cannot change your own {field.replace("_", " ")} here; '
                    'ask a company admin.'
                ]
        # Session-hijack hardening: a stolen access token could otherwise change
        # the password with no proof of the current one, locking the real user
        # out permanently. ChangePasswordView (/auth/change-password/) is the one
        # path that verifies the current password before setting a new one.
        if 'password' in data:
            errors['password'] = [
                'Change your password from Security settings (this verifies your '
                'current password first), not here.'
            ]
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

# Customer Serializer
class CustomerSerializer(serializers.ModelSerializer):
    # Only on the Customers list (core.services.customer_list annotates them);
    # null everywhere else.
    owed_amount = serializers.SerializerMethodField()
    overdue_amount = serializers.SerializerMethodField()
    oldest_overdue_due = serializers.SerializerMethodField()

    def get_owed_amount(self, obj):
        v = getattr(obj, 'owed_amount', None)
        return float(v) if v is not None else None

    def get_overdue_amount(self, obj):
        v = getattr(obj, 'overdue_amount', None)
        return float(v) if v is not None else None

    def get_oldest_overdue_due(self, obj):
        v = getattr(obj, 'oldest_overdue_due', None)
        return v.isoformat() if v else None

    class Meta:
        model = Customer
        # debtor_identity is the cross-tenant Capital link: never exposed.
        exclude = ['debtor_identity']
        read_only_fields = ['id', 'company', 'created_at', 'updated_at', 'legal_name_key']
        # Address details are optional on quick-add (directory). They can be
        # filled in later from the full customer record.
        extra_kwargs = {
            'address': {'required': False, 'allow_blank': True, 'default': ''},
            'state': {'required': False, 'allow_blank': True, 'default': ''},
            'zip_code': {'required': False, 'allow_blank': True, 'default': ''},
            'city': {'required': False, 'allow_blank': True, 'default': ''},
            'phone': {'required': False, 'allow_blank': True, 'default': ''},
        }

    def validate_country(self, value):
        value = (value or 'ZA').strip().upper()
        if not re.fullmatch(r'[A-Z]{2}', value):
            raise serializers.ValidationError('Use a two-letter country code, e.g. ZA.')
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        country = attrs.get('country') or (self.instance.country if self.instance else 'ZA')
        errors = {}
        for field, kind in (('vat_number', 'vat'), ('registration_number', 'reg')):
            if field in attrs:
                try:
                    attrs[field] = _validate_identifier(attrs[field], kind, country)
                except serializers.ValidationError as e:
                    errors[field] = e.detail
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


# Driver Serializer
class DriverSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    assigned_vehicle = serializers.SerializerMethodField()
    total_trips = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 'user' defaults to an unscoped User.objects.all() PK field, so
        # without this a driver record could be linked to another company's
        # user id. Same policy as CompanyFilterMixin: a platform superuser
        # with no company of their own stays unscoped, everyone else (staff
        # included) is limited to their own company's users.
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            if user.is_superuser and getattr(user, 'company_id', None) is None:
                return
            self.fields['user'].queryset = User.objects.filter(company_id=getattr(user, 'company_id', None))

    class Meta:
        model = Driver
        fields = [
            'id', 'user', 'user_details', 'license_number', 'license_expiry',
            'license_state', 'medical_card_expiry', 'hire_date', 'status',
            'emergency_contact', 'emergency_phone', 'violation_count',
            'accident_history', 'experience_years', 'created_at', 'updated_at',
            # computed fields written by background task
            'efficiency_score', 'on_time_rate', 'safety_score', 'total_distance',
            'trips_this_month', 'revenue_generated', 'avg_revenue_per_trip',
            'margin_per_trip',
            # live helpers
            'total_trips', 'assigned_vehicle',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at',
            'efficiency_score', 'on_time_rate', 'safety_score', 'total_distance',
            'trips_this_month', 'revenue_generated', 'avg_revenue_per_trip',
            'margin_per_trip', 'total_trips', 'assigned_vehicle',
        ]

    def get_total_trips(self, obj):
        """Live count — cheap, used for the profile header before the task runs."""
        return obj.loads.filter(status__in=['DELIVERED', 'INVOICED']).count()

    def get_assigned_vehicle(self, obj):
        vehicle = obj.vehicles.first()
        if not vehicle:
            return None
        parts = [vehicle.plate]
        if vehicle.make or vehicle.model:
            parts.append(f"{vehicle.make or ''} {vehicle.model or ''}".strip())
        return ' — '.join(filter(None, parts))


# Vehicle Serializer
class VehicleSerializer(serializers.ModelSerializer):
    driver_name = serializers.SerializerMethodField()
    vehicle_type_name = serializers.CharField(source='vehicle_type.name', read_only=True)
    # The linked VehicleType's own payload capacity (tonnes) — distinct from
    # this Vehicle's own `capacity` field below, which is set independently
    # per-vehicle and can drift from its type. The Vehicles directory shows
    # this one right after the type name specifically because it's the
    # authoritative "what does this type of truck carry" figure (see the
    # payload/GVM labeling fix across VehicleType — core/services/vehicle_types.py).
    vehicle_type_capacity = serializers.DecimalField(source='vehicle_type.capacity', max_digits=10, decimal_places=2, read_only=True)
    revenue_generated = serializers.SerializerMethodField()
    total_trips = serializers.SerializerMethodField()
    utilisation_rate = serializers.SerializerMethodField()
    type = serializers.CharField(max_length=50, required=False, default='TRUCK')
    vin = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')

    class Meta:
        model = Vehicle
        fields = [
            'id', 'company', 'vin', 'make', 'model', 'driver', 'vehicle_type',
            'year', 'plate', 'type', 'capacity', 'status', 'fuel_type', 'mileage',
            'service_interval_km', 'last_service_mileage',
            'last_maintenance_date', 'next_maintenance_due', 'insurance_expiry',
            'registration_expiry', 'ai_health_score', 'fuel_efficiency_score',
            'uptime_score', 'maintenance_score', 'uptime_percentage', 'cost_per_km',
            'margin_per_trip', 'fuel_consumption_per_km', 'created_at', 'updated_at',
            'driver_name', 'vehicle_type_name', 'vehicle_type_capacity', 'revenue_generated', 'total_trips',
            'utilisation_rate', 'cartrack_registration', 'latitude', 'longitude',
            'heading', 'speed_kmh', 'ignition_on', 'last_location_at',
            'temp1', 'temp2', 'temp3', 'temp4', 'cartrack_current_driver_ref',
            'door_open', 'last_door_event_at', 'ctrlfleet_vehicle_code',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'driver_name', 'vehicle_type_name', 'vehicle_type_capacity',
                           'revenue_generated', 'total_trips', 'utilisation_rate',
                           'latitude', 'longitude', 'heading', 'speed_kmh', 'ignition_on',
                           'last_location_at', 'temp1', 'temp2', 'temp3', 'temp4',
                           'cartrack_current_driver_ref', 'door_open', 'last_door_event_at',
                           'ctrlfleet_vehicle_code']

    def get_driver_name(self, obj):
        if not obj.driver:
            return None
        u = obj.driver.user
        name = f"{u.first_name} {u.last_name}".strip()
        return name or u.username

    def get_revenue_generated(self, obj):
        """Calculate total revenue from all delivered loads for this vehicle."""
        from django.db.models import Sum
        total = obj.loads.filter(status='DELIVERED').aggregate(total=Sum('total_amount'))['total']
        return float(total) if total else 0.0

    def get_total_trips(self, obj):
        """Count total delivered loads for this vehicle."""
        return obj.loads.filter(status='DELIVERED').count()

    def get_utilisation_rate(self, obj):
        """Calculate utilization rate as percentage of loads in transit or delivered."""
        total_loads = obj.loads.count()
        if total_loads == 0:
            return 0.0
        active_loads = obj.loads.filter(status__in=['IN_TRANSIT', 'DELIVERED', 'LOADING', 'ASSIGNED']).count()
        return round((active_loads / total_loads) * 100, 2)


# VehicleType Serializer
class VehicleTypeSerializer(serializers.ModelSerializer):
    # Number of AVAILABLE vehicles of this type — used to hide types the
    # company can't actually fulfil when creating a quote.
    available_vehicle_count = serializers.SerializerMethodField()
    # Number of vehicles of this type the company OWNS, whatever their status
    # today. available_vehicle_count answers "can we run this right now";
    # this answers "does this fleet run this type at all", which is what a
    # quote for a load weeks out needs — a truck in transit today is still a
    # truck the fleet owns.
    owned_vehicle_count = serializers.SerializerMethodField()
    # True for a company-owned row that shadows a shared (company=None)
    # default of the same name — the result of a tenant user editing a shared
    # type (VehicleTypeViewSet.update's copy-on-write, core/views.py). The
    # frontend uses this to offer "Reset to shared default" instead of
    # "Delete" for these specifically.
    overrides_shared_default = serializers.SerializerMethodField()

    class Meta:
        model = VehicleType
        fields = '__all__'
        read_only_fields = ['id', 'company', 'created_at', 'updated_at']
        extra_kwargs = {
            'description': {'required': False, 'allow_blank': True, 'default': ''},
            'capacity': {'required': False, 'default': 0},
            'max_distance': {'required': False, 'default': 0},
            'base_rate': {'required': False, 'default': 0},
        }

    def get_owned_vehicle_count(self, obj):
        request = self.context.get('request')
        company = getattr(getattr(request, 'user', None), 'company', None)
        if company is None:
            # No company context (superuser browsing, tests) — the link-only
            # count is the best available answer without leaking other
            # companies' vehicles through a shared (company=None) type.
            try:
                return obj.vehicles.count()
            except Exception:
                return 0
        # Cached per request, not per object: DRF reuses one serializer
        # instance across every row in a list response.
        from core.services.vehicle_types import owned_vehicle_rows, count_available
        if not hasattr(self, '_owned_vehicles_cache'):
            self._owned_vehicles_cache = owned_vehicle_rows(company)
        return count_available(self._owned_vehicles_cache, obj.id, obj.name)

    def get_available_vehicle_count(self, obj):
        # A vehicle counts toward this type if EITHER its vehicle_type link
        # points here OR its own free-text `type` name matches this type's
        # name (case-insensitive) — the link can silently drift from what a
        # vehicle actually displays as its type (confirmed with real data:
        # a vehicle whose visible type didn't match what it was linked to),
        # so trusting the link alone can both hide a type you own and show
        # one you don't. Also scoped to the requesting company only — these
        # rows are frequently the shared (company=None) defaults, so counting
        # through the bare link without a company filter would leak other
        # companies' vehicles into the number.
        request = self.context.get('request')
        company = getattr(getattr(request, 'user', None), 'company', None)

        if company is None:
            # No company context (superuser browsing, an unauthenticated/test
            # call) — fall back to the old link-only annotation/count so this
            # never breaks other callers.
            val = getattr(obj, 'avail_count', None)
            if val is not None:
                return val
            try:
                return obj.vehicles.filter(status='AVAILABLE').count()
            except Exception:
                return 0

        # Cached on the serializer instance, not per-object: DRF reuses one
        # serializer instance across every object in a list response, so this
        # runs once per request, not once per vehicle type.
        from core.services.vehicle_types import available_vehicle_rows, count_available
        if not hasattr(self, '_available_vehicles_cache'):
            self._available_vehicles_cache = available_vehicle_rows(company)

        return count_available(self._available_vehicles_cache, obj.id, obj.name)

    def get_overrides_shared_default(self, obj):
        if obj.company_id is None:
            return False
        # Cached per-request (not per-object) — same reasoning as
        # _available_vehicles_cache above: DRF reuses one serializer instance
        # across a whole list response.
        if not hasattr(self, '_shared_default_names_cache'):
            self._shared_default_names_cache = set(
                VehicleType.objects.filter(company__isnull=True).values_list('name', flat=True)
            )
        return obj.name in self._shared_default_names_cache


# VehicleLog Serializer
class VehicleLogSerializer(serializers.ModelSerializer):
    vehicle_details = VehicleSerializer(source='vehicle', read_only=True)
    user_name = serializers.CharField(source='user.username', read_only=True)
    
    class Meta:
        model = VehicleLog
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


# Load Serializer
class LoadSerializer(CompanyScopedRelationsMixin, serializers.ModelSerializer):
    company_scoped_relations = {
        'customer': 'company_id', 'driver': 'company_id',
        'vehicle': 'company_id', 'quote': 'company_id',
    }
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    driver_name = serializers.SerializerMethodField()
    vehicle_info = serializers.SerializerMethodField()
    quote_number = serializers.SerializerMethodField()
    # Estimated fuel: the quote's fuel line, copied to fuel_surcharge by
    # convert_to_load. Actual fuel: approved FUEL expenses logged against the
    # load's trips. None when there's no figure, never a misleading 0.
    fuel_cost_estimated = serializers.SerializerMethodField()
    fuel_cost_actual = serializers.SerializerMethodField()
    # Price excl. VAT, VAT and total incl. VAT for the order, by the same
    # rule as its quote (core.services.quote_vat: 15%, or 0% international).
    customer_price = serializers.SerializerMethodField()

    class Meta:
        model = Load
        fields = '__all__'
        # POD fields are evidence a capital advance is funded against, so they
        # are written only by LoadViewSet.upload_pod (and fleet integrations),
        # never by a PATCH that could type in a "signature" (audit §6 #5).
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'created_by', 'actual_delivered_at', 'company',
            'pod_signature', 'pod_received_by', 'pod_document',
            'pod_captured_at', 'pod_latitude', 'pod_longitude', 'pod_device',
            'pod_source', 'pod_file_sha256',
        ]

    def get_customer_price(self, obj):
        from core.services.quote_vat import public_fields, quote_vat
        return public_fields(quote_vat(obj))

    def get_fuel_cost_estimated(self, obj):
        value = obj.fuel_surcharge
        return float(value) if value else None

    def get_fuel_cost_actual(self, obj):
        if hasattr(obj, 'fuel_actual_total'):
            total = obj.fuel_actual_total
        else:
            from django.db.models import Sum
            from core.models import Expense
            total = Expense.objects.filter(
                trip__load=obj, category='FUEL', status='APPROVED',
            ).aggregate(t=Sum('amount'))['t']
        return float(total) if total is not None else None

    def get_driver_name(self, obj):
        if not obj.driver:
            return None
        u = obj.driver.user
        name = f"{u.first_name} {u.last_name}".strip()
        return name or u.username

    def get_vehicle_info(self, obj):
        if obj.vehicle:
            return f"{obj.vehicle.make} {obj.vehicle.model} - {obj.vehicle.plate}"
        return None

    def get_quote_number(self, obj):
        if obj.quote:
            return obj.quote.quote_number
        return None

    def validate(self, attrs):
        # 'vehicle' is absent from attrs on a partial update that doesn't
        # touch it — fall back to the existing instance so a PATCH that only
        # sends {status: 'ASSIGNED'} is checked against what's actually
        # assigned, not treated as if it were blank. Driver is optional — a
        # vehicle alone is enough to mark an order Assigned.
        has_key = lambda k: k in attrs
        vehicle = attrs['vehicle'] if has_key('vehicle') else getattr(self.instance, 'vehicle', None)
        new_status = attrs.get('status', getattr(self.instance, 'status', None))
        if new_status == 'ASSIGNED' and not vehicle:
            raise serializers.ValidationError({
                'status': 'Assign a vehicle before this order can be marked Assigned.'
            })
        return attrs


# Quote Serializer
class QuoteSerializer(CompanyScopedRelationsMixin, serializers.ModelSerializer):
    company_scoped_relations = {
        'customer': 'company_id', 'vehicle': 'company_id', 'driver': 'company_id',
    }
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_email = serializers.CharField(source='customer.email', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    customer_company = serializers.CharField(source='customer.company_name', read_only=True)
    customer_city = serializers.CharField(source='customer.city', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    quote_number = serializers.CharField(required=False, allow_blank=True)
    vehicle_display = serializers.SerializerMethodField()
    driver_display = serializers.SerializerMethodField()
    # The load this quote was converted into (convert_to_load). The load is
    # the source of truth for "converted": the quote itself stays ACCEPTED.
    booked_load = serializers.SerializerMethodField()
    converted = serializers.SerializerMethodField()
    # What the customer is shown once the quote is sent: price excl. VAT,
    # VAT and total incl. VAT (core.services.quote_vat; same figures as the
    # PDF, emails and online quote page). Used for the WhatsApp message.
    customer_price = serializers.SerializerMethodField()
    # Pricing analysis (additive): what the pricing panel showed and what the
    # operator picked, stored as QuotePricingDecision. Write-only here; the
    # quote DETAIL response carries it back read-only (to_representation).
    pricing_decision = serializers.JSONField(required=False, allow_null=True, write_only=True)
    # Declared explicitly (not left to ModelSerializer's auto-introspection)
    # so the OpenAPI schema is honest about this: the old client-side heuristic
    # always sent a number here, so API consumers (incl. a future mobile app)
    # may still assume it's always numeric. It is genuinely null now whenever
    # no real pricing decision scored the quote, or a stale one was superseded
    # (see supersede_if_price_changed) — never faked back to a number.
    win_probability = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True, allow_null=True)
    # Additive, list + detail: margin % against the full cost floor from the
    # stored pricing decision (null when the quote has none). The viewset
    # select_related's the decision, so this costs no extra query.
    pricing_margin_pct = serializers.SerializerMethodField()

    def get_pricing_margin_pct(self, obj):
        try:
            d = obj.pricing_decision
        except Exception:
            return None
        # A superseded decision was for an earlier price: no margin from it.
        if d.superseded_at is not None:
            return None
        price, floor = d.final_price, d.floor
        if not price or floor is None or price <= 0:
            return None
        from core.services.pricing_analysis import pct_half_up
        return pct_half_up(float(price - floor), float(price))   # half away from zero, as the panel

    class Meta:
        model = Quote
        fields = '__all__'
        # 'company' read-only (2026-09): a PATCH could move a quote into
        # another tenant. Create paths set it server-side via save(company=).
        # 'win_probability' read-only (pricing analysis): the server sets it
        # from pricing_decision — the model likelihood at the FINAL price when
        # a real model priced the quote, else null. A client-sent figure (the
        # old flow sent the heuristic at a price the operator never saw) is
        # ignored, not rejected, so older clients keep working.
        read_only_fields = ['id', 'company', 'created_at', 'updated_at', 'created_by', 'win_probability',
                            # Pricing snapshot (QUOTE-RULES.md §9): server-set on save.
                            'fuel_price_used', 'fuel_price_source', 'fuel_zone', 'fuel_effective_from',
                            'fuel_official_at_pricing', 'fuel_litres', 'priced_at', 'priced_vehicle_type',
                            'empty_return_included', 'cost_floor', 'costing_snapshot',
                            # Only the outcome flow (record outcome / accept /
                            # decline) sets it: a client-sent outcome is ignored,
                            # so a PATCH cannot fake model / market evidence.
                            'outcome']

    PRICING_DECISION_MAX_BYTES = 20_000

    def validate_pricing_decision(self, value):
        if value in (None, ''):
            return None
        if not isinstance(value, dict):
            raise serializers.ValidationError('pricing_decision must be a JSON object.')
        size = len(json.dumps(value, separators=(',', ':'), default=str).encode('utf-8'))
        if size > self.PRICING_DECISION_MAX_BYTES:
            raise serializers.ValidationError(
                f'pricing_decision is {size:,} bytes; the limit is {self.PRICING_DECISION_MAX_BYTES:,}.')
        picked = value.get('picked_choice')
        lines = value.get('floor_lines')
        if lines is not None and not isinstance(lines, list):
            raise serializers.ValidationError('floor_lines must be a list.')
        if picked not in (None, '', 'safe', 'balanced', 'stretch', 'custom'):
            raise serializers.ValidationError('picked_choice must be safe, balanced, stretch or custom.')
        level = value.get('likelihood_level')
        if level not in (None, '', 'model', 'rules'):
            raise serializers.ValidationError('likelihood_level must be model or rules.')
        for key in ('final_price', 'floor', 'likelihood_at_final_pct', 'price_adjustment'):
            v = value.get(key)
            if v is not None:
                try:
                    float(v)
                except (TypeError, ValueError):
                    raise serializers.ValidationError(f'{key} must be a number.')
        return value

    def validate_costing_inputs(self, value):
        """Only the documented keys (core.services.quote_costing
        COSTING_INPUT_KEYS), coerced to their types; null drops a key."""
        from core.services.quote_costing import COSTING_INPUT_KEYS
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('costing_inputs must be a JSON object.')
        unknown = sorted(set(value) - set(COSTING_INPUT_KEYS))
        if unknown:
            raise serializers.ValidationError(f'Unknown costing_inputs keys: {", ".join(unknown)}.')
        out = {}
        for key, kind in COSTING_INPUT_KEYS.items():
            v = value.get(key)
            if v is None or v == '':
                continue
            try:
                if kind is bool:
                    if not isinstance(v, bool):
                        raise ValueError
                    out[key] = v
                else:
                    num = float(v)
                    if num != num or num < 0:
                        raise ValueError
                    if key == 'fuel_price_override' and not (5 <= num <= 100):
                        raise serializers.ValidationError('costing_inputs.fuel_price_override must be between '
                                                          'R5 and R100 per litre.')
                    out[key] = int(num) if kind is int else num
            except (TypeError, ValueError):
                raise serializers.ValidationError(f'costing_inputs.{key} must be '
                                                  + ('true or false.' if kind is bool else 'a number of 0 or more.'))
        return out

    def _snapshot(self, instance, validated_data, created):
        from core.services.quote_snapshot import snapshot_quote
        snapshot_quote(instance)

    @staticmethod
    def _pricing_changed(instance, validated_data):
        """Pricing fields whose value actually changes (M2: a PATCH echoing
        the same values does not re-price)."""
        from core.services.quote_snapshot import PRICING_FIELDS
        out = set()
        for k in PRICING_FIELDS & set(validated_data):
            old = getattr(instance, k, None)
            new = validated_data[k]
            try:
                same = (old == new) or (old is not None and new is not None and float(old) == float(new))
            except (TypeError, ValueError):
                same = old == new
            if not same:
                out.add(k)
        return out

    # costing_inputs keys that restate a quote field; stale once that field changes.
    CONFLICTING_INPUTS = {
        'toll_charges': ('toll_cost_one_way', 'tolls_unknown'),
        'vehicle_type': ('vehicle_type_id',),
        'estimated_duration_minutes': ('duration_minutes',),
        'distance': ('distance_estimated', 'distance_confirmed', 'duration_minutes', 'toll_cost_one_way'),
        'pickup_location': ('distance_estimated', 'distance_confirmed', 'tolls_unknown', 'toll_cost_one_way'),
        'delivery_location': ('distance_estimated', 'distance_confirmed', 'tolls_unknown', 'toll_cost_one_way'),
    }

    def _prune_costing_inputs(self, instance, validated_data, changed):
        """H4: when pricing fields change and the client didn't send
        costing_inputs, drop the stored keys those fields make stale."""
        if 'costing_inputs' in validated_data or not changed:
            return
        ci = dict(getattr(instance, 'costing_inputs', None) or {})
        drop = {k for f in changed for k in self.CONFLICTING_INPUTS.get(f, ())}
        if drop & set(ci):
            validated_data['costing_inputs'] = {k: v for k, v in ci.items() if k not in drop}

    def create(self, validated_data):
        from django.db import transaction
        decision = validated_data.pop('pricing_decision', None)
        # Score the final price FIRST, outside the write transaction (no
        # SQLite write lock held while the model runs)...
        scored = self._score_decision(None, validated_data, decision) if decision else None
        # ...then one transaction: a quote saved with a decision either stores
        # both or neither — never a 201 that claims a decision the DB doesn't hold.
        with transaction.atomic():
            instance = super().create(validated_data)
            if decision:
                self._save_pricing_decision(instance, decision, scored)
            self._snapshot(instance, validated_data, created=True)
            if instance.status == 'SENT':
                # Created straight as SENT: the same guard as every other send
                # (the pre_save signal guards transitions of saved quotes);
                # raising rolls the whole create back.
                from core.services.quote_snapshot import enforce_send_guard
                enforce_send_guard(instance)
        return instance

    def update(self, instance, validated_data):
        from django.db import transaction
        decision = validated_data.pop('pricing_decision', None)
        scored = self._score_decision(instance, validated_data, decision) if decision else None
        changed = self._pricing_changed(instance, validated_data)
        self._prune_costing_inputs(instance, validated_data, changed)
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if decision:
                self._save_pricing_decision(instance, decision, scored)
            if changed:
                self._snapshot(instance, validated_data, created=False)
        return instance

    def _request_user(self):
        return getattr(self.context.get('request'), 'user', None)

    def _score_decision(self, instance, validated_data, decision):
        from core.services.pricing_decisions import quote_fields, score_final_price
        company = validated_data.get('company') or getattr(instance, 'company', None) \
            or getattr(self._request_user(), 'company', None)
        return score_final_price(quote_fields(instance, validated_data), decision,
                                 company=company, user=self._request_user())

    def _save_pricing_decision(self, quote, decision, scored=None):
        from core.services.pricing_decisions import save_pricing_decision
        save_pricing_decision(quote, decision, user=self._request_user(), scored=scored)

    def _first_load(self, obj):
        # .all() so a prefetch_related('loads') on the viewset serves it.
        loads = list(obj.loads.all())
        return min(loads, key=lambda l: l.pk) if loads else None

    def get_booked_load(self, obj):
        load = self._first_load(obj)
        return {'id': load.id, 'load_number': load.load_number, 'status': load.status} if load else None

    def get_customer_price(self, obj):
        from core.services.quote_vat import public_fields, quote_vat
        return public_fields(quote_vat(obj))

    def get_converted(self, obj):
        # Legacy IT/COMPLETED rows predate convert_to_load and count as converted.
        return self._first_load(obj) is not None or obj.status in ('IT', 'COMPLETED')

    def validate(self, attrs):
        # Safety net behind the frontend's own capacity check (QuoteBuilder's
        # weightBlockedMessage) — a load can't legally exceed the selected
        # vehicle type's rated capacity, so reject it here too rather than
        # trust every caller to have checked client-side. Quote.vehicle_type
        # is a plain name string (no FK), same lookup the frontend already
        # does by name+company.
        vt_name = attrs.get('vehicle_type', getattr(self.instance, 'vehicle_type', None))
        weight_kg = attrs.get('weight', getattr(self.instance, 'weight', None))
        if vt_name and weight_kg:
            request = self.context.get('request')
            company = getattr(getattr(request, 'user', None), 'company', None)
            vt = VehicleType.objects.filter(name=vt_name, company=company).first() if company else None
            from core.services.quote_costing import capacity_tonnes
            cap_t = capacity_tonnes(vt.capacity) if vt else None   # values > 100 are kg (QUOTE-RULES §3)
            if vt and cap_t and float(weight_kg) > cap_t * 1000:
                raise serializers.ValidationError({
                    'weight': f"{float(weight_kg) / 1000:.1f}t exceeds the {vt_name}'s rated capacity "
                              f"of {cap_t:g}t — this can't be priced as a standard quote."
                })
        return attrs

    # route_snapshot is the raw route request+response kept for ML training.
    # Client-supplied JSON: capped in size, and left out of list responses
    # (it can be large and holds coordinates/addresses).
    ROUTE_SNAPSHOT_MAX_BYTES = 200_000

    def validate_route_snapshot(self, value):
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('route_snapshot must be a JSON object.')
        size = len(json.dumps(value, separators=(',', ':'), default=str).encode('utf-8'))
        if size > self.ROUTE_SNAPSHOT_MAX_BYTES:
            raise serializers.ValidationError(
                f'route_snapshot is {size:,} bytes; the limit is {self.ROUTE_SNAPSHOT_MAX_BYTES:,}.')
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if isinstance(self.parent, serializers.ListSerializer):
            data.pop('route_snapshot', None)
        else:
            # Detail only (one extra query each; never on list pages).
            from core.services.pricing_decisions import (agreed_price_representation, decision_representation,
                                                         loss_reason_representation)
            data['pricing_decision'] = decision_representation(instance)
            data['loss_reason'] = loss_reason_representation(instance)
            # Read-only, display only: the price agreed when the quote was won
            # at a figure other than its total (billing is unchanged).
            data['agreed_price'] = agreed_price_representation(instance)
            # R5 (K-8): the margin at the agreed price, against the cost floor
            # stored with the pricing decision (null when either is missing).
            from core.services.pricing_decisions import agreed_margin_representation
            data.update(agreed_margin_representation(data['agreed_price'], data['pricing_decision']))
        return data

    def _converted_load(self, obj):
        # Assignment happens on the Load this quote was converted into (at
        # conversion time or later from the Bookings page) — nothing syncs it
        # back onto the quote's own vehicle/driver fields, so look there first.
        # A quote converts to at most one load (convert_to_load blocks a
        # second conversion), so the most recent is unambiguous. Not cached on
        # self — this serializer instance is reused across every item when
        # DRF serializes a list, so instance-level caching would leak the
        # first quote's load onto every other quote in the list.
        return obj.loads.select_related('vehicle', 'driver__user').order_by('-id').first()

    def get_vehicle_display(self, obj):
        load = self._converted_load(obj)
        vehicle = (load.vehicle if load else None) or obj.vehicle
        if vehicle:
            return f"{vehicle.make} {vehicle.model} ({vehicle.plate})"
        return None

    def get_driver_display(self, obj):
        load = self._converted_load(obj)
        driver = (load.driver if load else None) or obj.driver
        if driver:
            u = driver.user
            name = f"{u.first_name} {u.last_name}".strip() or u.username
            return name
        return None


# Invoice Serializer
class InvoiceLineSerializer(serializers.ModelSerializer):
    # How much of this line (excl. VAT) issued credit notes have reversed,
    # so a partial-credit UI can cap each line without guessing.
    credited_net_amount = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceLine
        fields = ['id', 'position', 'description', 'quantity', 'unit_price', 'discount_amount',
                  'discount_percent', 'tax_code', 'revenue_type', 'tax_rate', 'net_amount', 'vat_amount',
                  'total_amount', 'load', 'credited_net_amount']
        read_only_fields = fields

    def get_credited_net_amount(self, obj):
        from django.db.models import Sum
        from decimal import Decimal
        total = obj.credit_note_lines.filter(credit_note__status='ISSUED').aggregate(t=Sum('net_amount'))['t']
        return str(Decimal(total or 0).quantize(Decimal('0.01')))


class InvoiceSerializer(CompanyScopedRelationsMixin, serializers.ModelSerializer):
    """Invoices. Totals are computed from `lines` server-side
    (core.services.invoice_lines); a client never sends money totals.

    Write `lines`: [{description, quantity, unit_price, discount_amount |
    discount_percent, tax_code, load?}]. Older clients that post
    subtotal / vat_amount / line_items are translated to lines once.

    Issued (non-draft) invoices are locked: only `notes` may change, and not
    even that once the invoice is financed. Corrections are credit notes.
    """
    company_scoped_relations = {
        'customer': 'company_id', 'load': 'company_id', 'trip': 'load__company_id',
    }
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    # Lets a client offer "share this invoice on WhatsApp" without a second
    # round-trip to the customer endpoint just for the number.
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    load_number = serializers.CharField(source='load.load_number', read_only=True)
    # The 0.25% take-rate charge on this invoice, if any — lets the operator
    # see directly on the invoice whether/when the platform fee was taken.
    # SerializerMethodField (not a plain nested serializer): the reverse
    # OneToOneField raises DoesNotExist for any invoice with no charge yet
    # (drafts, pre-this-feature invoices) — getattr's default swallows that.
    delivery_fee_charge = serializers.SerializerMethodField()
    lines = InvoiceLineSerializer(many=True, read_only=True)
    is_locked = serializers.BooleanField(read_only=True)
    is_financed = serializers.SerializerMethodField()
    lock_reason = serializers.SerializerMethodField()
    has_provisional_number = serializers.BooleanField(read_only=True)
    credit_notes = serializers.SerializerMethodField()
    # Where this invoice is in Xero / QuickBooks (null when not connected).
    accounting_sync = serializers.SerializerMethodField()

    def get_accounting_sync(self, obj):
        from core.accounting.presenters import accounting_sync
        return accounting_sync(obj, 'INVOICE', self.context)

    # Statuses a client may create an invoice in ("Save as" on New invoice).
    # Every later change goes through an action (send, record payment), so a
    # PATCH can't mark an invoice paid with no payment behind it.
    CREATE_STATUSES = ('DRAFT', 'SENT')
    # What may still change on an issued invoice.
    LOCKED_EDITABLE = ('notes', 'early_pay_offered')

    class Meta:
        model = Invoice
        fields = '__all__'
        # Money and payment state are server-derived: totals from the lines,
        # paid/credited/balance from the ledger (services.ledger), so the
        # payment ledger and every revenue figure stay in step.
        # subtotal/discount stay writable as INPUT for older clients and the
        # Copilot (they become one line via legacy_payload_to_lines); the
        # stored values are always recomputed from the lines.
        read_only_fields = ['id', 'company', 'created_at', 'updated_at', 'invoice_number',
                            'vat_amount', 'tax_amount', 'tax_rate',
                            'total_amount', 'paid_amount', 'credited_amount', 'balance',
                            'paid_at', 'sent_at', 'viewed_at', 'totals_source', 'terms_days',
                            'voided_at', 'void_reason', 'line_items', 'pdf_file', 'view_token',
                            'last_reminder_at', 'reminder_count']
        extra_kwargs = {
            'due_date': {'required': False},
            'payment_terms': {'required': False},
            'subtotal': {'required': False},
            'discount': {'required': False},
        }

    def get_is_financed(self, obj):
        return obj.is_financed

    def get_lock_reason(self, obj):
        if obj.is_financed:
            return 'Financed through Fast Pay: the invoice is locked. Contact the capital desk.'
        if obj.status == 'CANCELLED':
            return 'This invoice is void.'
        if obj.is_locked:
            return 'Issued invoices can\'t be edited. Issue a credit note to correct it.'
        return None

    def get_credit_notes(self, obj):
        if not obj.pk:
            return []
        return [{'id': cn.id, 'credit_note_number': cn.credit_note_number,
                 'issue_date': cn.issue_date, 'total_amount': str(cn.total_amount), 'status': cn.status}
                for cn in obj.credit_notes.all()]

    def validate_status(self, value):
        if self.instance is None:
            if value not in self.CREATE_STATUSES:
                raise serializers.ValidationError('A new invoice is saved as a draft or sent.')
        elif value != self.instance.status:
            raise serializers.ValidationError(
                'Change an invoice\'s status with its actions (send, record payment, void), not by editing it.')
        return value

    def _locked_error(self, message):
        err = serializers.ValidationError({'error': message, 'code': 'invoice_locked'})
        return err

    def validate(self, attrs):
        attrs = super().validate(attrs)
        inst = self.instance
        raw = getattr(self, 'initial_data', {}) or {}
        if inst is not None and inst.is_locked:
            changed = {k for k, v in attrs.items() if getattr(inst, k, None) != v}
            # Totals are read-only (DRF would silently drop them); a client
            # trying to change one on an issued invoice gets told why.
            for k in ('subtotal', 'vat_amount', 'tax_amount', 'discount', 'total_amount'):
                if k in raw and str(raw[k]) not in ('', 'None') and _dec_or_none(raw[k]) != getattr(inst, k):
                    changed.add(k)
            if 'lines' in raw or ('line_items' in raw and raw['line_items'] != inst.line_items):
                changed.add('lines')
            if inst.is_financed and changed:
                raise self._locked_error(self.get_lock_reason(inst))
            if changed - set(self.LOCKED_EDITABLE):
                raise self._locked_error(
                    'This invoice has been issued and can\'t be edited. Issue a credit note to correct it.')
        issue = attrs.get('issue_date') or (inst.issue_date if inst is not None else None)
        due = attrs.get('due_date')
        if due is not None and issue and due < issue:
            raise serializers.ValidationError({'due_date': 'The due date can\'t be before the issue date.'})
        return attrs

    def _raw_lines(self, company):
        from core.services.invoice_lines import legacy_payload_to_lines
        raw = getattr(self, 'initial_data', {}) or {}
        if 'lines' in raw:
            lines = raw.get('lines')
            if isinstance(lines, str):
                try:
                    lines = json.loads(lines)
                except ValueError:
                    raise serializers.ValidationError({'lines': 'Not valid JSON.'})
            return lines
        if any(k in raw for k in ('subtotal', 'line_items')):
            return legacy_payload_to_lines(raw, company)
        return None

    def _apply_terms(self, validated_data, customer, instance=None):
        from core.services.invoice_lines import terms_days_for, due_date_for, customer_terms
        terms = validated_data.get('payment_terms') or (instance.payment_terms if instance else None) \
            or customer_terms(customer)
        validated_data['payment_terms'] = terms
        validated_data['terms_days'] = terms_days_for(terms)
        issue = validated_data.get('issue_date') or (instance.issue_date if instance else None)
        if issue is None:
            from datetime import date as _date
            issue = _date.today()
            validated_data['issue_date'] = issue
        if 'due_date' not in validated_data or validated_data['due_date'] is None:
            if instance is None or 'payment_terms' in validated_data or 'issue_date' in validated_data:
                validated_data['due_date'] = due_date_for(issue, terms)

    def update(self, instance, validated_data):
        from django.db import transaction
        from core.services.invoice_lines import apply_lines, LineError
        old_due = instance.due_date
        if instance.status == 'DRAFT':
            self._apply_terms(validated_data, validated_data.get('customer') or instance.customer, instance)
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            if instance.status == 'DRAFT':
                raw_lines = self._raw_lines(instance.company)
                try:
                    if raw_lines is not None:
                        apply_lines(instance, raw_lines)
                    elif 'issue_date' in validated_data and instance.totals_source == 'LINES' and instance.lines.exists():
                        # Re-rate on the new date (a VAT rate change is dated).
                        apply_lines(instance, [self._line_as_raw(l) for l in instance.lines.all()])
                except LineError as e:
                    raise serializers.ValidationError({'lines': str(e)})
        if instance.due_date != old_due:
            # The due date drives overdue status, reminders and fast-pay, so a
            # change is recorded with both dates (the save signal only notes
            # that the invoice changed).
            from core.models import AuditLog
            request = self.context.get('request')
            try:
                AuditLog.log_update(instance, user=getattr(request, 'user', None),
                                    changes={'due_date': [old_due.isoformat(), instance.due_date.isoformat()]})
            except Exception:
                pass
        return instance

    @staticmethod
    def _line_as_raw(line):
        return {'description': line.description, 'quantity': line.quantity, 'unit_price': line.unit_price,
                'discount_amount': line.discount_amount if line.discount_percent is None else None,
                'discount_percent': line.discount_percent, 'tax_code': line.tax_code, 'load': line.load_id}

    def create(self, validated_data):
        from django.db import transaction
        from django.utils import timezone
        from core.services.invoice_lines import apply_lines, LineError
        from core.services.numbering import provisional_number

        company = validated_data.get('company')
        if company is None:
            request = self.context.get('request')
            company = getattr(getattr(request, 'user', None), 'company', None)
            validated_data['company'] = company
        raw_lines = self._raw_lines(company)
        if not raw_lines:
            raise serializers.ValidationError({'lines': 'Add at least one line.'})
        wanted_status = validated_data.pop('status', 'DRAFT') or 'DRAFT'
        self._apply_terms(validated_data, validated_data['customer'])
        validated_data.update(invoice_number=provisional_number(), status='DRAFT',
                              subtotal=0, vat_amount=0, total_amount=0, balance=0)
        with transaction.atomic():
            invoice = Invoice(**validated_data)
            try:
                apply_lines(invoice, raw_lines)
            except LineError as e:
                raise serializers.ValidationError({'lines': str(e)})
            if wanted_status == 'SENT':
                invoice.status = 'SENT'
                invoice.sent_at = timezone.now()
                invoice.save()
        return invoice

    def get_delivery_fee_charge(self, obj):
        charge = getattr(obj, 'delivery_fee_charge', None)
        return DeliveryFeeChargeSerializer(charge).data if charge else None


class CreditNoteLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditNoteLine
        fields = ['id', 'position', 'description', 'quantity', 'unit_price', 'tax_code', 'revenue_type',
                  'tax_rate', 'net_amount', 'vat_amount', 'total_amount', 'invoice_line']
        read_only_fields = fields


class CreditNoteSerializer(serializers.ModelSerializer):
    """Read shape; creation goes through core.services.credit_notes."""
    lines = CreditNoteLineSerializer(many=True, read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    accounting_sync = serializers.SerializerMethodField()

    class Meta:
        model = CreditNote
        fields = ['id', 'credit_note_number', 'invoice', 'invoice_number', 'customer', 'customer_name',
                  'issue_date', 'reason', 'status', 'lines', 'subtotal', 'vat_amount', 'total_amount',
                  'source', 'external_id', 'created_at', 'voided_at', 'void_reason', 'accounting_sync']
        read_only_fields = fields

    def get_accounting_sync(self, obj):
        from core.accounting.presenters import accounting_sync
        return accounting_sync(obj, 'CREDIT_NOTE', self.context)


class SupplierSerializer(serializers.ModelSerializer):
    expense_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Supplier
        fields = ['id', 'name', 'vat_number', 'registration_number', 'email', 'phone', 'category',
                  'is_active', 'expense_count', 'source', 'external_id', 'created_at', 'updated_at']
        read_only_fields = ['id', 'expense_count', 'source', 'external_id', 'created_at', 'updated_at']

    def validate_name(self, value):
        from core.services.identity import legal_name_key
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A supplier name is required.')
        key = legal_name_key(value)
        company_id = self.instance.company_id if self.instance else getattr(
            getattr(self.context.get('request'), 'user', None), 'company_id', None)
        dupe = Supplier.objects.filter(company_id=company_id, name_key=key)
        if self.instance:
            dupe = dupe.exclude(pk=self.instance.pk)
        if dupe.exists():
            raise serializers.ValidationError('A supplier with this name already exists.')
        return value

    def validate_vat_number(self, value):
        return _validate_identifier(value, 'vat')

    def validate_registration_number(self, value):
        return _validate_identifier(value, 'reg')

    def validate_category(self, value):
        if value and value not in dict(Expense.CATEGORY_CHOICES):
            raise serializers.ValidationError('Unknown category.')
        return value


def _dec_or_none(value):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _validate_identifier(value, kind, country='ZA'):
    from core.services.identity import normalise_vat_number, normalise_registration_number
    try:
        if kind == 'vat':
            return normalise_vat_number(value, country)
        return normalise_registration_number(value, country)
    except ValueError as e:
        raise serializers.ValidationError(str(e))


# Payment Serializer
class PaymentSerializer(CompanyScopedRelationsMixin, serializers.ModelSerializer):
    company_scoped_relations = {'invoice': 'company_id', 'customer': 'company_id'}
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    
    class Meta:
        model = Payment
        fields = '__all__'
        # 'company' read-only (2026-09): it was writable on PATCH. Create
        # paths (services.payments.record_payment, CompanyFilterMixin) set it
        # server-side via save(company=).
        read_only_fields = ['id', 'company', 'created_at', 'updated_at', 'source', 'external_id']


# Expense Serializer
class ExpenseSerializer(CompanyScopedRelationsMixin, serializers.ModelSerializer):
    """Expenses. `amount` is GROSS (incl. VAT, as on the receipt);
    `vat_amount` is the input VAT inside it; `net_amount` = amount - VAT is
    the cost every report uses. If vat_amount isn't sent it is derived from
    the tax code (15/115 of the gross for STANDARD)."""
    company_scoped_relations = {
        'supplier': 'company_id', 'load': 'company_id', 'trip': 'load__company_id',
    }
    vehicle_info = serializers.CharField(source='vehicle.__str__', read_only=True)
    driver_name = serializers.CharField(source='driver.user.username', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    net_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Expense
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by', 'company']
        extra_kwargs = {'expense_number': {'required': False},
                        'tax_code': {'required': False},
                        'vat_amount': {'required': False}}

    def _company(self):
        if self.instance is not None:
            return self.instance.company
        request = self.context.get('request')
        return getattr(getattr(request, 'user', None), 'company', None)

    def validate(self, attrs):
        from core import tax_codes
        from core.services.invoice_lines import default_tax_code
        attrs = super().validate(attrs)
        inst = self.instance
        company = self._company()
        amount = attrs.get('amount', inst.amount if inst else None)
        supplier = attrs.get('supplier', inst.supplier if inst else None)
        category = attrs.get('category', inst.category if inst else None)
        code = attrs.get('tax_code')
        if code is None and inst is None:
            if company is not None and not getattr(company, 'vat_registered', True):
                code = tax_codes.NO_VAT  # a non-vendor can't claim input VAT
            elif supplier is not None and not supplier.vat_number:
                code = tax_codes.NO_VAT  # no VAT number, no valid tax invoice
            elif category == 'FUEL':
                code = tax_codes.ZERO_RATED  # diesel and petrol are zero-rated (s11(1)(k))
            else:
                code = default_tax_code(company)
            attrs['tax_code'] = code
        code = attrs.get('tax_code', inst.tax_code if inst else tax_codes.NO_VAT)
        if company is not None and not getattr(company, 'vat_registered', True) and code != tax_codes.NO_VAT:
            raise serializers.ValidationError({'tax_code': 'This company is not VAT registered, so no input VAT can be claimed.'})
        if amount is not None:
            if 'vat_amount' in attrs and attrs['vat_amount'] is not None:
                vat = attrs['vat_amount']
                if code != tax_codes.STANDARD and vat != 0:
                    raise serializers.ValidationError({'vat_amount': f'{code} expenses carry no VAT.'})
            elif 'amount' in attrs or 'tax_code' in attrs or inst is None:
                vat = tax_codes.vat_fraction_of_gross(amount, code)
                attrs['vat_amount'] = vat
            else:
                vat = inst.vat_amount
            if vat < 0 or vat > amount:
                raise serializers.ValidationError({'vat_amount': 'VAT must be between zero and the amount.'})
        if supplier is not None and not attrs.get('vendor') and (inst is None or not inst.vendor):
            attrs['vendor'] = supplier.name[:200]
        return attrs

    def create(self, validated_data):
        # Auto-generate a unique expense_number if the client didn't supply one.
        if not validated_data.get('expense_number'):
            import random
            from django.utils import timezone
            ts = timezone.now().strftime('%Y%m%d')
            num = f"EXP-{ts}-{random.randint(1000, 9999)}"
            while Expense.objects.filter(expense_number=num).exists():
                num = f"EXP-{ts}-{random.randint(1000, 9999)}"
            validated_data['expense_number'] = num
        return super().create(validated_data)


# Settlement Serializer
class SettlementSerializer(serializers.ModelSerializer):
    driver_name = serializers.CharField(source='driver.user.username', read_only=True)
    
    class Meta:
        model = Settlement
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


# Notification Serializer
class NotificationSerializer(serializers.ModelSerializer):
    description = serializers.CharField(source='message')
    unread = serializers.BooleanField(source='is_read', read_only=True)
    
    class Meta:
        model = Notification
        fields = ['id', 'title', 'description', 'type', 'unread', 'link', 'created_at']
        read_only_fields = ['id', 'created_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['unread'] = not instance.is_read
        data['type'] = instance.type.lower()
        return data


# Company Serializer
BANK_FIELDS = (
    'bank_name', 'bank_account_holder', 'bank_account_number',
    'bank_branch_code', 'bank_account_type', 'payment_reference_hint',
)


def _is_live_echo(value, zone='INLAND'):
    """True when an old client's fuel_price_per_litre WRITE is not a price the
    fleet chose: empty, the 23.50 factory default, or within half a cent of
    an official 50ppm diesel price a client could have been shown recently —
    either zone (a zone change in the same save shows the other zone), in
    force now or in the previous period. Any other value (e.g. an old 500ppm
    figure) is OWN. Migration 0150 uses the wider historic rule for the
    one-off backfill. `zone` is kept for callers; both zones are checked."""
    if value is None:
        return True
    value = Decimal(str(value))
    if abs(value - Decimal('23.50')) <= Decimal('0.00001'):
        return True
    from datetime import timedelta
    from core.services.fuel_price import official_row_in_force, row_effective_from
    current = official_row_in_force()
    rows = [current]
    if current is not None:
        rows.append(official_row_in_force(row_effective_from(current) - timedelta(microseconds=1)))
    for row in rows:
        if row is None:
            continue
        for price in (row.diesel_inland, row.diesel_coastal):
            if price is not None and abs(value - price) <= Decimal('0.005'):
                return True
    return False


def _is_official_petrol(value):
    """True when `value` is (±0.005) an official petrol price (95 or 93,
    inland or coastal) in force now or in the period before."""
    from datetime import timedelta
    from core.services.fuel_price import PETROL_FIELDS, official_row_in_force, row_effective_from
    value = Decimal(str(value))
    for column in PETROL_FIELDS:
        current = official_row_in_force(column=column)
        rows = [current]
        if current is not None:
            rows.append(official_row_in_force(row_effective_from(current) - timedelta(microseconds=1),
                                              column=column))
        for row in rows:
            p = getattr(row, column, None) if row is not None else None
            if p is not None and abs(value - p) <= Decimal('0.005'):
                return True
    return False


# Plain, SA-format wording for every bounded company setting: the same
# sentence for an out-of-range value, a non-number, too many digits or
# decimals — never a DRF default ("Ensure this value is ...").
SETTINGS_MESSAGES = {
    'fuel_price_own': 'Enter a diesel price between R 5 and R 100 per litre, or leave it blank.',
    'fuel_price_per_litre': 'Enter a diesel price between R 5 and R 100 per litre, or leave it blank.',
    'fuel_price_petrol': 'Enter a petrol price between R 5 and R 100 per litre, or leave it blank.',
    'fuel_price_electric': 'Enter an electricity price above R 0 and up to R 20 per kWh, or leave it blank.',
    'fuel_price_hybrid': 'Enter a price above R 0 and up to R 100 per litre, or leave it blank.',
    'default_base_rate_per_km': 'Enter a default price per km between R 0 and R 1 000, or leave it blank.',
    'default_toll_rate_per_km': 'Enter a toll rate between R 0 and R 50 per km.',
    'minimum_charge': 'Enter a minimum charge between R 0 and R 5 000 000, or leave it blank.',
    'empty_return_min_km': 'Enter a distance between 0 and 5 000 km.',
    'operating_cost_per_km': 'Enter an operating cost between R 1 and R 200 per km, or leave it blank.',
    'driver_allowance_per_night': 'Enter a driver allowance between R 1 and R 5 000 per night, or leave it blank.',
    'margin_target_pct': 'Enter a target margin above 0% and below 100%.',
}
_SETTINGS_ERROR_KEYS = ('invalid', 'max_value', 'min_value', 'max_digits', 'max_decimal_places',
                        'max_whole_digits', 'max_string_length')


class CompanySerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Company
        fields = [
            'company_name', 'registration_number', 'vat_number',
            'industry', 'website', 'description', 'logo_url',
            'address', 'contact',
            'default_base_rate_per_km', 'default_sla_hours',
            'default_quote_validity_days', 'allow_cross_border', 'auto_email_invoices',
            'cross_border_crossings_per_year',
            'fuel_zone',
            'fuel_price_per_litre', 'fuel_price_petrol', 'fuel_price_electric', 'fuel_price_hybrid',
            # QUOTE-RULES.md §1 (additive): LIVE/OWN diesel and the price a
            # quote would use right now (read-only, resolved server-side).
            'fuel_price_mode', 'fuel_price_own', 'fuel_price_own_set_at', 'diesel_price_in_use',
            # Petrol (and hybrid trucks), same rule as diesel: LIVE/OWN, own
            # price = fuel_price_petrol, official grade 95 (93 inland option).
            'fuel_price_petrol_mode', 'fuel_price_petrol_set_at', 'fuel_price_petrol_grade',
            'petrol_price_in_use',
            # QUOTE-RULES.md §5/§6 (additive).
            'include_empty_return_default', 'empty_return_min_km', 'minimum_charge',
            'margin_at_risk_pct', 'margin_caution_pct', 'margin_target_pct',
            'ai_optimizer_min_margin_pct', 'ai_optimizer_min_win_probability_pct',
            'ai_optimizer_max_market_deviation_pct',
            'default_toll_rate_per_km',
            # Pricing analysis (additive): empty-return default and global-model opt-in.
            'pricing_include_empty_return', 'pool_pricing_data', 'operating_cost_per_km',
            # Round 4 (additive): allowance per night (used when no approved
            # allowance is on record) and the operating cost the pricing
            # analysis is using right now (read-only).
            'driver_allowance_per_night', 'operating_cost_in_use',
            # Round 5 (read-only): the target margin range the analysis uses (it clamps to it).
            'margin_target_range',
            'onboarding_completed_at',
        ] + list(BANK_FIELDS)

    operating_cost_in_use = serializers.SerializerMethodField()
    margin_target_range = serializers.SerializerMethodField()
    diesel_price_in_use = serializers.SerializerMethodField()
    petrol_price_in_use = serializers.SerializerMethodField()
    fuel_price_petrol_set_at = serializers.DateTimeField(read_only=True)
    fuel_price_petrol_mode = serializers.ChoiceField(choices=Company.FUEL_PRICE_MODE_CHOICES, required=False)
    fuel_price_own = serializers.DecimalField(max_digits=8, decimal_places=4, required=False, allow_null=True)
    fuel_price_own_set_at = serializers.DateTimeField(read_only=True)
    # Old clients still write it; see validate() for how a write is read.
    fuel_price_per_litre = serializers.DecimalField(max_digits=8, decimal_places=4, required=False,
                                                    allow_null=True)

    def get_petrol_price_in_use(self, obj):
        """core.services.fuel_price.resolve_company_petrol: {fuel_type,
        grade, mode, source, price, zone, official{...}, own{...}, warnings}.
        Never raises."""
        try:
            from core.services.fuel_price import resolve_company_petrol
            out = resolve_company_petrol(obj)
            out.pop('input', None)
            return out
        except Exception:
            return None

    def get_diesel_price_in_use(self, obj):
        """core.services.fuel_price.resolve_company_diesel: {mode, source,
        price, zone, official{...}, own{...}, warnings}. Never raises."""
        try:
            from core.services.fuel_price import resolve_company_diesel
            out = resolve_company_diesel(obj)
            out.pop('input', None)
            return out
        except Exception:
            return None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Deprecated mirror for old clients: OWN -> own price, LIVE -> the
        # official zone price in force (null when there is none). Never the
        # 23.50 factory default.
        in_use = data.get('diesel_price_in_use') or {}
        if (instance.fuel_price_mode or 'LIVE') == 'OWN' and instance.fuel_price_own is not None:
            mirror = instance.fuel_price_own
        else:
            mirror = (in_use.get('official') or {}).get('price')
        data['fuel_price_per_litre'] = (str(Decimal(str(mirror)).quantize(Decimal('0.0001')))
                                        if mirror is not None else None)
        return data

    def validate_fuel_price_own(self, value):
        if self._unchanged('fuel_price_own', value):
            return value
        if value is not None and not (Decimal('5') <= value <= Decimal('100')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['fuel_price_own'])
        return value

    def _unchanged(self, field, value):
        """An old client echoing a stored value back: never rejected, so a
        company whose existing value is outside the new range is not locked
        out of saving its other settings."""
        if self.instance is None or value is None:
            return False
        stored = getattr(self.instance, field, None)
        try:
            return stored is not None and Decimal(str(stored)) == Decimal(str(value))
        except Exception:
            return False

    def validate_minimum_charge(self, value):
        if self._unchanged('minimum_charge', value):
            return value
        if value is not None and not (Decimal('0') <= value <= Decimal('5000000')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['minimum_charge'])
        return value or None

    def validate_fuel_price_electric(self, value):
        if self._unchanged('fuel_price_electric', value):
            return value
        if value is not None and not (Decimal('0') < value <= Decimal('20')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['fuel_price_electric'])
        return value

    def validate_fuel_price_hybrid(self, value):
        # Hybrid trucks price on the PETROL setting; this old field is only
        # stored for old screens (QUOTE_RULES_DEPLOY.md).
        if self._unchanged('fuel_price_hybrid', value):
            return value
        if value is not None and not (Decimal('0') < value <= Decimal('100')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['fuel_price_hybrid'])
        return value

    def validate_default_base_rate_per_km(self, value):
        if self._unchanged('default_base_rate_per_km', value):
            return value
        if value is not None and not (Decimal('0') <= value <= Decimal('1000')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['default_base_rate_per_km'])
        return value

    def validate_empty_return_min_km(self, value):
        if self._unchanged('empty_return_min_km', value):
            return value
        if value is not None and not (Decimal('0') <= value <= Decimal('5000')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['empty_return_min_km'])
        return value

    def validate(self, attrs):
        """Diesel mode (QUOTE-RULES.md §1). New clients send fuel_price_mode /
        fuel_price_own: own empty => LIVE. Old clients only send
        fuel_price_per_litre: 23.50, empty, or (±0.005) any official price on
        record is the live price echoed back => LIVE, own price untouched;
        anything else is a price the fleet typed => OWN at that value."""
        from django.utils import timezone as dj_tz
        attrs = super().validate(attrs)
        legacy = attrs.pop('fuel_price_per_litre', serializers.empty)
        instance = self.instance
        if 'fuel_price_mode' in attrs or 'fuel_price_own' in attrs:
            own = attrs.get('fuel_price_own', getattr(instance, 'fuel_price_own', None))
            mode = attrs.get('fuel_price_mode') or ('OWN' if 'fuel_price_own' in attrs and own is not None
                                                    else getattr(instance, 'fuel_price_mode', 'LIVE'))
            if own is None:
                mode = 'LIVE'
            attrs['fuel_price_mode'] = mode
        elif legacy is not serializers.empty:
            current_own = getattr(instance, 'fuel_price_own', None)
            unchanged = (legacy is not None and current_own is not None
                         and abs(Decimal(str(legacy)) - current_own) <= Decimal('0.00001'))
            zone_change = ('fuel_zone' in attrs and instance is not None
                           and attrs['fuel_zone'] != getattr(instance, 'fuel_zone', None))
            echo = _is_live_echo(legacy)
            # A stored value echoed back (own or official) always passes, so an
            # existing company is never locked out by the new range.
            if legacy is not None and not unchanged and not echo \
                    and not (Decimal('5') <= Decimal(str(legacy)) <= Decimal('100')):
                raise serializers.ValidationError(
                    {'fuel_price_per_litre': SETTINGS_MESSAGES['fuel_price_own']})
            if unchanged:
                pass   # the old client echoed the own price back: nothing changed
            elif echo and zone_change:
                pass   # the official price echoed with a zone change: never flips OWN -> LIVE
            elif echo:
                attrs['fuel_price_mode'] = 'LIVE'   # live price / factory default echoed back (own kept)
            else:
                attrs['fuel_price_mode'] = 'OWN'
                attrs['fuel_price_own'] = legacy
        if 'fuel_price_own' in attrs and attrs['fuel_price_own'] != getattr(instance, 'fuel_price_own', None):
            attrs['fuel_price_own_set_at'] = dj_tz.now() if attrs['fuel_price_own'] is not None else None
        if attrs.get('fuel_price_mode') == 'OWN' or (attrs.get('fuel_price_mode') is None
                                                     and getattr(instance, 'fuel_price_mode', 'LIVE') == 'OWN'):
            own = attrs.get('fuel_price_own', getattr(instance, 'fuel_price_own', None))
            if own is not None:
                attrs['fuel_price_per_litre'] = own   # keep the stored mirror honest
        self._validate_petrol(attrs, instance)
        # Empty-return default: the new field and the old pricing_include_empty_return mirror each other.
        if 'include_empty_return_default' in attrs:
            attrs['pricing_include_empty_return'] = attrs['include_empty_return_default']
        elif 'pricing_include_empty_return' in attrs:
            # An old client saving its whole settings form echoes the old
            # toggle back; only a real change of it is a change of the default.
            if attrs['pricing_include_empty_return'] != getattr(instance, 'pricing_include_empty_return', None):
                attrs['include_empty_return_default'] = attrs['pricing_include_empty_return']
            else:
                attrs.pop('pricing_include_empty_return')
        return attrs

    def _validate_petrol(self, attrs, instance):
        """Petrol mode, same rule as diesel (QUOTE-RULES.md §1). New clients
        send fuel_price_petrol_mode (+ fuel_price_petrol): own empty => LIVE.
        Old clients only send fuel_price_petrol: unchanged => nothing; a value
        echoing the official 95/93 price => not stored (never the official
        figure in the own field), mode unchanged; empty => LIVE; anything
        else => OWN at that value."""
        from django.utils import timezone as dj_tz
        current_own = getattr(instance, 'fuel_price_petrol', None)
        if 'fuel_price_petrol_mode' in attrs:
            own = attrs.get('fuel_price_petrol', current_own)
            if own is None:
                attrs['fuel_price_petrol_mode'] = 'LIVE'
        elif 'fuel_price_petrol' in attrs:
            value = attrs['fuel_price_petrol']
            if value is None:
                attrs['fuel_price_petrol_mode'] = 'LIVE'
            elif current_own is not None and abs(Decimal(str(value)) - current_own) <= Decimal('0.00001'):
                attrs.pop('fuel_price_petrol')      # echoed back unchanged
            elif _is_official_petrol(value):
                attrs.pop('fuel_price_petrol')      # the official price echoed back: not an own price
            else:
                attrs['fuel_price_petrol_mode'] = 'OWN'
        if 'fuel_price_petrol' in attrs and attrs['fuel_price_petrol'] != current_own:
            attrs['fuel_price_petrol_set_at'] = dj_tz.now() if attrs['fuel_price_petrol'] is not None else None

    def validate_fuel_price_petrol(self, value):
        if value is None or value <= 0:
            return None                      # empty / 0 = no own price (LIVE)
        if self.instance is not None and value == getattr(self.instance, 'fuel_price_petrol', None):
            return value                     # an old client echoing a stored value back
        if not (Decimal('5') <= value <= Decimal('100')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['fuel_price_petrol'])
        return value

    def get_margin_target_range(self, obj):
        from core.services.pricing_analysis import MARGIN_TARGET_RANGE
        return list(MARGIN_TARGET_RANGE)

    def get_operating_cost_in_use(self, obj):
        """{value, source: setting|company_actuals|vehicle_default, trips,
        window, label} — what pricing analysis uses for operating cost per km
        right now (company actuals are cached 10 minutes). Never raises."""
        try:
            from core.services.pricing_analysis import operating_cost_in_use
            return operating_cost_in_use(obj)
        except Exception:
            return None

    def validate_default_toll_rate_per_km(self, value):
        if self._unchanged('default_toll_rate_per_km', value):
            return value
        if value is not None and not (Decimal('0') <= value <= Decimal('50')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['default_toll_rate_per_km'])
        return value

    def validate_margin_target_pct(self, value):
        # Only nonsense is refused (a margin on price can't reach 100%), so a
        # company already storing an unusual figure can still save its
        # profile; the pricing analysis itself clamps the target to 1–40%.
        if value is not None and not (Decimal('0') < value < Decimal('100')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['margin_target_pct'])
        return value

    def validate_driver_allowance_per_night(self, value):
        if self._unchanged('driver_allowance_per_night', value):
            return value
        if value is not None and not (Decimal('1') <= value <= Decimal('5000')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['driver_allowance_per_night'])
        return value

    def validate_operating_cost_per_km(self, value):
        # Blank clears it (back to the figure from expenses); otherwise a
        # plausible R/km, so a typo can't price every quote at R0.10 or R10 000/km.
        if self._unchanged('operating_cost_per_km', value):
            return value
        if value is not None and not (Decimal('1') <= value <= Decimal('200')):
            raise serializers.ValidationError(SETTINGS_MESSAGES['operating_cost_per_km'])
        return value

    def get_fields(self):
        # Banking details follow the company-edit permission: only a company
        # ADMIN (the role CompanyProfileView itself requires) may change them.
        # Defence in depth — if this serializer is ever reached by anyone else
        # the bank fields come back read-only instead of silently writable.
        fields = super().get_fields()
        for name, message in SETTINGS_MESSAGES.items():
            f = fields.get(name)
            if f is not None and not f.read_only:
                f.error_messages.update({k: message for k in _SETTINGS_ERROR_KEYS})
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        can_edit = bool(user and getattr(user, 'is_authenticated', False) and (
            getattr(user, 'role', None) == 'ADMIN' or getattr(user, 'is_superuser', False)
        ))
        if not can_edit:
            for name in BANK_FIELDS:
                if name in fields:
                    fields[name].read_only = True
        return fields

    # Light validation: spaces/hyphens people paste from banking apps are
    # stripped, then the value must be digits of a sane length. Blank/null
    # clears the field. Existing rows are never re-validated.
    @staticmethod
    def _digits(value, label, min_len, max_len):
        if value in (None, ''):
            return None
        cleaned = re.sub(r'[\s-]', '', str(value))
        if not cleaned:
            return None
        if not cleaned.isdigit():
            raise serializers.ValidationError(f'{label} may contain digits only.')
        if not (min_len <= len(cleaned) <= max_len):
            raise serializers.ValidationError(f'{label} must be {min_len}–{max_len} digits.')
        return cleaned

    def validate_bank_account_number(self, value):
        return self._digits(value, 'Account number', 6, 20)

    def validate_bank_branch_code(self, value):
        return self._digits(value, 'Branch code', 4, 10)

    def _blank_to_none(self, value):
        value = (value or '').strip()
        return value or None

    def validate_bank_name(self, value):
        return self._blank_to_none(value)

    def validate_bank_account_holder(self, value):
        return self._blank_to_none(value)

    def validate_payment_reference_hint(self, value):
        return self._blank_to_none(value)

    def validate_bank_account_type(self, value):
        return value or None

    def get_logo_url(self, obj):
        if obj.logo:
            return obj.logo.url
        return "/brand/logo.svg" # Default as requested


# Quote Pipeline Serializer (NEW)
class QuotePipelineSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    price = serializers.DecimalField(source='total_amount', max_digits=10, decimal_places=2, read_only=True)
    margin_pct = serializers.DecimalField(source='margin_percentage', max_digits=9, decimal_places=2, read_only=True,
                                          allow_null=True)
    updated_at_iso = serializers.DateTimeField(source='updated_at', format='%Y-%m-%dT%H:%M:%SZ', read_only=True)
    confidence = serializers.SerializerMethodField()  # ADD THIS
    status = serializers.SerializerMethodField()  # ADD THIS
    
    class Meta:
        model = Quote
        fields = [
            'id', 'quote_number', 'customer', 'customer_name', 
            'origin', 'destination', 'sla_hours', 'price', 
            'margin_pct', 'confidence', 'status', 'updated_at', 'updated_at_iso'
        ]
        read_only_fields = ['id', 'updated_at']
    
    def get_confidence(self, obj):
        # Convert "HIGH" → "High", "MEDIUM" → "Medium"
        return obj.confidence.capitalize()
    
    def get_status(self, obj):
        # Convert "DRAFT" → "Draft", "SENT" → "Sent"
        return obj.status.capitalize()


# Driver Performance Serializer
class DriverPerformanceSerializer(serializers.ModelSerializer):
    driver_id = serializers.CharField(source='user.username')
    driver_name = serializers.SerializerMethodField()
    vehicle = serializers.SerializerMethodField()
    on_time_percentage = serializers.DecimalField(source='on_time_rate', max_digits=5, decimal_places=2, read_only=True)
    fuel_efficiency = serializers.IntegerField(source='efficiency_score', read_only=True)
    roi_score = serializers.SerializerMethodField()
    driver_status = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = [
            'id', 'driver_id', 'driver_name', 'vehicle', 'on_time_percentage',
            'safety_score', 'fuel_efficiency', 'margin_per_trip',
            'roi_score', 'driver_status', 'status',
        ]

    def get_driver_name(self, obj):
        name = f"{obj.user.first_name} {obj.user.last_name}".strip()
        return name or obj.user.username

    def get_vehicle(self, obj):
        vehicle = obj.vehicles.first()
        return vehicle.plate if vehicle else None

    def get_roi_score(self, obj):
        # Composite of stored scores — no fake randomness
        return int((float(obj.on_time_rate) * 0.4 + obj.safety_score * 0.35 + obj.efficiency_score * 0.25))

    def get_driver_status(self, obj):
        if obj.status != 'ACTIVE':
            return obj.status.replace('_', ' ').title()
        recent = obj.loads.order_by('-created_at').first()
        if recent and recent.status == 'IN_TRANSIT':
            return 'In Transit'
        if recent and recent.status == 'ASSIGNED':
            return 'Assigned'
        return 'Available'


class WebhookSerializer(serializers.ModelSerializer):
    """Serializer for Webhook model."""

    class Meta:
        from core.models import Webhook
        model = Webhook
        fields = [
            'id', 'url', 'secret', 'events', 'active',
            'failure_count', 'last_fired_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'secret', 'failure_count', 'last_fired_at', 'created_at', 'updated_at']


class IntegrationAPIKeySerializer(serializers.ModelSerializer):
    """Serializer for IntegrationAPIKey model.

    The full key is only ever shown once, in the response to the request that
    generated it (IntegrationAPIKeyViewSet sets show_full_key=True only for
    'create') — every other read (list/retrieve/update) gets a masked value.
    Losing the full value after that point is the point: it lives in the
    operator's password manager from here on, not in a browser tab they can
    leave open, and not in every future API response.
    """

    class Meta:
        from core.models import IntegrationAPIKey
        model = IntegrationAPIKey
        fields = [
            'id', 'name', 'key', 'key_type', 'active',
            'created_at', 'last_used_at',
            'usage_count', 'monthly_quota', 'quota_used',
            'allowed_ips', 'webhook_url',
        ]
        read_only_fields = ['id', 'key', 'created_at', 'last_used_at', 'usage_count', 'quota_used']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get('show_full_key'):
            full = data.get('key') or ''
            data['key'] = ('•' * 8 + full[-4:]) if len(full) >= 4 else '•' * 8
        return data


class APICallLogSerializer(serializers.ModelSerializer):
    """Serializer for per-key API call log entries."""

    class Meta:
        from core.models.integration_api_key import APICallLog
        model = APICallLog
        fields = ['id', 'scored_at', 'invoice_amount', 'risk_tier', 'score', 'eligible', 'caller_ip']
        read_only_fields = fields


class ActivityEventSerializer(serializers.ModelSerializer):
    """Serializer for ActivityEvent model."""

    class Meta:
        model = ActivityEvent
        fields = ['id', 'event_type', 'title', 'description', 'entity_id', 'entity_type', 'metadata', 'created_at']
        read_only_fields = ['id', 'created_at']
