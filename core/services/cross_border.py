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
from decimal import Decimal
from typing import Any

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
# Hardcoded fallbacks (used when DB has no matching record)
# ---------------------------------------------------------------------------
# SA border fees: CBRTA application fee (R 798) + 14-day Class 1 permit (R 1 050)
# = R 1 848 per SA crossing. Source: Government Gazette No. 52198, 28 Feb 2025
# (Cross-Border Road Transport Act Amended Regulations, 2025), effective 1 Apr 2025.
# Non-SA multi-hop crossings remain 2024 industry estimates (not covered by SA gazette).
# C-BRTA permit, per vehicle PER COUNTRY. Freight Class 1 is up to 20 000kg,
# Class 2 above it — the load's own weight picks the class. The 12-month totals
# are what matter: a permit covers a period, so its cost per crossing is the
# annual fee spread over how often the fleet actually crosses
# (Company.cross_border_crossings_per_year).
#
# Class 2 is the current figure: Government Gazette 54229, 27 Feb 2026,
# effective 1 Apr 2026 — the same source migrations 0112/0113/0114 priced the
# corridor fees from.
#
# Class 1 is still the SUPERSEDED 2025 schedule (Gazette 52198, eff. 1 Apr
# 2025: R798 application + R5,969 issue). The 2026 gazette raised Class 2 from
# R8,761 to R9,041, so Class 1 has almost certainly moved too — it is simply
# not in any source we hold. It therefore under-charges loads at or below
# 20 000kg by an unknown margin. Replace the moment the 2026 Class 1 figure is
# to hand; nothing else needs to change.
_CBRTA_ANNUAL_CLASS1 = 6_767   # 2025 gazette — SUPERSEDED, see above
_CBRTA_ANNUAL_CLASS2 = 9_041   # 2026 gazette 54229, effective 1 Apr 2026
_CBRTA_CLASS2_WEIGHT_KG = 20_000       # above this the load is Class 2
_DEFAULT_CROSSINGS_PER_YEAR = 24

# Kept for the 14-day temporary-permit case, which nothing prices off yet.
_CBRTA_APPLICATION_FEE = 798    # Schedule 1, Part B (2025)
_CBRTA_PERMIT_14DAY_CLASS1 = 1_050  # Schedule 2, Part B, Class 1, 14 days (2025)
_CBRTA_SA_BORDER_FEE = _CBRTA_APPLICATION_FEE + _CBRTA_PERMIT_14DAY_CLASS1  # 1 848


def cbrta_annual_permit(weight_kg: float) -> float:
    """Cost of a 12-month C-BRTA permit for one vehicle, one country."""
    return float(_CBRTA_ANNUAL_CLASS2 if (weight_kg or 0) > _CBRTA_CLASS2_WEIGHT_KG
                 else _CBRTA_ANNUAL_CLASS1)


def amortised_sa_permit(weight_kg: float, crossings_per_year: int | None = None) -> float:
    """The C-BRTA permit's share of ONE crossing.

    Charged per crossing rather than per trip, so a round trip pays it twice —
    which is right as long as `crossings_per_year` counts legs, not trips. Over
    a year the total then comes back to exactly one annual permit.
    """
    n = int(crossings_per_year or _DEFAULT_CROSSINGS_PER_YEAR)
    if n < 1:
        n = _DEFAULT_CROSSINGS_PER_YEAR
    return round(cbrta_annual_permit(weight_kg) / n, 2)

