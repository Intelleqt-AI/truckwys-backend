"""Border, permit and in-country road charges for SA trucks, per country.

Each charge is one *component* with its own currency, source, as-of date and
a ``verified`` flag. A component is shown as a published figure only when
(a) its tariff comes from a primary source we hold, and (b) the vehicle
details it depends on (gross mass, axle units) are known rather than
inferred. Anything else is labelled "estimate" and says why.

Direction rules (an SA fleet's view):
  * ``entry``  — charged when the truck ENTERS that country.
  * ``exit``   — charged when it leaves that country back towards SA (only
                 where a schedule says so: Botswana's return-permit
                 supplement, clearing on the way back).
  * ``per_km`` — charged on the kilometres driven inside that country, on
                 every leg (loaded or empty).

Sources (as held on 8 Oct 2026):
  ZW  Zimborders BBP toll fees 2026 —
      https://zimborders.com/wp-content/uploads/2026/04/Toll-Fees-2026.pdf
      ZINARA transit fees — https://zinara.co.zw/services/transit-fees/
      ZINARA tolling — https://www.zinara.co.zw/services/tolling/
  BW  Road Transport (Permits) (Amendment) Regulations, SI 48 of 2017,
      Table 4 — https://botswanalaws.com/Botswana2017Pdf/48of2017.pdf
  NA  Road Fund Administration fees & tariffs (eff. 1 Aug 2026) —
      https://rfanam.com.na/fees-tariffs/
  LS  Road Fund toll-gate fees (eff. 1 Apr 2022) —
      https://www.roadfund.org.ls/news/road-fund-announces-an-increase-in-toll-gate-fees/
      (M650 under Legal Notice 65 of 2025 could NOT be verified)
  SZ  ERS "New toll fees for vehicles entering Eswatini" (1 Oct 2025),
      reported at https://eswatinipositivenews.online/new-toll-fees-for-vehicles-entering-eswatini/
      (the notice itself could not be retrieved)
  MZ  no primary source for foreign-truck border charges
  ZM  Zambia business licensing portal, port-of-entry tolls (undated) —
      https://www.businesslicenses.gov.zm/printlicense/id/518
  MW  Roads Fund Administration international transit fees (2023) —
      https://rfamw.com/index.php/international-transit-fees/
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from math import ceil

D = Decimal


# ---------------------------------------------------------------------------
# Vehicle profile
# ---------------------------------------------------------------------------

# Gross mass assumed per SANRAL class when the vehicle type does not say:
# the legal maximum for the typical vehicle of that class. An ESTIMATE.
GROSS_BY_SANRAL_CLASS = {1: 3_500, 2: 16_000, 3: 26_000, 4: 56_000}
UNITS_BY_SANRAL_CLASS = {1: (2,), 2: (2,), 3: (3,), 4: (3, 2, 2)}


@dataclass
class VehicleProfile:
    gross_kg: int
    gross_source: str            # 'vehicle' | 'sanral_class' | 'load_weight'
    units: tuple                 # axles per unit: (3, 2, 2) = 3-axle horse + 2 + 2
    units_source: str            # 'vehicle' | 'sanral_class' | 'load_weight'

    @property
    def gross_known(self) -> bool:
        return self.gross_source == 'vehicle'

    @property
    def units_known(self) -> bool:
        return self.units_source == 'vehicle'

    @property
    def combination(self) -> bool:
        return len(self.units) > 1

    @property
    def axles(self) -> int:
        return sum(self.units)

    @property
    def config(self) -> str:
        return '+'.join(str(u) for u in self.units)

    def gross_note(self) -> str:
        if self.gross_known:
            return ''
        if self.gross_source == 'sanral_class':
            return f'assumes {self.gross_kg:,} kg gross (typical for its toll class) — set the vehicle type\'s gross mass'
        return f'assumes {self.gross_kg:,} kg gross from the load/capacity — set the vehicle type\'s gross mass'

    def units_note(self) -> str:
        if self.units_known:
            return ''
        return f'assumes a {self.config} axle layout — set the vehicle type\'s axle configuration'


def parse_axle_config(text):
    """'3+2+2' / '3-2-2' / '3,2,2' → (3, 2, 2); None if unusable."""
    if not text:
        return None
    import re
    parts = [p for p in re.split(r'[+\-,/ x]+', str(text).strip()) if p]
    try:
        units = tuple(int(p) for p in parts)
    except ValueError:
        return None
    if not units or any(u < 1 or u > 6 for u in units):
        return None
    return units


def vehicle_profile(*, gross_mass_kg=None, axle_config=None, sanral_class=None, weight_kg=0,
                    vehicle_capacity_kg=0) -> VehicleProfile:
    banding = float(vehicle_capacity_kg or 0) or float(weight_kg or 0)
    if gross_mass_kg:
        gross, g_src = int(gross_mass_kg), 'vehicle'
    elif sanral_class in GROSS_BY_SANRAL_CLASS:
        gross, g_src = GROSS_BY_SANRAL_CLASS[sanral_class], 'sanral_class'
    else:
        gross, g_src = int(banding or 0), 'load_weight'
    units = parse_axle_config(axle_config)
    if units:
        u_src = 'vehicle'
    elif sanral_class in UNITS_BY_SANRAL_CLASS:
        units, u_src = UNITS_BY_SANRAL_CLASS[sanral_class], 'sanral_class'
    else:
        units = ((2,) if banding <= 8_000 else (3,) if banding <= 16_000
                 else (3, 2) if banding <= 20_000 else (3, 2, 2))
        u_src = 'load_weight'
    return VehicleProfile(gross, g_src, tuple(units), u_src)


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

@dataclass
class Charge:
    """One component before conversion to rand."""
    code: str
    type: str                    # 'border_crossing' | 'non_sa_toll'
    description: str
    currency: str
    amount: Decimal              # in `currency`
    tariff_verified: bool
    source_name: str
    source_url: str = ''
    as_of: date = None
    depends_on: tuple = ()       # ('gross',) / ('units',) — profile facts the amount needs
    notes: list = field(default_factory=list)


def to_line(ch: Charge, profile: VehicleProfile, today=None) -> dict:
    from core.services.fx import to_zar
    zar, rate = to_zar(ch.amount, ch.currency, today)
    notes = list(ch.notes)
    if 'gross' in ch.depends_on and not profile.gross_known:
        notes.append(profile.gross_note())
    if 'units' in ch.depends_on and not profile.units_known:
        notes.append(profile.units_note())
    verified = bool(ch.tariff_verified) and not (
        ('gross' in ch.depends_on and not profile.gross_known)
        or ('units' in ch.depends_on and not profile.units_known))
    if ch.currency != 'ZAR' and rate.is_fallback:
        notes.append(rate.label)
    label = 'published' if verified else 'estimate'
    detail = '; '.join(n for n in notes if n)
    desc = ch.description if verified else f'{ch.description} (estimate)'
    return {
        'type': ch.type, 'code': ch.code, 'description': desc, 'amount': float(zar),
        'currency': ch.currency, 'amount_foreign': float(ch.amount),
        'fx': rate.as_dict() if ch.currency != 'ZAR' else None,
        'verified': verified, 'label': label,
        'source': ch.source_name, 'source_url': ch.source_url,
        'as_of': ch.as_of.isoformat() if ch.as_of else None,
        'detail': detail,
    }


# ---------------------------------------------------------------------------
# Per-country schedules
# ---------------------------------------------------------------------------

ZB_URL = 'https://zimborders.com/wp-content/uploads/2026/04/Toll-Fees-2026.pdf'
ZINARA_TRANSIT_URL = 'https://zinara.co.zw/services/transit-fees/'
ZINARA_TOLL_URL = 'https://www.zinara.co.zw/services/tolling/'
BW_URL = 'https://botswanalaws.com/Botswana2017Pdf/48of2017.pdf'
NA_URL = 'https://rfanam.com.na/fees-tariffs/'
LS_URL = 'https://www.roadfund.org.ls/news/road-fund-announces-an-increase-in-toll-gate-fees/'
SZ_URL = 'https://eswatinipositivenews.online/new-toll-fees-for-vehicles-entering-eswatini/'
ZM_URL = 'https://www.businesslicenses.gov.zm/printlicense/id/518'
MW_URL = 'https://rfamw.com/index.php/international-transit-fees/'

ZW_CLEARING_AGENT_ZAR = D('2005.00')
# ZINARA's toll calculator: Beitbridge-Harare 580 km has 4 gates.
ZW_KM_PER_GATE = D('145')


def _zw_entry(p: VehicleProfile, frm: str):
    if p.gross_kg >= 56_000:
        usd, cls = D('375'), 'Abnormal (GCM 56 000 kg or more)'
    elif p.combination or p.axles >= 3:
        usd, cls = D('221'), 'Goods vehicle (3+ axle rigid, or rigid towing a trailer)'
    else:
        usd, cls = D('125'), 'Heavy vehicle (over 2 300 kg net)'
    return [
        Charge('zw_border_access_toll', 'border_crossing', f'Zimbabwe border access toll, Beitbridge — {cls}',
               'USD', usd, True, 'Zimborders BBP toll fees 2026', ZB_URL, date(2026, 4, 28),
               depends_on=('gross', 'units')),
        _zw_agent(),
    ]


def _zw_agent():
    return Charge('zw_clearing_agent', 'border_crossing', 'Clearing agent, Beitbridge',
                  'ZAR', ZW_CLEARING_AGENT_ZAR, False, 'Agent estimate — enter your agent\'s fee',
                  notes=['agent estimate — enter your agent\'s fee'])


def _zw_per_km(p: VehicleProfile, km: Decimal):
    per100 = D('10') if p.combination else D('8')
    hundreds = D(ceil(km / 100)) if km > 0 else D('0')
    gates = int((km / ZW_KM_PER_GATE).quantize(D('1'), rounding=ROUND_HALF_UP)) if km > 0 else 0
    gate_usd = D('20') if p.combination else D('10')
    out = [Charge('zw_transit_fee', 'non_sa_toll',
                  f'Zimbabwe transit fee, {int(km)} km (US${per100} per 100 km or part)',
                  'USD', per100 * hundreds, True, 'ZINARA transit fees', ZINARA_TRANSIT_URL,
                  depends_on=('units',), notes=['ZINARA gives no effective date'])]
    if gates:
        out.append(Charge('zw_toll_gates', 'non_sa_toll',
                          f'Zimbabwe toll gates, about {gates} (US${gate_usd} each on premium roads)',
                          'USD', gate_usd * gates, False, 'ZINARA tolling (S.I. 32 of 2021)', ZINARA_TOLL_URL,
                          date(2024, 10, 28),
                          notes=[f'gate count is an estimate: 1 per ~{ZW_KM_PER_GATE} km '
                                 '(ZINARA calculator: 4 on Beitbridge–Harare)']))
    return out


# Botswana SI 48/2017 Table 4 (SACU single-trip permit): gross-mass bands,
# (upper kg inclusive, one way P, return P).
BW_BANDS = [
    (3_499, 65, 117), (4_500, 91, 169), (6_500, 117, 208), (8_500, 143, 260), (10_500, 169, 299),
    (12_500, 195, 351), (14_500, 208, 377), (16_500, 234, 416), (18_500, 247, 442), (20_500, 260, 468),
    (22_500, 286, 520), (24_500, 312, 559), (26_500, 338, 611), (28_500, 364, 650), (30_500, 390, 702),
    (32_500, 429, 780), (34_500, 468, 819), (36_500, 507, 910), (38_500, 546, 988), (40_500, 585, 1053),
    (42_500, 624, 1118), (44_500, 676, 1222), (46_500, 754, 1365), (48_500, 806, 1456), (50_500, 858, 1560),
    (52_500, 897, 1651), (54_500, 936, 1742), (56_500, 975, 1833), (58_500, 1014, 1924), (60_500, 1053, 2015),
    (62_500, 1092, 2106), (64_500, 1131, 2197), (66_500, 1170, 2288), (68_500, 1209, 2379), (70_500, 1248, 2470),
    (72_500, 1287, 2561), (74_500, 1326, 2652), (76_500, 1365, 2743), (78_500, 1404, 2834), (80_500, 1443, 2925),
    (82_500, 1482, 3016), (84_500, 1521, 3107), (86_500, 1560, 3198), (88_500, 1599, 3289), (90_500, 1638, 3380),
    (92_500, 1677, 3471), (94_500, 1716, 3562), (96_500, 1755, 3653), (98_500, 1794, 3744), (100_500, 1833, 3835),
    (10 ** 9, 1872, 3926),
]


def bw_band(gross_kg: int):
    for upper, one_way, ret in BW_BANDS:
        if gross_kg <= upper:
            return upper, D(one_way), D(ret)
    raise AssertionError('unreachable')


def _bw_entry(p: VehicleProfile, frm: str):
    _, one_way, _ = bw_band(p.gross_kg)
    return [Charge('bw_single_trip_permit', 'border_crossing',
                   f'Botswana single-trip permit (SACU), one way, {p.gross_kg:,} kg band',
                   'BWP', one_way, True, 'Botswana SI 48 of 2017, Table 4', BW_URL, date(2017, 4, 28),
                   depends_on=('gross',))]


def _bw_exit(p: VehicleProfile, to: str):
    _, one_way, ret = bw_band(p.gross_kg)
    return [Charge('bw_return_permit_supplement', 'border_crossing',
                   f'Botswana permit, return trip (P{ret} return less P{one_way} one way)',
                   'BWP', ret - one_way, True, 'Botswana SI 48 of 2017, Table 4', BW_URL, date(2017, 4, 28),
                   depends_on=('gross',),
                   notes=['a return permit is bought at entry; this is its extra over the one-way permit'])]


# Namibia RFA Cross-Border Charge per vehicle unit (N$), eff. 1 Aug 2026.
NA_CBC_TRACTOR = {2: 1261, 3: 1601}            # 4+ axles: 3058
NA_CBC_SINGLE_UNIT = {2: 1261, 3: 1601}
NA_CBC_TRAILER = {1: 826, 2: 1261, 3: 1601, 4: 2159}   # 5+ axles: 2621
# Mass Distance Charge, N$ per 100 km, by mass band (upper kg exclusive).
NA_MDC = [(7_000, D('11.11')), (16_000, D('13.44')), (34_000, D('24.38')), (44_000, D('48.92')),
          (10 ** 9, D('73.30'))]


def na_cbc(units) -> Decimal:
    first, trailers = units[0], units[1:]
    if trailers:
        total = NA_CBC_TRACTOR.get(first, 3058 if first >= 4 else 1261)
    else:
        total = NA_CBC_SINGLE_UNIT.get(first, 1601 if first >= 3 else 1261)
    for t in trailers:
        total += NA_CBC_TRAILER.get(t, 2621 if t >= 5 else 826)
    return D(total)


def _na_entry(p: VehicleProfile, frm: str):
    return [Charge('na_cross_border_charge', 'border_crossing',
                   f'Namibia Cross-Border Charge, {p.config} axle units (per entry)',
                   'NAD', na_cbc(p.units), True, 'Namibia RFA fees & tariffs', NA_URL, date(2026, 8, 1),
                   depends_on=('units',))]


def _na_per_km(p: VehicleProfile, km: Decimal):
    if p.gross_kg < 3_500 or km <= 0:
        return []
    rate = next(r for upper, r in NA_MDC if p.gross_kg < upper)
    return [Charge('na_mass_distance_charge', 'non_sa_toll',
                   f'Namibia Mass Distance Charge, {int(km)} km at N${rate}/100 km',
                   'NAD', (rate * km / 100).quantize(D('0.01')), True, 'Namibia RFA fees & tariffs', NA_URL,
                   date(2026, 8, 1), depends_on=('gross',))]


def _ls_entry(p: VehicleProfile, frm: str):
    if p.axles >= 4:
        amt, cls, note = D('650'), '4+ axles', ('M650 is reported under Legal Notice 65 of 2025, not verified; '
                                                'the last official figure is M450 (1 Apr 2022)')
    elif p.axles == 3:
        amt, cls, note = D('190'), '3 axles', 'Road Fund 2022 figure; current rate not verified'
    else:
        amt, cls, note = D('125'), 'heavy, 2 axles', 'Road Fund 2022 figure; current rate not verified'
    return [Charge('ls_toll_gate', 'border_crossing', f'Lesotho toll-gate charge, foreign {cls} (per entry)',
                   'LSL', amt, False, 'Lesotho Road Fund toll-gate fees', LS_URL, date(2022, 4, 1),
                   depends_on=('units',), notes=[note])]


def _sz_entry(p: VehicleProfile, frm: str):
    amt, cls = ((D('450'), '4+ axles') if p.axles >= 4 else (D('400'), '3 axles') if p.axles == 3
                else (D('350'), '2 axles'))
    return [Charge('sz_entry_toll', 'border_crossing', f'Eswatini entry toll, foreign heavy {cls} (per entry)',
                   'SZL', amt, False, 'ERS notice, 1 Oct 2025 (press report)', SZ_URL, date(2025, 10, 1),
                   depends_on=('units',), notes=['only a press report of the ERS notice was found'])]


def _mz_entry(p: VehicleProfile, frm: str):
    return [Charge('mz_insurance_inspection', 'border_crossing',
                   'Mozambique third-party insurance (amortised) + inspection, Lebombo',
                   'ZAR', D('473.29'), False, 'Not verified — no primary source',
                   notes=['no primary source for Mozambique\'s foreign-truck charges; enter your own'])]


def _zm_entry(p: VehicleProfile, frm: str):
    usd = D('10') if p.combination else D('6')
    return [Charge('zm_port_of_entry_toll', 'border_crossing', 'Zambia port-of-entry toll (COMESA/SADC vehicle)',
                   'USD', usd, False, 'Zambia business licensing portal (undated)', ZM_URL,
                   depends_on=('units',), notes=['official portal, but undated'])]


def _zm_per_km(p: VehicleProfile, km: Decimal):
    if km <= 0:
        return []
    return [Charge('zm_road_user_charge', 'non_sa_toll', f'Zambia road user charge, {int(km)} km',
                   'USD', (D('10') * D(ceil(km / 100))), False, 'Press reports (US$10–16 per 100 km)',
                   notes=['rate not verified from a primary source'])]


def _mw_per_km(p: VehicleProfile, km: Decimal):
    if km <= 0:
        return []
    per100 = D('15') if p.combination else D('8')
    return [Charge('mw_transit_fee', 'non_sa_toll', f'Malawi international transit fee, {int(km)} km',
                   'USD', per100 * D(ceil(km / 100)), False, 'Malawi RFA international transit fees (2023)', MW_URL,
                   date(2023, 8, 18), depends_on=('units',),
                   notes=['COMESA rate; whether SA trucks pay it is not confirmed'])]


SCHEDULES = {
    'ZW': {'entry': _zw_entry, 'exit': lambda p, to: [_zw_agent()], 'per_km': _zw_per_km},
    'BW': {'entry': _bw_entry, 'exit': _bw_exit},
    'NA': {'entry': _na_entry, 'per_km': _na_per_km},
    'LS': {'entry': _ls_entry},
    'SZ': {'entry': _sz_entry},
    'MZ': {'entry': _mz_entry},       # tolls: TollPlaza rows (TRAC / REVIMO)
    'ZM': {'entry': _zm_entry, 'per_km': _zm_per_km},
    'MW': {'per_km': _mw_per_km},
}


def has_schedule(country: str) -> bool:
    return country in SCHEDULES
