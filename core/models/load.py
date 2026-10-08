from django.db import models
from django.conf import settings
from .customer import Customer
from .vehicle import Vehicle
from .driver import Driver

# Server-side defaults for the trip-economics columns (db_default): an older
# app image (rollback) that doesn't know these columns can still insert loads.
JSON_EMPTY = models.Value({}, output_field=models.JSONField())


class Load(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ASSIGNED', 'Assigned'),
        ('LOADING', 'Loading'),
        ('IN_TRANSIT', 'In Transit'),
        ('DELIVERED', 'Delivered'),
        ('INVOICED', 'Invoiced'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="loads")
    
    load_number = models.CharField(max_length=100, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='loads')
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='loads')
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='loads')
    quote = models.ForeignKey('core.Quote', on_delete=models.SET_NULL, null=True, blank=True, related_name='loads')
    # International transport: the delivery auto-invoice zero-rates it (VAT
    # 0%), matching the quote it came from (Quote.is_international).
    is_international = models.BooleanField(default=False)
    
    pickup_location = models.CharField(max_length=500)
    pickup_city = models.CharField(max_length=100)
    pickup_state = models.CharField(max_length=50)
    pickup_zip = models.CharField(max_length=20)
    pickup_date = models.DateTimeField()
    pickup_lat = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)
    pickup_lng = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)

    delivery_location = models.CharField(max_length=500)
    delivery_city = models.CharField(max_length=100)
    delivery_state = models.CharField(max_length=50)
    delivery_zip = models.CharField(max_length=20)
    delivery_date = models.DateTimeField()
    delivery_lat = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)
    delivery_lng = models.DecimalField(max_digits=12, decimal_places=7, null=True, blank=True)

    # Carried straight over from the quote at conversion time — see
    # Quote.stops / Quote.route_geometry for the shapes and why these exist.
    stops = models.JSONField(default=list, blank=True)
    route_geometry = models.JSONField(default=list, blank=True)

    cargo_description = models.TextField()
    weight = models.DecimalField(max_digits=10, decimal_places=2)
    distance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    fuel_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    additional_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Copied from the quote by convert_to_load so a booking itemises the same
    # lines the quote priced (previously tolls and the driver allowance were
    # dropped and showed as "Not itemised"). Already inside total_amount.
    toll_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    driver_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    # Tonnage (per_tonne quotes): invoiced at rate_per_tonne x max(actual
    # tonnes, min_tonnes). actual_tonnes is the weighbridge figure: editable
    # here, and the field a TMS sync should write (trip-economics branch);
    # actual_tonnes_source says where it came from. Without it the invoice
    # uses planned_tonnes and is flagged "Awaiting weighbridge tonnes".
    pricing_basis = models.CharField(max_length=10, default='per_load',
                                     choices=[('per_load', 'Per load'), ('per_tonne', 'Per tonne')])
    rate_per_tonne = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    min_tonnes = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    planned_tonnes = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    actual_tonnes = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True,
                                        help_text='Weighbridge tonnes delivered')
    weighbridge_slip = models.CharField(max_length=60, blank=True, default='',
                                        help_text='Weighbridge ticket / slip number')
    actual_tonnes_source = models.CharField(max_length=20, blank=True, default='',
                                            help_text='weighbridge | manual | tms')
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='PENDING')
    actual_delivered_at = models.DateTimeField(null=True, blank=True, help_text='Timestamp when load was marked DELIVERED')
    notes = models.TextField(blank=True)
    
    pod_signature = models.TextField(blank=True)  # Proof of delivery signature
    pod_received_by = models.CharField(max_length=200, blank=True)
    pod_document = models.FileField(upload_to='pod/', blank=True, null=True)

    # POD evidence metadata (capital-safety 2026-10). A POD photo is what an
    # advance is funded against, so these are written only by the POD upload
    # endpoint (and fleet integrations), never by a generic PATCH — see
    # LoadSerializer.read_only_fields. pod_file_sha256 is computed server-side
    # so a funder can later prove the file they saw is the file on record.
    POD_SOURCE_CHOICES = [
        ('CAMERA', 'Camera'),
        ('LIBRARY', 'Photo library'),
        ('UPLOAD', 'File upload'),
        ('UNKNOWN', 'Unknown'),
    ]
    pod_captured_at = models.DateTimeField(null=True, blank=True)
    pod_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    pod_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    pod_device = models.CharField(max_length=200, blank=True)
    pod_source = models.CharField(max_length=10, choices=POD_SOURCE_CHOICES, blank=True, default='')
    pod_file_sha256 = models.CharField(max_length=64, blank=True)

    # --- Costing assumptions (trip economics, 2026-10) -----------------------
    # What the job was priced on, so its P&L estimate and "quoted vs actual"
    # use the quote's own compute() figures instead of a generic model.
    # Written by convert_to_load (copied from the quote's pricing snapshot) or
    # by core.services.trip_costing for loads that never had a quote (TMS).
    COSTING_SOURCE_CHOICES = [
        ('', 'Not costed (legacy)'),
        ('quote', 'Quote snapshot'),
        ('computed', 'Computed from the load'),
        ('unknown', 'Not enough information'),
    ]
    TRIP_TYPE_CHOICES = [('ONE_WAY', 'One Way'), ('ROUND_TRIP', 'Round Trip')]
    trip_type = models.CharField(max_length=20, choices=TRIP_TYPE_CHOICES, default='ONE_WAY', db_default='ONE_WAY')
    return_location = models.CharField(max_length=500, blank=True, default='', db_default='')
    return_distance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    return_date = models.DateField(null=True, blank=True)
    return_cargo = models.TextField(blank=True, default='', db_default='')
    costing_source = models.CharField(max_length=10, choices=COSTING_SOURCE_CHOICES, blank=True, default='', db_default='')
    # compute() inputs the load fields don't carry (same keys as
    # Quote.costing_inputs, plus toll_cost = all loaded legs).
    costing_inputs = models.JSONField(default=dict, db_default=JSON_EMPTY, blank=True)
    # compute() output (lines incl. the empty_return leg, floor, warnings).
    costing_snapshot = models.JSONField(default=dict, db_default=JSON_EMPTY, blank=True)
    cost_floor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                     help_text='Floor of costing_snapshot (null = incomplete / unknown)')
    empty_return_assumed = models.BooleanField(null=True, blank=True,
                                               help_text='The costing includes an empty return leg (null = unknown)')
    fuel_price_used = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    fuel_price_source = models.CharField(max_length=10, blank=True, default='', db_default='')
    fuel_zone = models.CharField(max_length=10, blank=True, default='', db_default='')
    fuel_effective_from = models.DateTimeField(null=True, blank=True)
    fuel_litres = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    priced_vehicle_type = models.ForeignKey('core.VehicleType', on_delete=models.SET_NULL, null=True, blank=True,
                                            related_name='+')
    costed_at = models.DateTimeField(null=True, blank=True)
    # As quoted: never changed after conversion (quoted vs actual margin).
    quoted_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    quoted_cost_floor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    quoted_margin_pct = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)

    # --- Return-load linking (trip economics) --------------------------------
    # A return load (backhaul) points at the outbound load whose truck it
    # brings home: one return per outbound, pairs only (a return is never
    # itself an outbound with its own return). Same company always; see
    # core.services.return_loads for the rules. While linked, neither leg's
    # estimate carries an empty return (the truck came back loaded).
    return_of = models.OneToOneField('self', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='return_load')
    RETURN_LINK_SOURCES = [('manual', 'Linked by a user'), ('tms', 'Linked by the TMS'),
                           ('convert', 'Linked when booking the quote')]
    return_link_source = models.CharField(max_length=10, choices=RETURN_LINK_SOURCES, blank=True, default='', db_default='')
    return_linked_at = models.DateTimeField(null=True, blank=True)
    return_linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='+')
    # The outbound is waiting for a return load (set when booking; cleared
    # when one is linked). Drives "find a return load" prompts only.
    expecting_return = models.BooleanField(default=False, db_default=False)
    # The user says every cost of this job is recorded: actual expenses are
    # the whole cost (no estimate for unrecorded categories).
    costs_closed = models.BooleanField(default=False, db_default=False)
    # Cached estimate (core.services.trip_economics.recompute, signals): the
    # pair-aware estimated cost and how it was worked out. Reports compute
    # live from the same function; this is for lists and the app.
    # --- TMS identity (trip economics) ---------------------------------------
    # The id the company's TMS knows this job by (unique per company when
    # set) and which system sent it. Sync endpoints upsert on it.
    external_id = models.CharField(max_length=100, blank=True, default='', db_default='')
    external_source = models.CharField(max_length=50, blank=True, default='', db_default='')
    # A TMS named an outbound (return_of_external_id) not synced yet: linked
    # as soon as it arrives. 'number:<load_number>' for return_of_load_number.
    return_of_external_ref = models.CharField(max_length=120, blank=True, default='', db_default='')
    # Set when the TMS changed the rate after the load was invoiced: the
    # invoice is never changed, this says it differs (code, invoice, amounts).
    invoice_mismatch = models.JSONField(default=dict, db_default=JSON_EMPTY, blank=True)
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    estimate_basis = models.CharField(max_length=30, blank=True, default='', db_default='')
    economics_updated_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='loads_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'loads'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['load_number']),
            models.Index(fields=['status']),
            models.Index(fields=['pickup_date']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['company', 'external_id'], condition=~models.Q(external_id=''),
                                    name='uniq_load_external_id_per_company'),
        ]
    
    # Written only by their own services (queryset updates): a full save() of
    # an instance read earlier must never put back a stale value (e.g. a
    # webhook's load.save() unlinking a return load linked meanwhile).
    SERVER_ONLY_FIELDS = frozenset({
        # return-load link
        'return_of', 'return_link_source', 'return_linked_at', 'return_linked_by', 'expecting_return',
        # cached estimate
        'estimated_cost', 'estimate_basis', 'economics_updated_at',
        # costing assumptions (trip_costing / tms_routing / close-costs)
        'costing_source', 'costing_inputs', 'costing_snapshot', 'cost_floor', 'empty_return_assumed',
        'fuel_price_used', 'fuel_price_source', 'fuel_zone', 'fuel_effective_from', 'fuel_litres',
        'priced_vehicle_type', 'costed_at', 'quoted_price', 'quoted_cost_floor', 'quoted_margin_pct',
        'costs_closed',
        # TMS identity / flags
        'external_id', 'external_source', 'return_of_external_ref', 'invoice_mismatch',
    })

    def save(self, *args, **kwargs):
        if (not self._state.adding and self.pk is not None and kwargs.get('update_fields') is None
                and not kwargs.get('force_insert')):
            kwargs['update_fields'] = [f.name for f in self._meta.concrete_fields
                                       if not f.primary_key and f.name not in self.SERVER_ONLY_FIELDS]
            self._guard_stale_progress()
        return super().save(*args, **kwargs)

    def _guard_stale_progress(self):
        """A full save never undoes progress made since this instance was read:
        an INVOICED job is never moved back (only CANCELLED may follow), and a
        delivered job keeps its delivery time. The instance takes the stored
        values, so the caller's response shows the truth."""
        row = type(self).objects.filter(pk=self.pk).values('status', 'actual_delivered_at').first()
        if row is None:
            return
        if row['status'] == 'INVOICED' and self.status not in ('INVOICED', 'CANCELLED'):
            self.status = 'INVOICED'
        if (self.actual_delivered_at is None and row['actual_delivered_at'] is not None
                and self.status in ('DELIVERED', 'INVOICED')):
            self.actual_delivered_at = row['actual_delivered_at']

    def __str__(self):
        return f"Load {self.load_number} - {self.status}"
