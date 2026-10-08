from django.db import models
from django.conf import settings
from .customer import Customer
from .vehicle import Vehicle
from .driver import Driver

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
    trip_type = models.CharField(max_length=20, choices=TRIP_TYPE_CHOICES, default='ONE_WAY')
    return_location = models.CharField(max_length=500, blank=True, default='')
    return_distance = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    return_date = models.DateField(null=True, blank=True)
    return_cargo = models.TextField(blank=True, default='')
    costing_source = models.CharField(max_length=10, choices=COSTING_SOURCE_CHOICES, blank=True, default='')
    # compute() inputs the load fields don't carry (same keys as
    # Quote.costing_inputs, plus toll_cost = all loaded legs).
    costing_inputs = models.JSONField(default=dict, blank=True)
    # compute() output (lines incl. the empty_return leg, floor, warnings).
    costing_snapshot = models.JSONField(default=dict, blank=True)
    cost_floor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                     help_text='Floor of costing_snapshot (null = incomplete / unknown)')
    empty_return_assumed = models.BooleanField(null=True, blank=True,
                                               help_text='The costing includes an empty return leg (null = unknown)')
    fuel_price_used = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    fuel_price_source = models.CharField(max_length=10, blank=True, default='')
    fuel_zone = models.CharField(max_length=10, blank=True, default='')
    fuel_effective_from = models.DateTimeField(null=True, blank=True)
    fuel_litres = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    priced_vehicle_type = models.ForeignKey('core.VehicleType', on_delete=models.SET_NULL, null=True, blank=True,
                                            related_name='+')
    costed_at = models.DateTimeField(null=True, blank=True)
    # As quoted: never changed after conversion (quoted vs actual margin).
    quoted_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    quoted_cost_floor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    quoted_margin_pct = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)

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
    
    def __str__(self):
        return f"Load {self.load_number} - {self.status}"
