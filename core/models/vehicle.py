from django.db import models
from django.conf import settings


class VehicleType(models.Model):
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="vehicle_types")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    capacity = models.DecimalField(max_digits=10, decimal_places=2)
    max_distance = models.DecimalField(max_digits=10, decimal_places=2)
    base_rate = models.DecimalField(max_digits=10, decimal_places=2)
    fuel_consumption_l_per_100km = models.DecimalField(
        max_digits=5, decimal_places=2, default=36.0,
        help_text='Diesel consumption in litres per 100km (e.g. 32 for Flatbed)'
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'vehicle_types'
        ordering = ['name']

    def __str__(self):
        return self.name


class Vehicle(models.Model):
    company = models.ForeignKey("Company", on_delete=models.CASCADE, null=True, blank=True, related_name="fleet_vehicles")
    vin = models.CharField(max_length=100, unique=True)
    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    driver = models.ForeignKey('core.Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='vehicles')
    vehicle_type = models.ForeignKey(VehicleType, on_delete=models.SET_NULL, null=True, blank=True, related_name='vehicles')
    year = models.IntegerField()
    plate = models.CharField(max_length=50)
    type = models.CharField(max_length=50)
    capacity = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=50, default='AVAILABLE')
    fuel_type = models.CharField(max_length=50)
    mileage = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    last_maintenance_date = models.DateField(null=True, blank=True)
    next_maintenance_due = models.DateField(null=True, blank=True)
    # When we last sent a maintenance-due alert for this vehicle, so the daily
    # sweep re-alerts at most weekly (core/management/commands/sweep_maintenance_due.py).
    last_maintenance_alert_at = models.DateField(null=True, blank=True)
    service_interval_km = models.IntegerField(null=True, blank=True, help_text='How many km between services (e.g. 10000)')
    last_service_mileage = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text='Odometer reading at last service')
    insurance_expiry = models.DateField(null=True, blank=True)
    registration_expiry = models.DateField(null=True, blank=True)
    
    # AI Health Score fields
    ai_health_score = models.IntegerField(default=0)
    fuel_efficiency_score = models.IntegerField(default=0)
    uptime_score = models.IntegerField(default=0)
    maintenance_score = models.IntegerField(default=0)
    uptime_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cost_per_km = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    margin_per_trip = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # NEW: Fuel consumption for expense calculation
    fuel_consumption_per_km = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0.35,
        help_text='Fuel consumption in litres per km (default: 0.35 L/km for trucks)'
    )

    # Cartrack Fleet API — live telematics (current-state, updated in place on each poll)
    cartrack_registration = models.CharField(
        max_length=50,
        blank=True,
        help_text='Registration string Cartrack identifies this vehicle by, if it differs from plate'
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    heading = models.DecimalField(
        max_digits=5, decimal_places=1, null=True, blank=True,
        help_text='Compass heading in degrees (0-360)'
    )
    speed_kmh = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    ignition_on = models.BooleanField(null=True, blank=True)
    last_location_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Timestamp of the last Cartrack location reading'
    )

    # Cartrack extended telemetry — cargo/cabin temperature probes (up to 4),
    # reported informational driver reference, and door state.
    temp1 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    temp2 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    temp3 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    temp4 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    cartrack_current_driver_ref = models.CharField(
        max_length=100, blank=True,
        help_text='Raw driver/tag identifier Cartrack reports as currently in this vehicle. '
                   'Informational only — does not affect the authoritative `driver` field.'
    )
    door_open = models.BooleanField(null=True, blank=True)
    last_door_event_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'vehicles'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.make} {self.model} - {self.plate}"


class VehicleLog(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='logs')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    log_type = models.CharField(max_length=50)
    description = models.TextField()
    mileage = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'vehicle_logs'
        ordering = ['-date']
    
    def __str__(self):
        return f"{self.vehicle} - {self.log_type} - {self.date}"


