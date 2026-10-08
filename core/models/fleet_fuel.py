"""Measured fuel use per truck and per vehicle type, from the company's own
fleet tracker (Cartrack). Written only by the weekly refresh
(core.services.fleet_fuel_actuals.refresh_company); read by pricing
(core.services.quote_costing.resolve_rated_burn) and the fleet settings API.

Nothing here is ever fetched on a request: rows are what the last refresh
stored, with the period, sample size, source and confidence they were built
from.
"""
from django.conf import settings
from django.db import models


class FleetFuelMeasurement(models.Model):
    SCOPE_VEHICLE = 'VEHICLE'
    SCOPE_VEHICLE_TYPE = 'VEHICLE_TYPE'
    SCOPE_CHOICES = [(SCOPE_VEHICLE, 'One truck'), (SCOPE_VEHICLE_TYPE, 'Vehicle type')]

    # How the vehicle type's quotes take their rated burn (type rows only).
    MODE_AUTO = 'AUTO'              # measured when there is enough data, else the configured figure
    MODE_MEASURED = 'MEASURED'      # admin chose "Use measured figure" (same rule as AUTO, recorded)
    MODE_CONFIGURED = 'CONFIGURED'  # admin pinned the typed figure
    MODE_CHOICES = [(MODE_AUTO, 'Automatic'), (MODE_MEASURED, 'Use measured figure'),
                    (MODE_CONFIGURED, 'Use my figure')]

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='fleet_fuel_measurements')
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)
    # A type row may point at a shared (company=None) vehicle type: the
    # measurement is still this company's own trucks only.
    vehicle_type = models.ForeignKey('VehicleType', on_delete=models.CASCADE, null=True, blank=True,
                                     related_name='fuel_measurements')
    vehicle = models.ForeignKey('Vehicle', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='fuel_measurements')

    provider = models.CharField(max_length=20, default='cartrack')
    # can_bus = Cartrack "fuel consumed" counter (GET /fuel/consumed);
    # fuel_level = Cartrack's refuel-adjusted fuel-level estimate
    # (GET /fuel/level estimated_fuel_used); mixed = a type built from both.
    fuel_source = models.CharField(max_length=20, blank=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)

    distance_km = models.FloatField(default=0)
    litres = models.FloatField(default=0)
    l_per_100km = models.FloatField(null=True, blank=True)           # overall average, all km
    loaded_km = models.FloatField(default=0)                        # km on recorded loads (TMS trips)
    loaded_litres = models.FloatField(default=0)
    loaded_l_per_100km = models.FloatField(null=True, blank=True)
    loaded_mean_load_ratio = models.FloatField(null=True, blank=True)
    other_km = models.FloatField(default=0)                         # km not on a recorded load
    other_l_per_100km = models.FloatField(null=True, blank=True)
    # The figure pricing uses: full-load burn under the 0,70 + 0,30 × ratio rule.
    rated_burn_l_per_100km = models.FloatField(null=True, blank=True)
    # 'loaded_trips' (from recorded loads' own ratios) | 'overall_assumed'
    # (all km, unlinked km assumed at ASSUMED_LOAD_RATIO).
    rated_method = models.CharField(max_length=30, blank=True)
    vehicles_count = models.PositiveIntegerField(default=0)
    windows_used = models.PositiveIntegerField(default=0)
    windows_rejected = models.PositiveIntegerField(default=0)
    rejections = models.JSONField(default=list, blank=True)        # [{reason, start, end, detail}]
    # high | medium | low | insufficient | rejected
    confidence = models.CharField(max_length=20, default='insufficient')
    sufficient = models.BooleanField(default=False)
    note = models.CharField(max_length=300, blank=True)
    computed_at = models.DateTimeField(null=True, blank=True)

    burn_mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_AUTO)
    burn_mode_set_at = models.DateTimeField(null=True, blank=True)
    burn_mode_set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='+')

    class Meta:
        db_table = 'fleet_fuel_measurements'
        constraints = [
            models.UniqueConstraint(fields=['company', 'vehicle_type'], condition=models.Q(scope='VEHICLE_TYPE'),
                                    name='uniq_fuel_measurement_per_company_type'),
            models.UniqueConstraint(fields=['company', 'vehicle'], condition=models.Q(scope='VEHICLE'),
                                    name='uniq_fuel_measurement_per_company_vehicle'),
        ]
        indexes = [models.Index(fields=['company', 'scope'])]

    def __str__(self):
        what = self.vehicle_id if self.scope == self.SCOPE_VEHICLE else self.vehicle_type_id
        return f'{self.scope} {what}: {self.rated_burn_l_per_100km} L/100km ({self.confidence})'


class FleetFuelSyncRun(models.Model):
    """One refresh attempt for one company (status for the settings screen)."""
    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='fleet_fuel_runs')
    provider = models.CharField(max_length=20, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # ok | partial | skipped | failed | running
    status = models.CharField(max_length=20, default='running')
    message = models.CharField(max_length=300, blank=True)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'fleet_fuel_sync_runs'
        ordering = ['-started_at']
        indexes = [models.Index(fields=['company', '-started_at'])]