# Per-corridor crossing fee = SA-side CBRTA permit + destination-country entry costs
# (road-access, carbon tax, third-party insurance, gate pass) folded into one number.
# Every SADC neighbour SA actually borders (ZW, BW, MZ, LS, NA, SZ) is now
# corrected against primary sources (2026-09) — see core/migrations/
# 0110_fix_zw_border_fee.py, 0112_fix_bw_mz_border_fees.py,
# 0113_fix_ls_border_fee.py and 0114_fix_na_sz_border_fees.py for exact
# sourcing (Zimborders' tariff page, Botswana's SI 48/2017 permit schedule,
# TRAC N4's own toll tariff, Lesotho's Toll-Gate Act gazette, Namibia's RFA
# tariff, Eswatini's ERS notice, the 2026 C-BRTA permit gazette). The
# further multi-hop crossings below (ZW-ZM, ZW-MW, ZM-TZ, TZ-KE) remain
# unverified industry estimates — DB rows override these fallbacks; this
# dict only matters when a DB lookup misses.
#
# DESTINATION-COUNTRY CHARGES ONLY, heavy band (>20,000kg). The SA C-BRTA permit used to be folded into
# each of these at a flat R376.71 (a figure that only holds at 24 crossings a
# year); it is now computed per quote by amortised_sa_permit() from the annual
# gazetted fee, the load's weight class and the fleet's own crossing count. It
# must not reappear here or every crossing is charged for it twice. See
# migration 0116_split_sa_permit_out_of_border_fees.
_ZW_CROSSING_FEE = 5550.00    # Beitbridge bridge toll + SA-side clearing agent
_BW_CROSSING_FEE = 1173.29    # Botswana single-trip permit, 56t band (≈P975)
_MZ_CROSSING_FEE = 473.29     # Mozambique SORCA insurance amortised + inspection fee
_LS_CROSSING_FEE = 650.29     # Lesotho toll-gate charge, foreign 4+ axle (M650)
_NA_CROSSING_FEE = 4463.29    # Namibia RFA Cross-Border Charge, 7-axle interlink (N$4,463)
_SZ_CROSSING_FEE = 450.29     # Eswatini ERS border toll, foreign 4+ axle (E450)
_FALLBACK_BORDER_FEES: dict[str, float] = {
    'SA-ZW': _ZW_CROSSING_FEE,  # Beitbridge (Zimbabwe is the most expensive corridor)
    'SA-MZ': _MZ_CROSSING_FEE,  # Lebombo / Ressano Garcia
    'SA-BW': _BW_CROSSING_FEE,  # Skilpadshek / Pioneer Gate (Trans-Kalahari)
    'SA-NA': _NA_CROSSING_FEE,  # Vioolsdrift / Ariamsvlei
    'SA-LS': _LS_CROSSING_FEE,  # Maseru Bridge / Maputsoe
    'SA-SZ': _SZ_CROSSING_FEE,  # Oshoek / Ngwenya
    # SA re-entries — same cost class on return
    'ZW-SA': _ZW_CROSSING_FEE, 'MZ-SA': _MZ_CROSSING_FEE,
    'BW-SA': _BW_CROSSING_FEE, 'NA-SA': _NA_CROSSING_FEE,
    'LS-SA': _LS_CROSSING_FEE, 'SZ-SA': _SZ_CROSSING_FEE,
    # Multi-hop crossings — industry estimates (non-SA, not in SA gazette)
    'ZW-ZM': 900, 'ZW-MW': 850,
    'ZM-TZ': 1200, 'TZ-KE': 1100,
}
_FALLBACK_WEIGHBRIDGE: dict[str, int] = {
    # BW/MZ/LS/NA/SZ corrected to 0 (2026-09) — none of the five publishes
    # any weighing fee (gov.bw's and Namibia's Roads Authority weighbridge
    # pages list none; Mozambique's Fundo de Estradas revenue breakdown has
    # no weighing line; Lesotho has never enacted weighbridge/axle
    # legislation; Eswatini's fee schedule has no weighing line either).
    'ZW': 250, 'BW': 0, 'NA': 0, 'MZ': 0,
    'ZM': 280, 'MW': 260, 'TZ': 320, 'KE': 300, 'LS': 0, 'SZ': 0,
}
# Foreign in-country toll/transit rate (ZAR/km). Zimbabwe transit ≈ USD1/10km ≈
# R0.90/km. Botswana, Lesotho and Eswatini corrected to 0 (2026-09) — none of
# the three has any toll roads (their border charges are collected AT the
# border, like a toll-gate, not per km driven inside the country). Mozambique
# corrected to an effective R6.30/km — TRAC N4's real, current toll is a flat
# R598.78 one-way through its two Mozambican plazas (Moamba + Maputo), not
# actually a per-km charge; this rate is that flat total spread over the
# ~95km border-to-Maputo corridor so it fits this model's per-km field, same
# approximation already used for Zimbabwe's per-gate ZINARA tolls. Namibia is
# the one corridor where this field is a REAL per-km charge, not an
# approximation: R0.733/km is the Road Fund Administration's actual
# published Mass Distance Charge for a >44,000kg combination.
_FALLBACK_TOLL_RATE: dict[str, float] = {
    'ZW': 0.90, 'BW': 0.0, 'NA': 0.733, 'MZ': 0.00,
    'ZM': 0.60, 'MW': 0.55, 'TZ': 0.60, 'KE': 0.65, 'LS': 0.0, 'SZ': 0.0,
}
# Approximate km from Johannesburg to SA border post for each neighbour —
# EXCEPT Lesotho (reached from Bloemfontein via the N8, ~150km) and Namibia
# (reached from Cape Town via the N7, ~666km — corrected 2026-09 from a
# 1400km figure that had conflated this with the much longer Cape Town-to-
# Windhoek distance). MZ corrected 380->450 (Komatipoort/Lebombo), BW
# corrected 360->290 (Skilpadshek/Pioneer Gate, the actual main SA-BW
# freight route, not Kopfontein), SZ corrected 380->335 (the real corridor
# is the N17 via Oshoek, not the N4) — 2026-09, alongside the border/toll
# fee corrections above.
# Countries whose tolls are gate-based, not per-km — see migration
# 0118_mozambique_flat_toll. Mirrors CountryTransitRate.toll_flat_zar.
_FALLBACK_TOLL_FLAT: dict[str, float] = {
    'MZ': 598.78,   # TRAC Moamba + Maputo/Matola, Class 4, one way
}
_FALLBACK_SA_BORDER_KM: dict[str, float] = {
    'ZW': 580.0, 'MZ': 450.0, 'BW': 290.0, 'NA': 666.0,
    'LS': 150.0, 'SZ': 335.0, 'ZM': 580.0, 'MW': 580.0,
    'TZ': 580.0, 'KE': 580.0,
}

