from decimal import Decimal

from django.db import models


class TollPlaza(models.Model):
    """
    SANRAL toll plaza with per-vehicle-class tariffs (ZAR).

    Gauteng Urban Network (GFIP / e-toll) is EXCLUDED — scrapped April 2024.

    Vehicle classes follow SANRAL classification:
        Class 2 — light motor vehicles (passenger, LDV, minibus ≤3.5 t GVM)
        Class 3 — medium motor vehicles (2-axle truck/bus, 3.5–11 t GVM)
        Class 4 — heavy motor vehicles (3+ axle single unit, >11 t GVM)
        Class 5 — multi-unit combinations (truck + trailer / semi-truck)
    """

    ROUTE_CHOICES = [
        ('N1',  'N1 — Cape Town to Johannesburg / Polokwane'),
        ('N2',  'N2 — Cape Town to Durban (coastal)'),
        ('N3',  'N3 — Johannesburg to Durban'),
        ('N4',  'N4 — Pretoria to Maputo'),
        ('N14', 'N14 — Johannesburg to Springbok'),
        ('N17', 'N17 — Johannesburg to Ermelo / Swaziland'),
        ('R30', 'R30/R730/R34 — Bloemfontein region'),
        ('EN4', 'EN4 — Mozambique, Ressano Garcia to Maputo (TRAC)'),
        ('REVIMO', 'Mozambique — Maputo–Katembe bridge, Maputo ring road, N200 (REVIMO)'),
    ]

    TYPE_MAINLINE = 'mainline'
    TYPE_RAMP = 'ramp'
    PLAZA_TYPE_CHOICES = [(TYPE_MAINLINE, 'Mainline'), (TYPE_RAMP, 'Ramp')]

    name = models.CharField(max_length=100, help_text='Official SANRAL plaza name')
    route = models.CharField(max_length=10, choices=ROUTE_CHOICES, db_index=True)
    direction = models.CharField(
        max_length=100,
        help_text='Route description e.g. "Cape Town → Johannesburg"',
    )
    location_km = models.DecimalField(
        max_digits=7, decimal_places=1,
        help_text='Distance from route origin (km)',
    )
    tariff_class_2 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Light motor vehicle tariff (ZAR)',
    )
    tariff_class_3 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Medium motor vehicle tariff (ZAR)',
    )
    tariff_class_4 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Heavy motor vehicle tariff (ZAR)',
    )
    tariff_class_5 = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text='Multi-unit combination tariff (ZAR)',
    )
    tariff_year = models.PositiveSmallIntegerField(
        default=2024,
        help_text='Tariff revision year (SANRAL announces increases annually)',
    )
    lat = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text='Plaza GPS latitude (WGS84) for geofence matching',
    )
    lng = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text='Plaza GPS longitude (WGS84) for geofence matching',
    )
    radius_meters = models.IntegerField(
        default=500,
        help_text='Geofence trigger radius in metres (default 500 m to tolerate GPS uncertainty)',
    )
    # Verification of the tariffs above against their published source (the
    # SANRAL gazette / tariff poster). Set by the seed data migration, by the
    # monthly refresh_verified_rates job when it confirms the stored figures
    # on the source page, and when an admin approves a changed tariff
    # (core.services.verified_rates). Null = never verified: the AI price
    # check then reports this plaza's tolls as not verified.
    tariff_effective_from = models.DateField(
        null=True, blank=True,
        help_text='Date the stored tariff schedule took effect (SANRAL changes it every 1 March)',
    )
    tariff_source_url = models.URLField(max_length=1000, blank=True, default='')
    tariff_source_name = models.CharField(max_length=300, blank=True, default='')
    tariff_verified_at = models.DateField(
        null=True, blank=True,
        help_text='Date the stored tariffs were last confirmed on their source',
    )
    # --- Matching (geofence) ---------------------------------------------
    # A plaza usually has several booths (one per carriageway, plus booths on
    # the ramps at the same site). match_points holds every booth position
    # ([lat, lng] pairs, from OpenStreetMap barrier=toll_booth nodes); the
    # route is matched to the nearest one. lat/lng above stays the primary
    # point and is always matched too.
    plaza_type = models.CharField(max_length=10, choices=PLAZA_TYPE_CHOICES, default=TYPE_MAINLINE)
    # Plazas at the same site (a mainline plaza and the ramp plazas on its
    # interchange) are alternatives: a vehicle pays ONE of them. Blank = its own.
    plaza_group = models.CharField(max_length=60, blank=True, default='')
    match_points = models.JSONField(default=list, blank=True)
    # Ramp plazas only: points on the MAINLINE carriageway either side of the
    # interchange. A route that passes all of them stayed on the mainline —
    # it drove past the ramp booth, not through it — so the ramp is not charged.
    through_points = models.JSONField(default=list, blank=True)
    operator = models.CharField(max_length=40, blank=True, default='SANRAL',
                                help_text='SANRAL, N3TC, Bakwena, TRAC, …')
    country = models.CharField(max_length=2, default='ZA', help_text='ISO 3166-1 alpha-2')
    # Currency the tariff columns (and this plaza's TollTariff history) are
    # in. SA plazas are ZAR; TRAC's and REVIMO's Mozambican plazas publish in
    # meticais and are converted at the day's rate (core/services/fx.py).
    currency = models.CharField(max_length=3, default='ZAR')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'toll_plazas'
        ordering = ['route', 'location_km']
        unique_together = [('name', 'route')]

    def __str__(self):
        return f"{self.name} ({self.route}) km {self.location_km}"

    def all_match_points(self) -> list:
        pts = []
        if self.lat is not None and self.lng is not None:
            pts.append((float(self.lat), float(self.lng)))
        for p in self.match_points or []:
            pts.append((float(p[0]), float(p[1])))
        return pts

    def tariff_on(self, vehicle_class: int, on_date=None, history=None) -> tuple:
        """(tariff, effective_from) in force on `on_date` for column class 2–5.

        The plaza's own columns are the CURRENT schedule (what an admin
        approves through verified rates). A trip dated before that schedule
        took effect is priced from the TollTariff history row covering its
        date. `history` lets a caller pass prefetched rows (newest first).
        """
        current = self.get_tariff(vehicle_class)
        if on_date is None or (self.tariff_effective_from and on_date >= self.tariff_effective_from):
            return current, self.tariff_effective_from
        rows = history if history is not None else list(self.tariff_history.order_by('-effective_from'))
        for row in rows:
            if row.effective_from <= on_date and (row.effective_to is None or on_date <= row.effective_to):
                return row.get_tariff(vehicle_class), row.effective_from
        return current, self.tariff_effective_from

    def get_tariff(self, vehicle_class: int) -> Decimal:
        """Return tariff for SANRAL vehicle class 2–5."""
        mapping = {
            2: self.tariff_class_2,
            3: self.tariff_class_3,
            4: self.tariff_class_4,
            5: self.tariff_class_5,
        }
        if vehicle_class not in mapping:
            raise ValueError(f"Vehicle class must be 2–5, got {vehicle_class}")
        return mapping[vehicle_class]


