"""
Cross-border route calculation service.

Determines countries crossed, calculates border fees, weighbridge fees,
and non-SA tolls for international routes.

Cost data is read from DB (BorderCrossingFee, CountryTransitRate) seeded by
seed_cross_border_data, with hardcoded fallbacks when DB has no matching record.

Fixes applied vs original:
  1. Detection now uses resolved TomTom labels (passed by caller).
  2. All costs read from DB — admin-editable without code deploys.
  3. Distance split uses per-country sa_border_distance_km instead of equal division.
  4. SA toll plazas on cross-border routes charged where seeded data exists (N4/MZ).
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Any
from core.formatting import format_zar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Multi-hop route definitions
# ---------------------------------------------------------------------------
MULTI_HOP_ROUTES: dict[tuple[str, str], list[str]] = {
    ('SA', 'ZM'): ['SA', 'ZW', 'ZM'],
    ('SA', 'MW'): ['SA', 'ZW', 'MW'],
    ('SA', 'TZ'): ['SA', 'ZW', 'ZM', 'TZ'],
    ('SA', 'KE'): ['SA', 'ZW', 'ZM', 'TZ', 'KE'],
}

DIRECT_ROUTES = ['ZW', 'MZ', 'BW', 'NA', 'LS', 'SZ']

# ---------------------------------------------------------------------------
# SA C-BRTA permit
# ---------------------------------------------------------------------------
# Per vehicle, PER COUNTRY SERVED (a Zambia load via Zimbabwe needs both).
# Freight Class 1 is a vehicle up to 20 000 kg GROSS mass, Class 2 above it.
# A permit covers a period, so its cost per crossing is the annual fee spread
# over how often the fleet crosses (Company.cross_border_crossings_per_year).
# Government Gazette 54229 (27 Feb 2026), effective 1 Apr 2026:
#   Class 1: R823 application + R6,160 issue = R6,983 a year
#   Class 2: R823 application + R8,218 issue = R9,041 a year
# https://www.cbrta.co.za/uploads/files/2026-C-BRTA-PERMIT-FEES.pdf
CBRTA_SOURCE_URL = 'https://www.cbrta.co.za/uploads/files/2026-C-BRTA-PERMIT-FEES.pdf'
CBRTA_SOURCE_NAME = 'C-BRTA permit fees, GG 54229'
CBRTA_AS_OF = date(2026, 4, 1)
_CBRTA_ANNUAL_CLASS1 = 6_983
_CBRTA_ANNUAL_CLASS2 = 9_041
_CBRTA_CLASS2_GROSS_KG = 20_000       # above this GROSS mass the vehicle is Class 2
_CBRTA_CLASS2_WEIGHT_KG = _CBRTA_CLASS2_GROSS_KG   # old name
_DEFAULT_CROSSINGS_PER_YEAR = 24
_CBRTA_APPLICATION_FEE = 823          # GG 54229 (2026)
_CBRTA_PERMIT_14DAY_CLASS1 = 1_084    # GG 54229 (2026), Class 1, 14 days


def cbrta_annual_permit(gross_kg: float) -> float:
    """Cost of a 12-month C-BRTA permit for one vehicle, one country."""
    return float(_CBRTA_ANNUAL_CLASS2 if (gross_kg or 0) > _CBRTA_CLASS2_GROSS_KG
                 else _CBRTA_ANNUAL_CLASS1)


def amortised_sa_permit(gross_kg: float, crossings_per_year: int | None = None) -> float:
    """One country's C-BRTA permit, as its share of ONE crossing.

    `crossings_per_year` counts legs, so a year of crossings adds up to
    exactly one annual permit."""
    n = int(crossings_per_year or _DEFAULT_CROSSINGS_PER_YEAR)
    if n < 1:
        n = _DEFAULT_CROSSINGS_PER_YEAR
    return round(cbrta_annual_permit(gross_kg) / n, 2)


# ---------------------------------------------------------------------------
# Corridors
# ---------------------------------------------------------------------------
# SA's neighbours (and Zambia via Zimbabwe) are priced from the sourced,
# per-component schedule in core/services/border_schedule.py — each line
# carries its own source, as-of date and verified flag. The earlier single
# per-corridor rand totals (BorderCrossingFee rows for these corridors, and
# the fallback dict that mirrored them) are retired by migration 0165.
#
# BorderCrossingFee / CountryTransitRate rows still price any OTHER corridor
# an admin adds; those figures are shown as estimates.
#
# Which crossings a schedule covers: entry into the country FROM these
# neighbours, and the way back out TO these.
#
# * Namibia's Cross-Border Charge is per entry from ANY country (RFA), so
#   Botswana→Namibia (Trans-Kalahari / Mamuno) is covered.
# * Botswana's SACU single-trip permit (SI 48/2017) is for entering Botswana
#   whichever side the truck comes from (SA, Namibia, Zimbabwe); leaving
#   Botswana for Namibia costs nothing on the Botswana side.
# * Zimbabwe's Zimborders access toll is Beitbridge's (from SA) only; entry
#   from Botswana/Zambia/Mozambique has no source and stays unknown, as does
#   Mozambique↔Zimbabwe.
SCHEDULE_ENTRY_FROM = {'ZW': {'SA'}, 'BW': {'SA', 'NA', 'ZW'}, 'NA': {'SA', 'BW'}, 'LS': {'SA'},
                       'SZ': {'SA'}, 'MZ': {'SA'}, 'ZM': {'ZW'}}
SCHEDULE_EXIT_TO = {'ZW': {'SA'}, 'BW': {'SA', 'NA', 'ZW'}, 'NA': {'SA', 'BW'}, 'LS': {'SA'},
                    'SZ': {'SA'}, 'MZ': {'SA'}, 'ZM': {'ZW'}}

# Retired: no per-corridor rand totals and no weighbridge fees remain (no
# country charges a compliant truck for weighing).
_FALLBACK_BORDER_FEES: dict[str, float] = {}
_FALLBACK_WEIGHBRIDGE: dict[str, int] = {}
_FALLBACK_TOLL_FLAT: dict[str, float] = {}
# Approximate km from the usual SA origin to the border, only to split a
# route's distance when the router gave no country sections.
_FALLBACK_SA_BORDER_KM: dict[str, float] = {
    'ZW': 580.0, 'MZ': 450.0, 'BW': 290.0, 'NA': 666.0,
    'LS': 150.0, 'SZ': 335.0, 'ZM': 580.0, 'MW': 580.0,
}


def _db_fee(from_country: str, to_country: str, gross_kg: float):
    """(fee, exact) from an admin-managed BorderCrossingFee row, or (0, True)."""
    try:
        from core.models.border_crossing_fee import BorderCrossingFee
        fee, row, exact = BorderCrossingFee.get_fee_for_weight(from_country, to_country, gross_kg)
        if fee and float(fee) > 0:
            return float(fee), exact
    except Exception as exc:
        logger.warning('border fee lookup failed for %s-%s: %s', from_country, to_country, exc)
    return 0.0, True


def _db_rate(country: str):
    try:
        from core.models.country_transit_rate import CountryTransitRate
        return CountryTransitRate.objects.filter(country_code=country, is_active=True).first()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Country detection
# ---------------------------------------------------------------------------

def _extract_country(location: str) -> str:
    loc = location.upper().strip()

    if any(kw in loc for kw in [
        'JOHANNESBURG', 'JHB', 'CAPE TOWN', 'CPT', 'DURBAN', 'DBN',
        'PRETORIA', 'PTA', 'BLOEMFONTEIN', 'BFN', 'PORT ELIZABETH',
        'GQEBERHA', 'EAST LONDON', 'NELSPRUIT', 'MBOMBELA', 'POLOKWANE',
        'RUSTENBURG', 'KIMBERLEY', 'GEORGE', 'SOUTH AFRICA',
        # Major SA cities/areas not in the short list above
        'KEMPTON PARK', 'EKURHULENI', 'SANDTON', 'MIDRAND', 'CENTURION',
        'BOKSBURG', 'GERMISTON', 'BENONI', 'SOWETO', 'ROODEPOORT',
        'RANDBURG', 'KRUGERSDORP', 'WITBANK', 'EMALAHLENI', 'SECUNDA',
        'PIETERMARITZBURG', 'PMB', 'RICHARDS BAY', 'NEWCASTLE', 'VEREENIGING',
        'VANDERBIJLPARK', 'SASOLBURG', 'UPINGTON', 'SPRINGBOK', 'VREDENDAL',
        'WORCESTER', 'STELLENBOSCH', 'PAARL', 'MALMESBURY', 'BEAUFORT WEST',
        'OR TAMBO', 'KING SHAKA', 'BRAM FISCHER', 'LANSERIA',
    ]):
        return 'SA'

    # NOTE: Do NOT use short country-code suffixes like ', KE' or ', ZW' here —
    # they are substrings of SA place names (e.g. ', KE' matches ', KEMPTON',
    # ', ZW' matches ', ZWELITSHA', ', NA' matches ', NAPIER'). Use full city
    # or country names only.
    if any(kw in loc for kw in ['HARARE', 'BULAWAYO', 'MUTARE', 'ZIMBABWE']):
        return 'ZW'
    if any(kw in loc for kw in ['LUSAKA', 'NDOLA', 'KITWE', 'LIVINGSTONE', 'ZAMBIA']):
        return 'ZM'
    if any(kw in loc for kw in ['MAPUTO', 'BEIRA', 'NAMPULA', 'MOZAMBIQUE']):
        return 'MZ'
    if any(kw in loc for kw in ['GABORONE', 'FRANCISTOWN', 'MAUN', 'BOTSWANA']):
        return 'BW'
    if any(kw in loc for kw in ['WINDHOEK', 'WALVIS BAY', 'SWAKOPMUND', 'NAMIBIA']):
        return 'NA'
    if any(kw in loc for kw in ['LILONGWE', 'BLANTYRE', 'MALAWI']):
        return 'MW'
    if any(kw in loc for kw in ['DAR ES SALAAM', 'DODOMA', 'ARUSHA', 'TANZANIA']):
        return 'TZ'
    if any(kw in loc for kw in ['NAIROBI', 'MOMBASA', 'KISUMU', 'KENYA']):
        return 'KE'
    if any(kw in loc for kw in ['MASERU', 'LESOTHO']):
        return 'LS'
    if any(kw in loc for kw in ['MBABANE', 'MANZINI', 'ESWATINI', 'SWAZILAND']):
        return 'SZ'
    if any(kw in loc for kw in ['LUANDA', 'LUBANGO', 'ONDJIVA', 'ANGOLA']):
        return 'AO'

    return 'SA'


# TomTom ISO 2/3-letter → internal 2-letter codes used throughout this service
_ISO_TO_INTERNAL: dict[str, str] = {
    'ZA': 'SA', 'ZAF': 'SA',
    'SZ': 'SZ', 'SWZ': 'SZ', 'ESW': 'SZ',
    'MZ': 'MZ', 'MOZ': 'MZ',
    'ZW': 'ZW', 'ZWE': 'ZW',
    'BW': 'BW', 'BWA': 'BW',
    'NA': 'NA', 'NAM': 'NA',
    'LS': 'LS', 'LSO': 'LS',
    'ZM': 'ZM', 'ZMB': 'ZM',
    'MW': 'MW', 'MWI': 'MW',
    'TZ': 'TZ', 'TZA': 'TZ',
    'KE': 'KE', 'KEN': 'KE',
    # Beyond the corridors with fees on file: detected (never dropped), but
    # their border costs are NOT known — said so, never estimated.
    'AO': 'AO', 'AGO': 'AO',
    'CD': 'CD', 'COD': 'CD',
    'CG': 'CG', 'COG': 'CG',
    'UG': 'UG', 'UGA': 'UG',
    'RW': 'RW', 'RWA': 'RW',
    'BI': 'BI', 'BDI': 'BI',
    'MG': 'MG', 'MDG': 'MG',
}

COUNTRY_NAMES: dict[str, str] = {
    'SA': 'South Africa', 'ZW': 'Zimbabwe', 'MZ': 'Mozambique', 'BW': 'Botswana', 'NA': 'Namibia',
    'LS': 'Lesotho', 'SZ': 'Eswatini', 'ZM': 'Zambia', 'MW': 'Malawi', 'TZ': 'Tanzania', 'KE': 'Kenya',
    'AO': 'Angola', 'CD': 'the DR Congo', 'CG': 'the Republic of the Congo', 'UG': 'Uganda', 'RW': 'Rwanda',
    'BI': 'Burundi', 'MG': 'Madagascar',
}


def internal_country(code: str):
    """Internal 2-letter code for a TomTom ISO 2/3-letter code; an unmapped
    2-letter code is kept as-is (detected, costs unknown); None if empty."""
    code = (code or '').strip().upper()
    if not code:
        return None
    if code in _ISO_TO_INTERNAL:
        return _ISO_TO_INTERNAL[code]
    return code if len(code) == 2 and code.isalpha() else None


def country_costs_known(country: str) -> bool:
    """True when the app has this country's charges (a sourced schedule or an
    admin row); False for e.g. Angola — then nothing is estimated."""
    from core.services.border_schedule import has_schedule
    if country == 'SA' or has_schedule(country):
        return True
    return _db_rate(country) is not None


def corridor_fee_known(from_country: str, to_country: str) -> bool:
    """A crossing is known when the country ENTERED has sourced entry
    charges for it (or, into SA, the country left has its exit rules). An
    exit rule on the side being left never makes an unknown entry known —
    e.g. Botswana→Zimbabwe: Zimbabwe's entry charges away from Beitbridge
    have no source, so the crossing is unknown and the quote blocks."""
    if to_country != 'SA':
        if from_country in SCHEDULE_ENTRY_FROM.get(to_country, ()):
            return True
    elif to_country in SCHEDULE_EXIT_TO.get(from_country, ()):
        return True
    try:
        from core.models.border_crossing_fee import BorderCrossingFee
        return BorderCrossingFee.get_fee(from_country, to_country) > 0
    except Exception:
        return False


def detect_countries(
    origin: str,
    destination: str,
    origin_iso: str = '',
    dest_iso: str = '',
) -> list[str] | None:
    """
    Detect route countries from resolved address strings or ISO country codes.

    Pass ``origin_iso`` / ``dest_iso`` (TomTom ``address.countryCode``) when
    available — they take precedence over keyword matching and eliminate the
    risk of a city name not being in the keyword list.

    Returns list of country codes in order, or None for domestic SA.
    """
    origin_country = internal_country(origin_iso) if origin_iso else None
    origin_country = origin_country or _extract_country(origin)

    dest_country = internal_country(dest_iso) if dest_iso else None
    dest_country = dest_country or _extract_country(destination)

    if origin_country == 'SA' and dest_country == 'SA':
        return None

    route_key = (origin_country, dest_country)
    if route_key in MULTI_HOP_ROUTES:
        return MULTI_HOP_ROUTES[route_key]

    if origin_country == 'SA' and dest_country in DIRECT_ROUTES:
        return ['SA', dest_country]
    if dest_country == 'SA' and origin_country in DIRECT_ROUTES:
        return [origin_country, 'SA']

    # Unknown combination (e.g. SA -> Angola with no route sections): still
    # cross-border — the costs of an unmapped corridor are reported unknown,
    # never treated as a domestic trip.
    logger.warning('Cannot determine the full cross-border route for %r → %r', origin, destination)
    if origin_country != dest_country:
        return [origin_country, dest_country]
    return None


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def country_distances_km(geometry: list, sections: list) -> dict[str, float]:
    """Actual kilometres driven in each country, measured off the route.

    TomTom already returns COUNTRY sections (we ask for them in the routing
    call) carrying an ISO code and start/end indices into the geometry, so the
    real split is there for the summing. What it replaces was a guess:

        sa_km    = min(<per-country constant>, distance * 0.9)
        other_km = max(distance - sa_km, distance * 0.1)

    — where the constant was the road distance from ONE assumed origin city to
    that border (Namibia's, for instance, is measured from Cape Town). For a
    Johannesburg load it was wrong in both directions, and on a short route the
    0.1 floor decided the answer outright: every ~500km route came back as
    "50km in Namibia" whatever the map said.

    Returns {} when the sections aren't usable, and the caller keeps the old
    estimate — a bad measurement is worse than an honest approximation.
    """
    from core.services.toll_calculator import _haversine_m

    if not geometry or not sections:
        return {}
    out: dict[str, float] = {}
    for sec in sections:
        code = (sec.get('country_code') or sec.get('countryCode') or '').upper()
        if not code:
            continue
        start, end = sec.get('start'), sec.get('end')
        if start is None or end is None or end <= start:
            continue
        seg = geometry[start:end + 1]
        metres = sum(
            _haversine_m(seg[i]['lat'], seg[i]['lon'], seg[i + 1]['lat'], seg[i + 1]['lon'])
            for i in range(len(seg) - 1)
        )
        key = _ISO_TO_INTERNAL.get(code, code)
        out[key] = out.get(key, 0.0) + metres / 1000.0
    return {k: round(v, 1) for k, v in out.items() if v > 0}


def _db_line(kind, description, amount, detail):
    return {'type': kind, 'code': 'admin_estimate', 'description': f'{description} (estimate)',
            'amount': round(float(amount), 2), 'currency': 'ZAR', 'amount_foreign': round(float(amount), 2),
            'fx': None, 'verified': False, 'label': 'estimate', 'source': 'Admin-entered figure',
            'source_url': '', 'as_of': None, 'detail': detail}


# A foreign stretch shorter than this, between two stretches of the same
# country (or at either end of the route), is map noise at a border post —
# e.g. a delivery to the SA side of Beitbridge whose last few hundred metres
# fall inside Zimbabwe on TomTom's map. It is not a crossing.
MIN_CROSSING_KM = 2.0


def route_countries(geometry: list, sections: list, origin_country: str | None = None,
                    dest_country: str | None = None, min_km: float = MIN_CROSSING_KM) -> list[str]:
    """The countries a route really drives through, in order, from TomTom's
    COUNTRY sections.

    Foreign stretches before the first / after the last stretch in the
    pick-up / delivery country (as the geocoder places them) are dropped —
    leaving and coming back needs both a way out and a way in. Of the rest,
    a stretch counts only when it is at least `min_km` long.
    The endpoints' own countries are then added only if the route does not
    already start / end there — so a trip that starts and ends in SA is
    cross-border only when it actually leaves SA for a real distance.
    """
    from core.services.toll_calculator import _haversine_m

    stretches = []   # [country, km]
    for sec in sorted((s for s in (sections or []) if (s.get('type') in (None, 'COUNTRY'))
                       and (s.get('country_code') or s.get('countryCode'))),
                      key=lambda s: s.get('start') or 0):
        code = internal_country(sec.get('country_code') or sec.get('countryCode'))
        start, end = sec.get('start'), sec.get('end')
        km = 0.0
        if geometry and start is not None and end is not None and end > start:
            seg = geometry[start:end + 1]
            km = sum(_haversine_m(seg[i]['lat'], seg[i]['lon'], seg[i + 1]['lat'], seg[i + 1]['lon'])
                     for i in range(len(seg) - 1)) / 1000.0
        elif not geometry:
            km = float('inf')   # no geometry to measure: trust the section
        if stretches and stretches[-1][0] == code:
            stretches[-1][1] += km
        else:
            stretches.append([code, km])
    o_c, d_c = internal_country(origin_country or ''), internal_country(dest_country or '')
    # The geocoder knows which side of the border the pick-up and delivery
    # are. A route that ENDS in another country after its last stretch in
    # the delivery country (TomTom snapping a Beitbridge-post address over
    # the bridge) has not crossed: the crossing needs an exit AND an entry.
    # Same at the start.
    if d_c and any(c == d_c for c, _ in stretches):
        last = max(i for i, (c, _) in enumerate(stretches) if c == d_c)
        stretches = stretches[:last + 1]
    if o_c and any(c == o_c for c, _ in stretches):
        first = min(i for i, (c, _) in enumerate(stretches) if c == o_c)
        stretches = stretches[first:]
    kept = [c for c, km in stretches if km >= min_km]
    out = []
    for c in kept:
        if not out or out[-1] != c:
            out.append(c)
    if o_c and (not out or out[0] != o_c):
        out.insert(0, o_c)
    if d_c and (not out or out[-1] != d_c):
        out.append(d_c)
    return out


# Border posts on SA's borders (and Botswana–Namibia), from OpenStreetMap
# barrier=border_control nodes (read 8 Oct 2026), named "SA side / other side".
BORDER_POSTS = [
    ('Beitbridge', -22.2206, 29.9858),
    ('Groblersbrug / Martin\'s Drift', -22.9994, 27.9438),
    ('Pont Drift', -22.2169, 29.1399),
    ('Stockpoort / Parr\'s Halt', -23.4022, 27.3527),
    ('Derdepoort / Sikwane', -24.6430, 26.4041),
    ('Kopfontein / Tlokweng', -24.7072, 26.0946),
    ('Swartkopfontein / Ramotswa', -24.8740, 25.8863),
    ('Skilpadshek / Pioneer Gate', -25.2750, 25.7134),
    ('Ramatlabama', -25.6489, 25.5755),
    ('Bray', -25.4571, 23.7147),
    ('McCarthy\'s Rest', -26.2027, 22.5689),
    ('Middelputs / Middlepits', -26.6755, 21.8847),
    ('Nakop / Ariamsvlei', -28.0918, 20.0107),
    ('Vioolsdrif / Noordoewer', -28.7705, 17.6260),
    ('Onseepkans / Velloorsdrift', -28.7394, 19.3037),
    ('Alexander Bay / Oranjemund', -28.5684, 16.5058),
    ('Sendelingsdrif', -28.1230, 16.8911),
    ('Rietfontein / Klein Menasse', -26.7562, 20.0001),
    ('Mata-Mata', -25.7674, 19.9997),
    ('Trans-Kalahari: Mamuno / Buitepos', -22.2808, 20.0053),
    ('Lebombo / Ressano Garcia', -25.4425, 31.9858),
    ('Kosi Bay / Ponta do Ouro', -26.8644, 32.8294),
    ('Giriyondo', -23.5838, 31.6601),
    ('Pafuri', -22.4492, 31.3162),
    ('Oshoek / Ngwenya', -26.2129, 30.9884),
    ('Mahamba', -27.1054, 31.0696),
    ('Golela / Lavumisa', -27.3180, 31.8881),
    ('Jeppe\'s Reef / Matsamo', -25.7504, 31.4687),
    ('Mananga', -25.9340, 31.7616),
    ('Nerston / Sandlane', -26.5696, 30.7911),
    ('Josefsdal / Bulembu', -25.9433, 31.1182),
    ('Onverwacht / Salitje', -27.3165, 31.6438),
    ('Bothashoop / Gege', -26.9738, 30.9681),
    ('Emahlatini / Sicunusa', -26.8615, 30.9077),
    ('Waverley / Lundzi', -26.3263, 30.8857),
    ('Maseru Bridge', -29.2989, 27.4560),
    ('Ficksburg Bridge / Maputsoe', -28.8825, 27.8884),
    ('Caledonspoort', -28.6964, 28.2346),
    ('Van Rooyen\'s Gate', -29.7564, 27.1084),
    ('Qacha\'s Nek', -30.1323, 28.6840),
    ('Sani Pass', -29.5845, 29.2857),
    ('Peka Bridge', -28.9466, 27.7357),
    ('Makhaleng Bridge', -30.1652, 27.4003),
    ('Telle Bridge', -30.4327, 27.5682),
    ('Ongeluksnek', -30.3431, 28.3111),
    ('Ramatseliso\'s Gate', -30.0506, 28.9331),
    ('Monantsa Pass', -28.5823, 28.6989),
    ('Bushman\'s Nek', -29.8440, 29.2115),
]
BORDER_POST_MAX_KM = 15.0


def nearest_border_post(lat: float, lng: float, max_km: float = BORDER_POST_MAX_KM):
    from core.services.toll_calculator import _haversine_m
    best = min(BORDER_POSTS, key=lambda p: _haversine_m(lat, lng, p[1], p[2]))
    return best[0] if _haversine_m(lat, lng, best[1], best[2]) <= max_km * 1000 else None


def section_crossings(geometry: list, sections: list) -> list:
    """[(from, to, lat, lng)] where consecutive COUNTRY sections meet."""
    secs = sorted((s for s in (sections or []) if (s.get('country_code') or s.get('countryCode'))
                   and s.get('start') is not None), key=lambda s: s['start'])
    out = []
    for a, b in zip(secs, secs[1:]):
        fc = internal_country(a.get('country_code') or a.get('countryCode'))
        tc = internal_country(b.get('country_code') or b.get('countryCode'))
        idx = b['start']
        if fc != tc and geometry and 0 <= idx < len(geometry):
            out.append((fc, tc, float(geometry[idx]['lat']), float(geometry[idx]['lon'])))
    return out


def border_post_for(crossings, fc: str, tc: str):
    for c_from, c_to, lat, lng in crossings or []:
        if (c_from, c_to) == (fc, tc):
            return nearest_border_post(lat, lng)
    return None


def calculate_cross_border_costs(
    countries: list[str],
    distance_km: float,
    vehicle_type: str = 'truck',
    weight_kg: float = 0,
    crossings_per_year: int | None = None,
    country_km: dict[str, float] | None = None,
    vehicle_capacity_kg: float = 0,
    *,
    gross_mass_kg: float | None = None,
    axle_config: str | None = None,
    sanral_class: int | None = None,
    today: date | None = None,
    overrides: dict | None = None,
    abnormal_load: bool = False,
    crossings: list | None = None,
) -> dict[str, Any]:
    """Border, permit and in-country charges for ONE leg, in travel order.

    `countries` is the leg's own order: ['SA', 'NA'] going out, ['NA', 'SA']
    coming back. Entry charges apply on entering a country, exit charges
    only where a schedule has them (Botswana's return-permit supplement,
    clearing on the way back), per-km charges on every leg.

    The vehicle is described by its gross mass and axle units (what the
    schedules are written about); when the vehicle type does not give them
    they are inferred from the SANRAL class, or from the load, and every
    line that depends on them is labelled an estimate.

    `crossings` [(from, to, lat, lng)] — where the route crosses each border
    (section_crossings); the border post there is named on its lines.
    `abnormal_load` — the user marked the load abnormal (Zimbabwe's
    Abnormal access-toll class).
    `overrides` {component code: rand} replaces an estimate with the user's
    own figure (e.g. {'zw_clearing_agent': 1800} — their agent's fee).
    """
    from core.services import border_schedule as bs

    empty = {'border_fees': 0, 'weighbridge_fees': 0, 'non_sa_tolls': 0, 'total': 0, 'breakdown': [],
             'unknown_countries': [], 'unknown_crossings': [], 'complete': True,
             'estimate_zar': 0.0, 'verified': True, 'vehicle_profile': None}
    if not countries or len(countries) <= 1:
        return empty
    profile = bs.vehicle_profile(gross_mass_kg=gross_mass_kg, axle_config=axle_config, sanral_class=sanral_class,
                                 weight_kg=weight_kg, vehicle_capacity_kg=vehicle_capacity_kg,
                                 abnormal_load=abnormal_load)
    unknown_countries = [c for c in countries if not country_costs_known(c)]
    unknown_crossings = [f'{countries[i]}-{countries[i + 1]}' for i in range(len(countries) - 1)
                         if not corridor_fee_known(countries[i], countries[i + 1])]
    lines: list[dict] = []
    # A leg that starts outside SA is an SA truck's way back. Where a country
    # sells a return permit (Botswana), re-entering it on the way back costs
    # the return permit's extra, not a second one-way permit; and leaving a
    # country entered on this same leg owes no exit extra.
    homebound = countries[0] != 'SA'
    entered: set = set()

    # --- crossings ---
    for i in range(len(countries) - 1):
        fc, tc = countries[i], countries[i + 1]
        if f'{fc}-{tc}' in unknown_crossings:
            continue
        sched_entry = tc != 'SA' and fc in SCHEDULE_ENTRY_FROM.get(tc, ())
        sched_exit = tc in SCHEDULE_EXIT_TO.get(fc, ())
        charges = []
        if sched_entry:
            entered.add(tc)
            sched = bs.SCHEDULES[tc]
            if homebound and 'return_entry' in sched:
                charges += sched['return_entry'](profile, fc)
            else:
                charges += sched.get('entry', lambda p, f: [])(profile, fc)
        if sched_exit and fc not in entered:
            charges += bs.SCHEDULES[fc].get('exit', lambda p, t: [])(profile, tc)
        if not (sched_entry or sched_exit):
            fee, exact = _db_fee(fc, tc, profile.gross_kg)
            if fee > 0:
                lines.append(_db_line('border_crossing', f'{fc} → {tc} border crossing', fee,
                                      '' if exact else 'priced at the heaviest band on file'))
            continue
        post = border_post_for(crossings, fc, tc)
        for ch in charges:
            if post and post not in ch.description:
                ch.description = f'{ch.description} — {post}'
            if overrides and overrides.get(ch.code) is not None:
                ch.amount, ch.currency = Decimal(str(overrides[ch.code])), 'ZAR'
                ch.tariff_verified, ch.depends_on = True, ()
                ch.source_name, ch.source_url, ch.as_of, ch.notes = 'Your figure', '', None, []
            lines.append(bs.to_line(ch, profile, today))

    # --- SA C-BRTA permit: one per foreign country served ---
    if 'SA' in countries:
        n = int(crossings_per_year or _DEFAULT_CROSSINGS_PER_YEAR)
        per = amortised_sa_permit(profile.gross_kg, crossings_per_year)
        cls = 2 if profile.gross_kg > _CBRTA_CLASS2_GROSS_KG else 1
        served = [c for c in dict.fromkeys(countries) if c != 'SA' and c not in unknown_countries]
        for c in served:
            note = profile.gross_note()
            lines.append({
                'type': 'sa_permit', 'code': 'sa_cbrta_permit',
                'description': (f'SA C-BRTA Class {cls} permit for {COUNTRY_NAMES.get(c, c)} '
                                f'({format_zar(cbrta_annual_permit(profile.gross_kg), 0)}/yr over {n} crossings)'
                                + ('' if profile.gross_known else ' (estimate)')),
                'amount': per, 'currency': 'ZAR', 'amount_foreign': per, 'fx': None,
                'verified': profile.gross_known, 'label': 'published' if profile.gross_known else 'estimate',
                'source': CBRTA_SOURCE_NAME, 'source_url': CBRTA_SOURCE_URL, 'as_of': CBRTA_AS_OF.isoformat(),
                'detail': '; '.join(x for x in (f'class by gross mass, {note}' if note else '',
                                                'amortised over the fleet\'s crossings a year') if x),
            })

    # --- per-km charges inside each foreign country ---
    non_sa = [c for c in dict.fromkeys(countries) if c != 'SA' and c not in unknown_countries]
    if non_sa:
        measured = country_km or {}
        sa_km = min(_FALLBACK_SA_BORDER_KM.get(non_sa[0], 500.0), distance_km * 0.9)
        split = max(distance_km - sa_km, distance_km * 0.1) / len(non_sa)
        for c in non_sa:
            km = measured.get(c)
            approx = km is None
            km_d = Decimal(str(round(km if km is not None else split, 1)))
            if bs.has_schedule(c):
                for ch in bs.SCHEDULES[c].get('per_km', lambda p, k, h=False: [])(profile, km_d, homebound):
                    if approx:
                        # The km is guessed, so the amount is an estimate whatever the tariff.
                        ch.tariff_verified = False
                        ch.notes.append(f'~{int(km_d)} km is a rough split; the route had no country sections')
                    lines.append(bs.to_line(ch, profile, today))
            else:
                r = _db_rate(c)
                if r is not None and float(r.toll_rate_per_km or 0) > 0:
                    lines.append(_db_line('non_sa_toll', f'{c} road charges ({"~" if approx else ""}{bs.km_text(km_d)})',
                                          float(km_d) * float(r.toll_rate_per_km), 'admin rate per km'))

    border = sum(ln['amount'] for ln in lines if ln['type'] in ('border_crossing', 'sa_permit'))
    tolls = sum(ln['amount'] for ln in lines if ln['type'] == 'non_sa_toll')
    estimate = sum(ln['amount'] for ln in lines if not ln['verified'])
    return {
        'border_fees':      round(border, 2),
        'weighbridge_fees': 0,
        'non_sa_tolls':     round(tolls, 2),
        'total':            round(border + tolls, 2),
        'breakdown':        lines,
        'unknown_countries': unknown_countries,
        'unknown_crossings': unknown_crossings,
        'complete': not unknown_countries and not unknown_crossings,
        'estimate_zar': round(estimate, 2),
        'verified': all(ln['verified'] for ln in lines),
        'vehicle_profile': {'gross_kg': profile.gross_kg, 'gross_source': profile.gross_source,
                            'gross_assumed': not profile.gross_known,
                            'axle_config': profile.config, 'axle_config_source': profile.units_source,
                            'axle_config_assumed': not profile.units_known,
                            'assumptions': profile.assumptions()},
    }


# NOTE: SA-side SANRAL toll charging for cross-border routes is NOT handled
# in this module (a country-keyed highway lookup used to live here —
# calculate_sa_tolls_for_cross_border, removed 2026-09 — but it was dead
# code, never actually called). The real, live mechanism is
# core.services.toll_calculator.calculate_tolls_by_geometry, invoked from
# RouteCalculatorView (core/views.py) for every route — cross-border or not
# — using the real TomTom route polyline geofenced against every seeded
# TollPlaza by GPS coordinates. That's why a Durban-origin trip to Lesotho
# correctly picks up Mariannhill/Mooi River (both on the N3 the real route
# actually drives through) with no per-country/per-highway mapping needed at
# all — verified directly against those plazas' real coordinates. Don't
# reintroduce a static highway-lookup version of this; it can only ever
# cover the handful of corridors someone remembered to add.


def get_cross_border_warnings(countries: list[str]) -> list[str]:
    if not countries or len(countries) <= 1:
        return []
    warnings = []
    for c in countries:
        if not country_costs_known(c):
            warnings.append(f'Border costs for {COUNTRY_NAMES.get(c, c)} not known: add them to the quote '
                            'by hand.')
    if 'ZW' in countries:
        warnings.append('Zimbabwe crossing: ensure cargo insurance and customs documentation')
    if any(c in countries for c in ['ZM', 'MW', 'TZ', 'KE']):
        warnings.append('Multi-country route: allow 2–3 days extra for border clearances')
    if len(countries) > 3:
        warnings.append('Long-haul cross-border: recommend experienced driver with valid passport')
    return warnings