# ---------------------------------------------------------------------------
# DB helpers (with fallback to hardcoded dicts)
# ---------------------------------------------------------------------------

def _get_border_fee(from_country: str, to_country: str, weight_kg: float = 0):
    """(fee, exact) for this corridor at this weight — see _get_border_fee_raw."""
    try:
        from core.models.border_crossing_fee import BorderCrossingFee
        fee, row, exact = BorderCrossingFee.get_fee_for_weight(from_country, to_country, weight_kg)
        if fee and float(fee) > 0:
            return float(fee), exact
    except Exception as exc:
        logger.warning('border fee lookup failed for %s-%s, using fallback: %s',
                       from_country, to_country, exc)
    return _get_border_fee_raw(from_country, to_country), True


def _get_border_fee_raw(from_country: str, to_country: str) -> float:
    """DB first, hardcoded table only if there is no row.

    The fallback exists so a database blip can't stop a fleet quoting, but a
    stale one is worse than an outage: it returns a plausible wrong number with
    no signal. It is kept in step with the seeded rows by
    test_border_fee_fallbacks_match_db, and every use is logged.
    """
    try:
        from core.models.border_crossing_fee import BorderCrossingFee
        fee = BorderCrossingFee.get_fee(from_country, to_country)
        if fee > 0:
            return float(fee)
    except Exception as exc:
        logger.warning('border fee lookup failed for %s-%s, using fallback: %s',
                       from_country, to_country, exc)
    fallback = _FALLBACK_BORDER_FEES.get(f'{from_country}-{to_country}', 0)
    logger.warning('no BorderCrossingFee row for %s-%s — falling back to R%s',
                   from_country, to_country, fallback)
    return fallback


def _get_country_rate(country: str) -> dict[str, float]:
    try:
        from core.models.country_transit_rate import CountryTransitRate
        r = CountryTransitRate.objects.get(country_code=country, is_active=True)
        return {
            'weighbridge': float(r.weighbridge_fee_zar),
            'toll_rate':   float(r.toll_rate_per_km),
            'toll_flat':   float(getattr(r, 'toll_flat_zar', 0) or 0),
            'sa_border_km': float(r.sa_border_distance_km),
        }
    except Exception:
        pass
    return {
        'weighbridge':  _FALLBACK_WEIGHBRIDGE.get(country, 200),
        'toll_rate':    _FALLBACK_TOLL_RATE.get(country, 0.40),
        'toll_flat':    _FALLBACK_TOLL_FLAT.get(country, 0.0),
        'sa_border_km': _FALLBACK_SA_BORDER_KM.get(country, 500.0),
    }


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
}


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
    origin_country = _ISO_TO_INTERNAL.get(origin_iso.upper()) if origin_iso else None
    origin_country = origin_country or _extract_country(origin)

    dest_country = _ISO_TO_INTERNAL.get(dest_iso.upper()) if dest_iso else None
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

    # Unknown combination — treat as domestic
    logger.warning('Cannot determine cross-border route for %r → %r', origin, destination)
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