class TollTariff(models.Model):
    """One published tariff schedule for a plaza, valid over a date range.

    History for pricing a trip by its own date (SANRAL changes tariffs every
    1 March; TRAC's Mozambican plazas on their own dates). Columns use the
    SAME offset as TollPlaza: tariff_class_2 = SANRAL Class 1 … tariff_class_5
    = SANRAL Class 4. All amounts VAT inclusive, as published, in ZAR.
    """
    plaza = models.ForeignKey(TollPlaza, on_delete=models.CASCADE, related_name='tariff_history')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True, help_text='Last day in force (inclusive); blank = open')
    tariff_class_2 = models.DecimalField(max_digits=8, decimal_places=2)
    tariff_class_3 = models.DecimalField(max_digits=8, decimal_places=2)
    tariff_class_4 = models.DecimalField(max_digits=8, decimal_places=2)
    tariff_class_5 = models.DecimalField(max_digits=8, decimal_places=2)
    source_url = models.URLField(max_length=1000, blank=True, default='')
    source_name = models.CharField(max_length=300, blank=True, default='')

    class Meta:
        db_table = 'toll_tariffs'
        ordering = ['plaza_id', '-effective_from']
        unique_together = [('plaza', 'effective_from')]

    def __str__(self):
        return f"{self.plaza.name} from {self.effective_from}"

    def get_tariff(self, vehicle_class: int) -> Decimal:
        mapping = {2: self.tariff_class_2, 3: self.tariff_class_3, 4: self.tariff_class_4, 5: self.tariff_class_5}
        if vehicle_class not in mapping:
            raise ValueError(f"Vehicle class must be 2–5, got {vehicle_class}")
        return mapping[vehicle_class]