def calculate_cross_border_costs(
    countries: list[str],
    distance_km: float,
    vehicle_type: str = 'truck',
    weight_kg: float = 0,
    crossings_per_year: int | None = None,
    country_km: dict[str, float] | None = None,
    vehicle_capacity_kg: float = 0,
) -> dict[str, Any]:
    if not countries or len(countries) <= 1:
        return {'border_fees': 0, 'weighbridge_fees': 0, 'non_sa_tolls': 0, 'total': 0, 'breakdown': []}

    # Both schedules that scale — the destination country's own charge and the
    # SA permit class — are written about the VEHICLE, not the cargo. A 20t
    # payload on a 34t interlink is an interlink either way. So band on the
    # truck's capacity when one is known, and fall back to the load weight when
    # it isn't (a quote can be priced before any truck is picked). This also
    # takes the sting out of the 20 000kg boundary: two loads on the same truck
    # now land in the same band instead of one kilogram costing R1,370.
    banding_kg = float(vehicle_capacity_kg or 0) or float(weight_kg or 0)

    breakdown: list[dict] = []
    border_fees    = 0.0
    weighbridge_fees = 0.0
    non_sa_tolls   = 0.0

    # --- Border crossing fees ---
    # The stored per-corridor fee is the DESTINATION country's own charges
    # (its permit, road tax, insurance, border toll). The SA C-BRTA permit is
    # added separately below because it is the one component whose per-crossing
    # cost depends on the fleet, not the corridor.
    for i in range(len(countries) - 1):
        fc, tc = countries[i], countries[i + 1]
        fee, exact = _get_border_fee(fc, tc, banding_kg)
        if fee > 0:
            border_fees += fee
            # `exact` is False when no band covers this weight and the lightest
            # row on file was used — usually an interlink rate charged to a
            # smaller truck. Say so rather than over-charge silently.
            label = f'{fc} → {tc} border crossing'
            if not exact:
                label += ' (priced for an interlink — no lighter band on file)'
            breakdown.append({'type': 'border_crossing', 'description': label, 'amount': round(fee, 2)})

    # --- SA C-BRTA permit, amortised over the fleet's crossings ---
    # Required on every crossing into or out of SA, so it is charged per SA
    # crossing rather than per corridor — which also means it is no longer
    # possible for one corridor to be missing it, as Zimbabwe's row was.
    sa_crossings = sum(
        1 for i in range(len(countries) - 1)
        if 'SA' in (countries[i], countries[i + 1])
    )
    if sa_crossings:
        permit = amortised_sa_permit(banding_kg, crossings_per_year)
        if permit > 0:
            n = int(crossings_per_year or _DEFAULT_CROSSINGS_PER_YEAR)
            cls = 2 if banding_kg > _CBRTA_CLASS2_WEIGHT_KG else 1
            for _ in range(sa_crossings):
                border_fees += permit
                breakdown.append({
                    'type': 'sa_permit',
                    'description': (f'SA C-BRTA Class {cls} permit '
                                    f'(R{cbrta_annual_permit(banding_kg):,.0f}/yr over {n} crossings)'),
                    'amount': permit,
                })

    # --- Weighbridge fees (one per non-SA country) ---
    for country in countries:
        if country == 'SA':
            continue
        rate = _get_country_rate(country)
        fee = rate['weighbridge']
        weighbridge_fees += fee
        breakdown.append({'type': 'weighbridge', 'description': f'{country} weighbridge', 'amount': round(fee, 2)})

    # --- Non-SA toll costs with accurate distance splitting ---
    # Use sa_border_distance_km to estimate the SA portion of the journey,
    # then distribute the remainder across non-SA countries equally.
    non_sa_countries = [c for c in countries if c != 'SA']
    if non_sa_countries:
        first_foreign = non_sa_countries[0]
        rate_info     = _get_country_rate(first_foreign)
        sa_border_km  = min(rate_info['sa_border_km'], distance_km * 0.9)
        non_sa_total_km = max(distance_km - sa_border_km, distance_km * 0.1)
        dist_per_foreign = non_sa_total_km / len(non_sa_countries)
        measured = country_km or {}

        for country in non_sa_countries:
            r = _get_country_rate(country)
            # A gate-based toll is the same however far the route runs past it;
            # only a genuinely per-km charge scales with distance.
            if r.get('toll_flat', 0) > 0:
                cost = r['toll_flat']
                label = f'{country} tolls (fixed gate charge)'
            else:
                # Measured off the route when the router gave us country
                # sections; the even split is the fallback, and says "~".
                km = measured.get(country)
                cost = (km if km is not None else dist_per_foreign) * r['toll_rate']
                label = (f'{country} tolls ({int(km)} km)' if km is not None
                         else f'{country} tolls (~{int(dist_per_foreign)} km)')
            non_sa_tolls += cost
            breakdown.append({
                'type': 'non_sa_toll',
                'description': label,
                'amount': round(cost, 2),
            })

    total = border_fees + weighbridge_fees + non_sa_tolls
    return {
        'border_fees':      round(border_fees, 2),
        'weighbridge_fees': round(weighbridge_fees, 2),
        'non_sa_tolls':     round(non_sa_tolls, 2),
        'total':            round(total, 2),
        'breakdown':        breakdown,
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
    if 'ZW' in countries:
        warnings.append('Zimbabwe crossing: ensure cargo insurance and customs documentation')
    if any(c in countries for c in ['ZM', 'MW', 'TZ', 'KE']):
        warnings.append('Multi-country route: allow 2–3 days extra for border clearances')
    if len(countries) > 3:
        warnings.append('Long-haul cross-border: recommend experienced driver with valid passport')
    return warnings
