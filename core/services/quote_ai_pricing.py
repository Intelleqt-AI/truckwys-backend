"""OpenAI-based quote price verification (the "AI price analysis" panel).

Deliberately NOT built on core.services.agent._llm_generate: that helper is
bound to the legacy Chat Completions API, has no web_search tool support, and
never reads response.usage. This module talks to the Responses API directly.

Pipeline (the LLM never judges and never does arithmetic):

  1. FUEL: the official monthly diesel price in force today, from the app's
     own FIASA record (core.services.fuel_price) — no web search.
  2. BASE RATE: the lane benchmark from real quotes
     (core.services.lane_benchmark.resolve_market_rate) minus the market
     pass-through, per km — no web search: the web has no trustworthy
     published R/km freight rates.
  3. TOLLS + DRIVER ALLOWANCE: one focused RESEARCH call each, in parallel,
     forced to web-search (tool_choice='required'), then one STRUCTURING
     call (strict json_schema, no tools) that only EXTRACTS the figures and
     their start dates, each tied to source ids S1..Sn from a per-request
     enum. (web_search + strict json_schema in the same call has a
     documented truncation bug, hence two stages.)
  4. SOURCE CHECK (core.services.source_verification): every tariff /
     allowance must appear on a page it cites, dated for the period in
     force (tolls and allowances run from 1 March). Otherwise not used.
  5. Deterministic VERDICTS + PRICING in Python. Base rate is the margin
     lever: price = pass-through (fuel + tolls + driver + cross-border) +
     base rate x km; margin = base-rate share.
  6. Every combination of per-item AI/your choices is priced (and scored by
     the existing win-probability model), so the UI can switch items
     instantly and the price always equals the breakdown total.

Never raises for an anticipated failure — degrades to an honest
{'success': False, ...} and still records spent tokens.
"""
import itertools
import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from openai import OpenAI

from core.services import source_verification

logger = logging.getLogger(__name__)

AI_QUOTE_ANALYSIS_MODEL = getattr(settings, 'AI_QUOTE_ANALYSIS_MODEL', 'gpt-4o-mini')
AI_QUOTE_ANALYSIS_STRUCTURING_MODEL = getattr(settings, 'AI_QUOTE_ANALYSIS_STRUCTURING_MODEL', 'gpt-4o-mini')
AI_QUOTE_ANALYSIS_REASONING_EFFORT = getattr(settings, 'AI_QUOTE_ANALYSIS_REASONING_EFFORT', 'low')
AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE = getattr(settings, 'AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE', 'medium')
AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS = float(getattr(settings, 'AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS', 60))
# One budget for the whole run — kept below nginx's default 60 s proxy
# timeout and the frontend's 70 s wait, so the user never sees
# "unavailable" while a paid run finishes.
AI_QUOTE_ANALYSIS_DEADLINE_SECONDS = float(getattr(settings, 'AI_QUOTE_ANALYSIS_DEADLINE_SECONDS', 55))
AI_QUOTE_ANALYSIS_RESEARCH_TIMEOUT_SECONDS = float(getattr(settings, 'AI_QUOTE_ANALYSIS_RESEARCH_TIMEOUT_SECONDS', 30))
AI_QUOTE_ANALYSIS_MAX_REFERENCES = int(getattr(settings, 'AI_QUOTE_ANALYSIS_MAX_REFERENCES', 8))
# Hard cap on web_search calls PER TOPIC call — API-enforced, not a prompt request.
AI_QUOTE_ANALYSIS_MAX_WEB_SEARCH_CALLS = int(getattr(settings, 'AI_QUOTE_ANALYSIS_MAX_WEB_SEARCH_CALLS', 1))
DRIVER_DRIVING_HOURS_PER_DAY = float(getattr(settings, 'DRIVER_DRIVING_HOURS_PER_DAY', 9))

# MANUAL UPDATE REQUIRED when OpenAI changes pricing — never auto-fetched.
# Verified against OpenAI's published pricing 2026-09-24. $ per 1,000,000 tokens.
OPENAI_PRICING = {
    'gpt-4o-mini': {'input_per_1m': 0.15, 'cached_input_per_1m': 0.075, 'output_per_1m': 0.60},
    'gpt-4.1-mini': {'input_per_1m': 0.40, 'cached_input_per_1m': 0.10, 'output_per_1m': 1.60},
    'gpt-5.6-sol': {'input_per_1m': 4.00, 'cached_input_per_1m': 0.40, 'output_per_1m': 20.00},
    'gpt-5.6-terra': {'input_per_1m': 2.00, 'cached_input_per_1m': 0.20, 'output_per_1m': 12.00},
    'gpt-5.6-luna': {'input_per_1m': 0.20, 'cached_input_per_1m': 0.02, 'output_per_1m': 1.20},
}
# Web search: $10 / 1,000 calls for all models, plus search content tokens.
WEB_SEARCH_COST_PER_1000_CALLS = 10.00
# For these models OpenAI bills search content as "a fixed block of 8,000
# input tokens per call" instead of metered tokens.
FIXED_SEARCH_BLOCK_TOKENS = {'gpt-4o-mini': 8000, 'gpt-4.1-mini': 8000}
# Whether response.usage.input_tokens already includes that block. It does:
# the 2026-09-24 live run recorded 34,844 research input tokens for 4
# searches (~8,700 each) from prompts of ~200 tokens. Adding it again would
# double count.
SEARCH_BLOCK_INCLUDED_IN_USAGE = True

TOPICS = ('fuel', 'tolls', 'driver_allowance', 'base_rate')
# Only these are web-searched. Fuel comes from the app's own official FIASA
# record, and the base rate from the platform lane benchmark: the web has no
# trustworthy published R/km rates (a 2026 search found only blog tables,
# one of them withdrawn by its author as "not derived from any rate dataset").
RESEARCH_TOPICS = ('tolls', 'driver_allowance')
ITEM_LABELS = {'fuel': 'Fuel', 'tolls': 'Tolls', 'driver_allowance': 'Driver allowance', 'base_rate': 'Base rate'}
FIASA_URL = 'https://fuelsindustry.org.za/consumer-information/fuel-prices-current-past/'
FIASA_TITLE = 'Fuels Industry Association of SA: current fuel prices'
# resolve_market_rate sources that are real quotes (the SA estimate isn't).
BENCHMARK_SOURCES = {
    'platform': 'platform benchmark for this lane',
    'platform_lane': 'platform benchmark for this lane',
    'company': "your company's accepted quotes on this lane",
}
# The benchmark is one median, not a published range: a base rate within
# this share of the implied one counts as at market.
BASE_BAND = 0.10
# The two published per-day allowances the research may use, and how the UI
# names them — anything else it offers is a substitute and not used.
DRIVER_ALLOWANCE_TYPES = {
    'nbcrfli': 'NBCRFLI driver allowance',
    'sars_subsistence': 'SARS daily subsistence allowance (meals & incidentals)',
}

FUEL_TOLERANCE = 0.01            # fuel within 1% of the published price = at market
TOLL_TOLERANCE = 0.01            # route toll total within 1% = at market
FUEL_PRICE_BOUNDS = (10.0, 60.0)         # R per litre
PLAZA_TARIFF_MAX = 5000.0                # R per plaza, one way
DRIVER_RATE_MAX_PER_DAY = 5000.0         # R per day
# Recency: SA fuel changes on the first Wednesday of every month; SANRAL
# tariffs and the SARS / NBCRFLI allowances every 1 March.
# How close (chars) a figure's year must sit to the figure on its page.
DATE_NEAR_CHARS = 300
# A win probability is only shown when every price-dependent feature is
# within this many standard deviations of what the model was trained on.
WIN_FEATURE_Z_LIMIT = 2.0

UNAVAILABLE_MESSAGE = 'AI price verification is temporarily unavailable — please try again shortly.'


_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
           'September', 'October', 'November', 'December')


def _f(v, default=None):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _money(v):
    return round(float(v or 0.0), 2)


def _whole_rand(v):
    """Round half-up to whole rand — exactly what QuoteBuilder's Math.round
    does for its fuel and base-rate lines, so an applied AI price lands on
    the same total the panel showed."""
    return float(math.floor(float(v or 0.0) + 0.5))


def _long_date(d: date) -> str:
    return f'{d.day} {_MONTHS[d.month - 1]} {d.year}'


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(('gpt-5', 'o1', 'o3', 'o4'))


def _reasoning_kwargs(model: str) -> dict:
    """Only reasoning models accept `reasoning`; gpt-4o-mini rejects it."""
    if _is_reasoning_model(model):
        return {'reasoning': {'effort': AI_QUOTE_ANALYSIS_REASONING_EFFORT}}
    return {}


def _client():
    """Fresh client per call so a rotated key takes effect without a restart.
    No SDK retries: a retried call could double a call's time and blow the
    run deadline; a failed topic just comes back not verified.
    THIS is the test mock point: patch 'core.services.quote_ai_pricing._client'."""
    api_key = os.environ.get('OPENAI_API_KEY') or getattr(settings, 'OPENAI_API_KEY', '')
    return OpenAI(api_key=api_key, timeout=AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS, max_retries=0)


def _legs(payload: dict) -> int:
    legs = int(_f(payload.get('legs'), 1) or 1)
    return legs if legs >= 1 else 1


def _toll_class(vehicle_type, company=None):
    """The exact SANRAL class our own toll calculator uses for this vehicle,
    so the model never has to guess an axle class. With a company, this is
    main's resolve_toll_class (VehicleType.sanral_toll_class first, the same
    class the route calc priced); without one, the name-based guess."""
    try:
        from core.services.toll_calculator import resolve_toll_class, resolve_toll_class_from_name
        if company is not None:
            sanral_class = resolve_toll_class(vehicle_type or '', company).sanral_class
        else:
            sanral_class = resolve_toll_class_from_name(vehicle_type or '').sanral_class
    except Exception as exc:
        logger.warning('AI price analysis: toll class lookup failed: %s', exc)
        return None, None
    labels = {1: 'Class 1 (light vehicle)', 2: 'Class 2 (2-axle heavy vehicle)',
              3: 'Class 3 (3-4 axle heavy vehicle)', 4: 'Class 4 (5+ axle heavy vehicle / combination)'}
    return sanral_class, labels.get(sanral_class, f'Class {sanral_class}')


def _route_plazas(payload: dict) -> list:
    out = []
    for p in (payload.get('route') or {}).get('toll_breakdown') or []:
        if isinstance(p, dict) and p.get('plaza'):
            out.append({'plaza': str(p['plaza']), 'tariff_zar': _f(p.get('tariff'))})
    return out


def _toll_schedule_start(today: date) -> date:
    """SANRAL tariffs change every 1 March; this is the start of the schedule in force today."""
    march1 = date(today.year, 3, 1)
    return march1 if today >= march1 else date(today.year - 1, 3, 1)


def build_condensed_context(payload: dict, today: date = None, company=None) -> dict:
    """Token-lean context sent to OpenAI, built from an explicit include-list
    — anything not listed here is dropped even if present in `payload`. Never
    includes route['geometry'], route['sections'], coordinates, customer PII,
    or quote totals/margins (the model only extracts unit market values)."""
    today = today or timezone.localdate()
    route = payload.get('route') or {}
    legs = _legs(payload)
    toll_cost = _f(payload.get('toll_cost'))
    sanral_class, class_label = _toll_class(payload.get('vehicle_type'), company)
    return {
        'lane': {
            'origin': payload.get('origin'),
            'destination': payload.get('destination'),
            'trip_type': payload.get('trip_type') or ('ROUND_TRIP' if legs == 2 else 'ONE_WAY'),
            'legs': legs,
            'one_way_distance_km': _f(payload.get('one_way_distance_km')),
            'driving_minutes_one_way': _f(payload.get('duration_minutes')),
            'vehicle_type': payload.get('vehicle_type'),
            'weight_kg': _f(payload.get('weight')),
            'cross_border': bool(route.get('cross_border')),
            'country_codes': route.get('country_codes'),
        },
        'fuel': {
            'fuel_type': payload.get('fuel_type') or 'Diesel',
            'zone': payload.get('fuel_zone'),
            'operator_price_per_litre': _f(payload.get('fuel_price_used')),
        },
        'tolls': {
            'sanral_class': sanral_class,
            'sanral_class_label': class_label,
            'plazas_one_way': [p['plaza'] for p in _route_plazas(payload)],
            'operator_one_way_total_zar': round(toll_cost / legs, 2) if toll_cost is not None else None,
        },
        'today': today.isoformat(),
    }


RESEARCH_SYSTEM_PROMPT = """You are a South African road-freight pricing researcher. Use web \
search to find the figure asked for AS IT IS IN FORCE TODAY, from an official or authoritative \
source. Report each figure EXACTLY as printed on the source (same digits, same unit — do not \
round or convert), the source's name, and the date the figure took effect exactly as printed \
(day, month and year). Never fall back to an older figure or a different kind of figure: if you \
cannot find the one asked for, in force today, say NOT FOUND. Do not estimate, average, or \
calculate. Keep it short: a few lines."""

TOPIC_INSTRUCTIONS = {
    'tolls':("Today is {today}. Find the SANRAL toll tariffs for {sanral_class_label} in force today "
              "— the schedule that took effect on 1 March {tariff_year} — at these mainline toll "
              "plazas: {plazas}. List each plaza with its exact tariff for that class in Rand as "
              "printed, and the date the schedule took effect. Mainline plazas only, not ramp plazas; "
              "never an earlier year's schedule."),
    'driver_allowance': ("Today is {today}. Find the per-day allowance in force today for a South African "
                         "long-distance truck driver sleeping away from home. First choice: the NBCRFLI "
                         "(road freight bargaining council) main agreement night-out / subsistence "
                         "allowance. Only if that is not published: the SARS daily subsistence allowance "
                         "for meals and incidental costs (local travel) for the tax year that runs from "
                         "1 March {tariff_year} to 28 February {next_year} — SARS calls this the "
                         "\"{next_year}\" year of assessment, NOT {tariff_year}. Give the exact per-day amount "
                         "as printed, say which of the two it is, and the date the period STARTS "
                         "(e.g. 1 March {tariff_year})."),
}


def _topic_prompt(topic: str, context: dict) -> str:
    lane, fuel, tolls = context['lane'], context['fuel'], context['tolls']
    today = date.fromisoformat(context['today'])
    weight = lane.get('weight_kg')
    fields = {
        'today': _long_date(today),
        'month': _MONTHS[today.month - 1],
        'month_year': f'{_MONTHS[today.month - 1]} {today.year}',
        'tariff_year': _toll_schedule_start(today).year,
        'next_year': _toll_schedule_start(today).year + 1,
        'fuel_type': fuel.get('fuel_type') or 'Diesel',
        'zone': (fuel.get('zone') or 'inland').lower(),
        'sanral_class_label': tolls.get('sanral_class_label') or 'the applicable heavy-vehicle class',
        'plazas': ', '.join(tolls.get('plazas_one_way') or []) or 'the plazas on this route',
        'vehicle_type': lane.get('vehicle_type') or 'heavy truck',
        'weight_t': round(weight / 1000, 1) if weight else 'unknown',
        'origin': lane.get('origin') or 'origin',
        'destination': lane.get('destination') or 'destination',
    }
    return TOPIC_INSTRUCTIONS[topic].format(**fields)


def _run_topic_research(client, topic: str, context: dict, timeout: float):
    return client.responses.create(
        model=AI_QUOTE_ANALYSIS_MODEL,
        input=[
            {'role': 'system', 'content': RESEARCH_SYSTEM_PROMPT},
            {'role': 'user', 'content': _topic_prompt(topic, context)},
        ],
        tools=[{'type': 'web_search', 'search_context_size': AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE}],
        # Must search — an answer from the model's memory is an assumption.
        tool_choice='required',
        max_tool_calls=AI_QUOTE_ANALYSIS_MAX_WEB_SEARCH_CALLS,
        timeout=timeout,
        **_reasoning_kwargs(AI_QUOTE_ANALYSIS_MODEL),
    )


STRUCTURING_SYSTEM_PROMPT = """You get research notes for two topics (tolls, driver_allowance) \
and a numbered list of the sources the research used (ids S1, S2, ...). EXTRACT the figures — do \
not judge, estimate, calculate, or add anything.

- tolls.plazas: every plaza the research gives a published tariff for, with its name exactly as \
the research writes it (keep words like "Ramp") and the tariff in Rand.
- driver_allowance.rate_per_day_zar: the published per-day amount in Rand. allowance_type: \
"nbcrfli" for the road-freight bargaining council allowance, "sars_subsistence" for the SARS \
daily subsistence allowance, "other" for anything else, "none" if no figure.
- effective_date: the date the figure's period STARTS as the research gives it, as YYYY-MM-DD \
(or YYYY-MM / YYYY if only that is given). A SARS "year of assessment" or "tax year" named by a \
year Y is NOT a date: give the start date the research states (1 March of Y-1), or null. Null if \
the research gives no date.
- sources: the ids of the sources the research attributes THAT figure to.
- If the research says NOT FOUND or gives no figure, use null (or an empty plazas list)."""


def build_structuring_schema(source_ids: list) -> dict:
    """Strict json_schema, built per request so every `sources` entry can
    only be one of THIS run's real citation ids."""
    def sources():
        return {'type': 'array', 'items': {'type': 'string', 'enum': list(source_ids)}}

    def obj(props):
        return {'type': 'object', 'additionalProperties': False, 'properties': props, 'required': list(props)}

    number = {'type': ['number', 'null']}
    text_date = {'type': ['string', 'null']}
    breakdown = {
        'tolls': obj({'plazas': {'type': 'array', 'items': obj({
            'plaza': {'type': 'string'}, 'tariff_zar': {'type': 'number'},
            'effective_date': dict(text_date), 'sources': sources(),
        })}}),
        'driver_allowance': obj({
            'rate_per_day_zar': dict(number),
            'allowance_type': {'type': 'string', 'enum': [*DRIVER_ALLOWANCE_TYPES, 'other', 'none']},
            'effective_date': dict(text_date),
            'sources': sources(),
        }),
    }
    return {'type': 'json_schema', 'name': 'quote_price_extraction', 'strict': True, 'schema': obj(breakdown)}


def _run_structuring_call(client, research_by_topic: dict, numbered_citations: list, timeout: float):
    extra = {} if _is_reasoning_model(AI_QUOTE_ANALYSIS_STRUCTURING_MODEL) else {'temperature': 0}
    return client.responses.create(
        model=AI_QUOTE_ANALYSIS_STRUCTURING_MODEL,
        input=[
            {'role': 'system', 'content': STRUCTURING_SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps({
                'research_notes': research_by_topic,
                'available_sources': numbered_citations,
            })},
        ],
        text={'format': build_structuring_schema([c['id'] for c in numbered_citations])},
        timeout=timeout,
        **extra,
        **_reasoning_kwargs(AI_QUOTE_ANALYSIS_STRUCTURING_MODEL),
    )


def _extract_citations(response) -> list:
    """Real source links from a web_search-grounded response — never
    invented. De-duplicated by URL."""
    citations, seen = [], set()
    for item in getattr(response, 'output', None) or []:
        if getattr(item, 'type', None) != 'message':
            continue
        for content in getattr(item, 'content', None) or []:
            for ann in getattr(content, 'annotations', None) or []:
                url = getattr(ann, 'url', None)
                if getattr(ann, 'type', None) == 'url_citation' and url and url not in seen:
                    seen.add(url)
                    citations.append({'title': getattr(ann, 'title', None) or url, 'url': url})
    return citations


def _count_web_search_calls(response) -> int:
    return sum(1 for item in (getattr(response, 'output', None) or [])
               if getattr(item, 'type', None) == 'web_search_call')


def _map_sources(ids, sources_by_id: dict) -> list:
    out, seen = [], set()
    for sid in ids or []:
        src = sources_by_id.get(sid)
        if src and src['url'] not in seen:
            seen.add(src['url'])
            out.append({'title': src['title'], 'url': src['url']})
    return out


# Plaza names: our DB holds short mainline names ("Mooi", "Vaal"); the
# published lists also carry ramp plazas with their own, lower tariffs.
_PLAZA_STOP_WORDS = {'toll', 'plaza', 'mainline', 'main', 'line'}
_ROUTE_CODE_RE = re.compile(r'^[nr]\d+[a-z]?$')
# Extra words that make a published name a DIFFERENT plaza than ours
# ("Tugela East", "Grasmere Ramp"), as opposed to a longer spelling of the
# same one ("Mooi River").
_OTHER_PLAZA_WORDS = {'ramp', 'east', 'west', 'north', 'south', 'northbound', 'southbound',
                      'eastbound', 'westbound', 'on', 'off'}


def _plaza_tokens(name: str) -> frozenset:
    text = ''.join(ch if ch.isalnum() else ' ' for ch in (name or '').lower())
    return frozenset(w for w in text.split() if w not in _PLAZA_STOP_WORDS and not _ROUTE_CODE_RE.match(w))


def _plaza_candidates(ours: str, found: list) -> list:
    """Indexes of the published entries that are OUR plaza: an exact name
    match if there is one, otherwise whole-word supersets that add nothing
    that names a different (ramp/direction) plaza."""
    mine = _plaza_tokens(ours)
    if not mine:
        return []
    exact = [i for i, fp in enumerate(found) if _plaza_tokens(fp.get('plaza', '')) == mine]
    if exact:
        return exact
    return [i for i, fp in enumerate(found)
            if mine < _plaza_tokens(fp.get('plaza', ''))
            and not (_plaza_tokens(fp.get('plaza', '')) - mine) & _OTHER_PLAZA_WORDS]


def _fmt_rand(v):
    return f'R{v:,.2f}'


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------

def _parse_effective(value):
    """'2026-03-01' / '2026-03' / '2026' -> (earliest, latest) day it could
    mean, or None. Equal for a full date."""
    m = re.fullmatch(r'\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?\s*', str(value or ''))
    if not m:
        return None
    y = int(m[1])
    try:
        if m[3]:
            day = date(y, int(m[2]), int(m[3]))
            return day, day
        if m[2]:
            start = date(y, int(m[2]), 1)
            return start, (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return date(y, 1, 1), date(y, 12, 31)
    except ValueError:
        return None


def _freshness(raw_date, today: date):
    """Toll tariffs and the SARS / NBCRFLI allowances all run from 1 March.
    A figure counts only with an exact start date that is on or after the
    most recent 1 March and not in the future.
    (ok, note, clause, effective_date); `clause` finishes the sentence
    "…was found, but <clause>."."""
    parsed = _parse_effective(raw_date)
    if parsed is None:
        return False, 'no effective date given', 'the source gives no date for it', None
    earliest, latest = parsed
    if earliest != latest:
        return False, 'no exact effective date', f'the source only dates it "{raw_date}"', None
    eff = earliest
    if eff > today:
        return False, 'not in force yet', f'it only takes effect on {_long_date(eff)}', eff
    start = _toll_schedule_start(today)
    if eff < start:
        return (False, 'out of date', f'it is from {_long_date(eff)}, before the current period '
                f'(from {_long_date(start)})', eff)
    return True, None, None, eff


def _schedule_date_forms(eff: date) -> tuple:
    """How a 1-March-style start date is commonly printed ("1 March 2026",
    "01/03/2026", Afrikaans "Maart"). A bare year is never enough."""
    y, m, d = eff.year, eff.month, eff.day
    month = _MONTHS[m - 1]
    return (f'{month} {y}', f'{d} {month[:3]} {y}', f'{y}-{m:02d}-{d:02d}', f'{d:02d}/{m:02d}/{y}',
            f'{d}/{m}/{y}', f'{d:02d}.{m:02d}.{y}') + ((f'Maart {y}',) if m == 3 else ())


def _first_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7)


def _fuel_period(today: date):
    """(month record date, effective date) of the fuel price in force today:
    SA fuel prices change on the first Wednesday of each month."""
    change = _first_wednesday(today.year, today.month)
    if today >= change:
        return today.replace(day=1), change
    prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    return prev, _first_wednesday(prev.year, prev.month)


# Stored FuelPrice rows the fuel check trusts, in main's trust order: an ops
# MANUAL override beats the FIASA feed (core.services.fuel_price._SOURCE_TRUST).
# Scraper fallbacks (AA/SAPIA/DMRE) and the FALLBACK table are not official.
OFFICIAL_FUEL_SOURCES = ('MANUAL', 'FIASA')
# The latest official price is still shown (labelled as the latest, with its
# date) up to this long after it took effect; older than that it isn't used.
FUEL_MAX_AGE_DAYS = 62


def _fuel_effective_date(rec):
    """The day a stored price took effect, or None if unknown. FIASA rows
    carry main's effective_from (the dated column the price came from); a
    MANUAL row without one counts from the 1st of the month it is filed
    under."""
    if rec.effective_from is not None:
        return timezone.localtime(rec.effective_from).date()
    return rec.date if rec.source == 'MANUAL' else None


def official_fuel_price(fuel_type, zone, today: date) -> dict:
    """The official diesel price in force today, read ONLY from the app's
    stored FuelPrice rows. Never scrapes: refreshing is the refresh_fuel_price
    beat task's job, and a live scrape here could hold the request for ~23 s.

    Uses the newest MANUAL or FIASA row (FIASA only for the 50ppm grade, as
    QuoteBuilder shows) whose effective_from is on or before today. `current`
    is True when that is the adjustment in force today (SA fuel changes on the
    first Wednesday of the month); otherwise it is the latest one on record
    and the UI says so. {'price_per_litre', 'other_zone_price', 'zone',
    'effective_date', 'current', 'source', 'error'}. Never raises."""
    out = {'price_per_litre': None, 'other_zone_price': None, 'zone': None, 'effective_date': None,
           'current': None, 'source': None, 'error': None}
    if (fuel_type or 'Diesel').strip().lower() != 'diesel':
        out['error'] = f'only diesel has an official monthly price ({fuel_type})'
        return out
    _, change = _fuel_period(today)
    try:
        from core.models.fuel_price import FuelPrice

        rec = eff = None
        rows = (FuelPrice.objects.filter(source__in=OFFICIAL_FUEL_SOURCES, date__lte=today.replace(day=1))
                .order_by('-date')[:4])
        for row in rows:
            if row.source == 'FIASA' and row.diesel_grade != '50ppm':
                continue
            row_eff = _fuel_effective_date(row)
            if row_eff is not None and row_eff <= today:
                rec, eff = row, row_eff
                break
        if rec is None:
            out['error'] = 'no official price on record'
            return out
        if (today - eff).days > FUEL_MAX_AGE_DAYS:
            out['error'] = f'the latest official price on record is from {_long_date(eff)}'
            return out
    except Exception as exc:
        logger.warning('AI price analysis: official fuel price lookup failed: %s', exc)
        out['error'] = 'official fuel price unavailable'
        return out
    coastal = (zone or '').upper() == 'COASTAL'
    out.update({
        'price_per_litre': float(rec.diesel_coastal if coastal else rec.diesel_inland),
        'other_zone_price': float(rec.diesel_inland if coastal else rec.diesel_coastal),
        'zone': 'coastal' if coastal else 'inland',
        'effective_date': eff.isoformat(),
        # A MANUAL row without effective_from is the override for its month.
        'current': eff >= change or (rec.effective_from is None and rec.date == change.replace(day=1)),
        'source': rec.source,
    })
    return out


def lane_benchmark(payload: dict, company) -> dict:
    """The lane's market total from real quotes (core.services.lane_benchmark),
    the same definition the win model is trained on. Never raises."""
    try:
        from core.services.lane_benchmark import resolve_market_rate
        rate, source = resolve_market_rate(payload.get('origin'), payload.get('destination'),
                                           payload.get('vehicle_type'), company=company)
    except Exception as exc:
        logger.warning('AI price analysis: lane benchmark failed: %s', exc)
        rate, source = None, 'none'
    return {'rate': _f(rate), 'source': source}


# ---------------------------------------------------------------------------
# Deterministic verdicts — each returns an item dict for the response.
# ---------------------------------------------------------------------------

# What a verdict rests on, for the frontend's badge (`verification_kind`):
#   official   fuel, from the stored official monthly price (FIASA / ops override)
#   benchmark  base rate, from the lane benchmark of real quotes (not the web)
#   source     tolls / driver allowance, found on the page the search cited
#   unverified nothing to rest a verdict on (verdict is could_not_verify)
VERIFICATION_KINDS = ('official', 'benchmark', 'source', 'unverified')


def _item(verdict, current, market, reason, note, sources, detail, kind='unverified'):
    toggleable = verdict == 'needs_adjustment'
    return {
        'verification_kind': kind if verdict != 'could_not_verify' else 'unverified',
        'verdict': verdict,
        'toggleable': toggleable,
        'current_value_zar': _money(current),
        'ai_value_zar': _money(market if toggleable else current),
        'reason': reason,
        'verification': 'verified' if verdict != 'could_not_verify' else 'not_verified',
        'verification_note': note,
        'sources': sources,
        'detail': detail,
    }


def _verify_urls(sources):
    return [s['url'] for s in sources]


def _fuel_item(official, payload):
    """Fuel against the official price in force today (official_fuel_price)."""
    official = official or {}
    litres = _f(payload.get('fuel_usage_litres'), 0.0) or 0.0
    yours_total = _f(payload.get('fuel_cost'), 0.0) or 0.0
    yours_rate = _f(payload.get('fuel_price_used'))
    market_rate = _f(official.get('price_per_litre'))
    sources = [{'title': FIASA_TITLE, 'url': FIASA_URL}]
    detail = {'litres': round(litres, 2), 'your_price_per_litre': yours_rate, 'market_price_per_litre': None,
              'effective_date': official.get('effective_date'), 'zone': official.get('zone'),
              'other_zone_price_per_litre': official.get('other_zone_price'),
              'source': official.get('source') or 'FIASA', 'current': official.get('current')}

    def not_verified(reason, note):
        return _item('could_not_verify', yours_total, None, reason, note, [], detail)

    if market_rate is None:
        return not_verified(f'No official fuel price to check against: {official.get("error") or "not recorded"}.',
                            'no official price')
    if not (FUEL_PRICE_BOUNDS[0] <= market_rate <= FUEL_PRICE_BOUNDS[1]):
        return not_verified('The recorded official fuel price failed a sanity check.', 'failed sanity check')
    detail['market_price_per_litre'] = market_rate
    if litres <= 0 or not yours_rate:
        return not_verified('Fuel litres or your price per litre are missing.', 'missing inputs')
    eff = date.fromisoformat(official['effective_date'])
    other = official.get('other_zone_price')
    other_zone = 'coastal' if official.get('zone') == 'inland' else 'inland'
    current = official.get('current') is not False
    # A price that isn't the adjustment in force today is still the best
    # official figure on record, and is labelled as exactly that.
    published = ((f'the official {official.get("zone")} diesel price {_fmt_rand(market_rate)}/L (from '
                  if current else
                  f'the latest official {official.get("zone")} diesel price {_fmt_rand(market_rate)}/L (effective ')
                 + _long_date(eff) + (f'; {other_zone} {_fmt_rand(other)}/L)' if other else ')'))
    if official.get('source') == 'MANUAL':
        note = 'official monthly price (entered by TruckWys)' if current else 'latest official price on record'
    else:
        note = 'official monthly price (FIASA)' if current else 'latest official price on record (FIASA)'
    if abs(yours_rate - market_rate) <= FUEL_TOLERANCE * market_rate:
        return _item('accurate', yours_total, None, f'Your {_fmt_rand(yours_rate)}/L matches {published}.',
                     note, sources, detail, 'official')
    return _item('needs_adjustment', yours_total, _whole_rand(litres * market_rate),
                 f'Your {_fmt_rand(yours_rate)}/L vs {published}.', note, sources, detail, 'official')


def _excl_vat(amount_incl_vat) -> float:
    """SANRAL publishes tariffs INCLUDING VAT; quotes price tolls EXCLUDING
    VAT (the invoice adds 15% once). main's toll_calculator.tariff_excl_vat,
    so both sides round the same way."""
    from core.services.toll_calculator import tariff_excl_vat
    return float(tariff_excl_vat(amount_incl_vat))


def _tolls_item(raw, payload, sources_by_id, pages, today, company=None):
    """Tolls against the published SANRAL tariffs. The published figure is
    VAT inclusive: it is matched on the source page AS PRINTED, then
    converted to excl. VAT (like main's route calc) before it is compared
    with the operator's toll or used as a market value."""
    legs = _legs(payload)
    yours_total = _f(payload.get('toll_cost'), 0.0) or 0.0
    yours_one_way = yours_total / legs
    route_plazas = _route_plazas(payload)
    _, class_label = _toll_class(payload.get('vehicle_type'), company)
    found = [p for p in ((raw or {}).get('plazas') or []) if isinstance(p, dict)]

    rows, matched, all_sources = [], set(), []
    for rp in route_plazas:
        # your_tariff_zar and market_tariff_zar are both excl. VAT;
        # published_tariff_incl_vat_zar is the figure as printed.
        row = {'plaza': rp['plaza'], 'your_tariff_zar': rp['tariff_zar'], 'market_tariff_zar': None,
               'published_tariff_incl_vat_zar': None, 'matches_yours': None,
               'verified': False, 'note': 'not found in published tariffs'}
        candidates = _plaza_candidates(rp['plaza'], found)
        matched.update(candidates)
        tariffs = {_f(found[i].get('tariff_zar')) for i in candidates}
        if len(tariffs) > 1:
            row['note'] = 'more than one published plaza matches this name'
        elif candidates:
            fp = found[candidates[0]]
            tariff = _f(fp.get('tariff_zar'))
            srcs = _map_sources(fp.get('sources'), sources_by_id)
            all_sources.extend(s for s in srcs if s not in all_sources)
            fresh, fresh_note, _, eff = _freshness(fp.get('effective_date'), today)
            if tariff is None or not (0 < tariff <= PLAZA_TARIFF_MAX):
                row['note'] = 'failed sanity check'
            elif not fresh:
                row.update({'market_tariff_zar': _excl_vat(tariff), 'published_tariff_incl_vat_zar': tariff,
                            'note': f'{fresh_note}' + (f' (from {_long_date(eff)})' if eff else '')})
            else:
                # Matched on the page as printed (VAT inclusive).
                ok, note = source_verification.check_figure(tariff, _verify_urls(srcs), pages,
                                                            near_any=_schedule_date_forms(eff))
                market = _excl_vat(tariff)
                row.update({'market_tariff_zar': market, 'published_tariff_incl_vat_zar': tariff,
                            'verified': ok, 'note': note})
                if rp['tariff_zar'] is not None:
                    row['matches_yours'] = abs(rp['tariff_zar'] - market) <= TOLL_TOLERANCE * market
        rows.append(row)
    extra = [fp.get('plaza') for i, fp in enumerate(found) if i not in matched and fp.get('plaza')]
    detail = {'legs': legs, 'toll_class': class_label, 'plazas': rows, 'other_plazas_mentioned': extra,
              'your_one_way_zar': round(yours_one_way, 2), 'market_one_way_zar': None,
              'vat_basis': 'excl_vat', 'schedule_from': _toll_schedule_start(today).isoformat()}

    if not route_plazas:
        return _item('could_not_verify', yours_total, None, 'This route has no toll plazas to check.',
                     'no plazas on route', all_sources, detail)
    unverified = [r['plaza'] for r in rows if not r['verified']]
    if unverified:
        return _item('could_not_verify', yours_total, None,
                     f'{len(rows) - len(unverified)} of {len(rows)} plazas confirmed on the current published tariff '
                     f'({", ".join(unverified)} not confirmed).',
                     'not every plaza confirmed', all_sources, detail)
    market_one_way = round(sum(r['market_tariff_zar'] for r in rows), 2)
    detail['market_one_way_zar'] = market_one_way
    if abs(yours_one_way - market_one_way) <= TOLL_TOLERANCE * market_one_way:
        return _item('accurate', yours_total, None,
                     f'All {len(rows)} plazas match the published {class_label} tariffs (excl. VAT).',
                     'checked on source page', all_sources, detail, 'source')
    return _item('needs_adjustment', yours_total, market_one_way * legs,
                 f'Published {class_label} tariffs total {_fmt_rand(market_one_way)} excl. VAT one way '
                 f'vs your {_fmt_rand(yours_one_way)}.',
                 'checked on source page', all_sources, detail, 'source')


def _nights_away(driving_hours):
    """Nights the driver sleeps away from home: driving days - 1, where
    driving days = ceil(driving hours / DRIVER_DRIVING_HOURS_PER_DAY). The
    NBCRFLI night-out allowance is paid per night slept away, and the SARS
    subsistence allowance needs at least one night away, so a trip that fits
    in one driving day (the driver is home that night) gets none. None if
    the driving time is unknown."""
    if not driving_hours:
        return None, None
    days = math.ceil(driving_hours / DRIVER_DRIVING_HOURS_PER_DAY)
    return days, max(days - 1, 0)


def _driver_item(raw, payload, sources_by_id, pages, today):
    raw = raw or {}
    legs = _legs(payload)
    yours_total = _f(payload.get('driver_cost'), 0.0) or 0.0
    minutes = _f(payload.get('duration_minutes'))
    sources = _map_sources(raw.get('sources'), sources_by_id)
    rate = _f(raw.get('rate_per_day_zar'))
    kind = raw.get('allowance_type')
    driving_hours = (minutes * legs / 60.0) if minutes else None
    days, nights = _nights_away(driving_hours)
    # `days` = driving days; the allowance is per NIGHT away (`nights`).
    detail = {'rate_per_day_zar': None, 'allowance_type': None, 'allowance_label': None, 'days': days,
              'nights': nights, 'allowance_basis': 'per_night_away',
              'driving_hours': round(driving_hours, 1) if driving_hours else None,
              'hours_per_day': DRIVER_DRIVING_HOURS_PER_DAY, 'market_total_zar': None, 'effective_date': None}

    def not_verified(reason, note, srcs=sources):
        return _item('could_not_verify', yours_total, None, reason, note, srcs, detail)

    if rate is None:
        return not_verified('No published per-day allowance was found.', source_verification.REASON_NOT_FOUND)
    if kind not in DRIVER_ALLOWANCE_TYPES:
        return not_verified(f'The research offered {_fmt_rand(rate)}/day, but not as an NBCRFLI or SARS '
                            'per-day allowance, so it is not used.', 'not a recognised driver allowance')
    label = DRIVER_ALLOWANCE_TYPES[kind]
    if not (0 < rate <= DRIVER_RATE_MAX_PER_DAY):
        return not_verified('The extracted allowance failed a sanity check.', 'failed sanity check', [])
    fresh, fresh_note, clause, eff = _freshness(raw.get('effective_date'), today)
    if not fresh:
        return not_verified(f'A {label} of {_fmt_rand(rate)}/day was found, but {clause}.', fresh_note)
    if kind == 'sars_subsistence':
        # SARS tables label each row by the year the tax year ENDS
        # ("2027  R595" is 1 Mar 2026 - 28 Feb 2027), so the figure must sit
        # in that row — not in last year's row next to it.
        ok, note = source_verification.check_figure(rate, _verify_urls(sources), pages,
                                                    preceding_year=eff.year + 1)
    else:
        ok, note = source_verification.check_figure(rate, _verify_urls(sources), pages,
                                                    near_any=_schedule_date_forms(eff), near_chars=DATE_NEAR_CHARS)
    if not ok:
        return not_verified(f'A {label} of {_fmt_rand(rate)}/day was reported but could not be confirmed on its source.',
                            note)
    detail.update({'rate_per_day_zar': rate, 'allowance_type': kind, 'allowance_label': label,
                   'effective_date': eff.isoformat()})
    if nights is None:
        return not_verified('Trip driving time is missing, so nights away can’t be worked out.', note)
    market_total = round(rate * nights, 2)
    detail['market_total_zar'] = market_total
    if nights == 0:
        basis = (f'no night away (about {driving_hours:.1f} driving hours fits in one '
                 f'{DRIVER_DRIVING_HOURS_PER_DAY:g}-hour driving day)')
        return _item('accurate', yours_total, None, f'The published {label} does not apply: {basis}.',
                     note, sources, detail, 'source')
    basis = (f'{_fmt_rand(rate)}/night × {nights} night{"s" if nights != 1 else ""} away '
             f'({days} driving days at about {DRIVER_DRIVING_HOURS_PER_DAY:g} h/day)')
    if yours_total >= market_total:
        return _item('accurate', yours_total, None, f'Your allowance covers the published {label}: {basis}.',
                     note, sources, detail, 'source')
    return _item('needs_adjustment', yours_total, market_total,
                 f'Published {label}: {basis} = {_fmt_rand(market_total)} vs your {_fmt_rand(yours_total)}.',
                 note, sources, detail, 'source')


def _base_rate_item(benchmark, payload, pass_through_market):
    """Base rate against the lane benchmark from real quotes: the market total
    minus the market pass-through, per km, gives the implied base rate; a
    band of BASE_BAND around it counts as at market."""
    benchmark = benchmark or {}
    distance = _f(payload.get('distance_km'), 0.0) or 0.0
    yours_rate = _f(payload.get('base_rate_per_km'), 0.0) or 0.0
    yours_total = _whole_rand(yours_rate * distance)
    rate, source = _f(benchmark.get('rate')), benchmark.get('source')
    # Your rate stays unrounded: "Use my price" writes it back into the R/km
    # box, and a rounded copy would shift the base line by a few rand.
    detail = {'distance_km': round(distance, 1), 'your_rate_per_km': yours_rate, 'ai_rate_per_km': yours_rate,
              'market_low_per_km': None, 'market_high_per_km': None, 'implied_rate_per_km': None,
              'benchmark_zar': rate, 'benchmark_source': source,
              'benchmark_label': BENCHMARK_SOURCES.get(source)}

    def not_verified(reason, note):
        return _item('could_not_verify', yours_total, None, reason, note, [], detail)

    if source not in BENCHMARK_SOURCES or not rate:
        return not_verified('There is no benchmark from real quotes for this lane yet.', 'no benchmark for this lane')
    if _legs(payload) != 1:
        return not_verified('The lane benchmark is for one-way loads, so it isn’t compared with a round trip.',
                            'benchmark is one-way')
    if distance <= 0:
        return not_verified('Distance is missing, so the benchmark can’t be turned into a rate per km.', 'missing distance')
    implied = (rate - pass_through_market) / distance
    if implied <= 0:
        return not_verified(f'The lane benchmark ({_fmt_rand(rate)}) is below this trip’s fuel, tolls and driver, '
                            'so it says nothing about the base rate.', 'benchmark below pass-through')
    # Cents, like the R/km box: the verdict, the AI line and what Apply
    # writes all use the same rate, so the price equals the applied total.
    low, high = round(implied * (1 - BASE_BAND), 2), round(implied * (1 + BASE_BAND), 2)
    detail.update({'market_low_per_km': low, 'market_high_per_km': high, 'implied_rate_per_km': round(implied, 2)})
    basis = (f'The {BENCHMARK_SOURCES[source]} is {_fmt_rand(rate)}; after market fuel, tolls and driver that '
             f'leaves {_fmt_rand(implied)}/km (band {_fmt_rand(low)} to {_fmt_rand(high)}/km)')
    note = 'lane benchmark from real quotes'
    if low <= round(yours_rate, 2) <= high:
        return _item('accurate', yours_total, None, f'{basis}; your {_fmt_rand(yours_rate)}/km is inside it.',
                     note, [], detail, 'benchmark')
    target = low if yours_rate < low else high
    detail['ai_rate_per_km'] = target
    direction = 'below' if yours_rate < low else 'above'
    return _item('needs_adjustment', yours_total, _whole_rand(target * distance),
                 f'{basis}; your {_fmt_rand(yours_rate)}/km is {direction} it.', note, [], detail, 'benchmark')


def _return_leg(items: dict, payload: dict):
    """One-way trips: what the empty run home would cost at market figures,
    shown so the base rate can be judged against it. Never priced in."""
    if _legs(payload) != 1:
        return None
    fuel, tolls, driver = (items[t]['detail'] for t in ('fuel', 'tolls', 'driver_allowance'))
    litres = fuel.get('litres') or 0.0
    per_litre = fuel.get('market_price_per_litre') or _f(payload.get('fuel_price_used'), 0.0) or 0.0
    fuel_zar = _whole_rand(litres * per_litre)
    tolls_zar = _money(items['tolls']['ai_value_zar'])
    # The empty run home adds only the extra nights away that a round trip
    # has over the one-way trip (a short run home is the same day).
    rate = driver.get('rate_per_day_zar')
    one_way_hours = driver.get('driving_hours')
    if rate and one_way_hours:
        extra_nights = _nights_away(one_way_hours * 2)[1] - _nights_away(one_way_hours)[1]
        driver_zar = _money(rate * extra_nights)
    else:
        driver_zar = _money(items['driver_allowance']['ai_value_zar'])
    return {'fuel_zar': fuel_zar, 'tolls_zar': tolls_zar, 'driver_zar': driver_zar,
            'total_zar': _money(fuel_zar + tolls_zar + driver_zar),
            'fuel_basis': 'official' if fuel.get('market_price_per_litre') else 'yours'}


# ---------------------------------------------------------------------------
# Pricing combinations + win probability
# ---------------------------------------------------------------------------

def choice_key(choices: dict) -> str:
    return '|'.join(f'{t}={choices.get(t, "mine")}' for t in TOPICS)


def _combinations(items: dict, payload: dict) -> dict:
    cross_border = _f(payload.get('cross_border_cost'), 0.0) or 0.0
    toggleable = [t for t in TOPICS if items[t]['toggleable']]
    combos = {}
    for picks in itertools.product(('ai', 'mine'), repeat=len(toggleable)):
        choices = {t: 'mine' for t in TOPICS}
        choices.update(dict(zip(toggleable, picks)))
        values = {t: items[t]['ai_value_zar'] if choices[t] == 'ai' else items[t]['current_value_zar'] for t in TOPICS}
        base_detail = items['base_rate']['detail']
        rate = base_detail['ai_rate_per_km'] if choices['base_rate'] == 'ai' else base_detail['your_rate_per_km']
        pass_through = values['fuel'] + values['tolls'] + values['driver_allowance'] + cross_border
        price = pass_through + values['base_rate']
        combos[choice_key(choices)] = {
            'choices': choices,
            'values': {k: _money(v) for k, v in values.items()},
            'base_rate_per_km': round(rate, 2),
            'pass_through_zar': _money(pass_through),
            'price_zar': _money(price),
            'margin_zar': _money(values['base_rate']),
            'margin_pct': round(values['base_rate'] / price * 100.0, 1) if price > 0 else 0.0,
            'win_probability': None,
        }
    return combos


def _parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _training_z_scores(predict_proba, features: dict) -> dict:
    """How far this quote's price-dependent features sit from the model's
    training data, in standard deviations — read from the fitted pipeline's
    StandardScaler. {} if the model can't be introspected."""
    model_obj = getattr(predict_proba, '__self__', None)
    pipeline = getattr(model_obj, 'model', None)
    names = list((getattr(model_obj, 'metadata', None) or {}).get('feature_names') or [])
    try:
        scaler = pipeline[0]
        mean, scale = scaler.mean_, scaler.scale_
    except Exception:
        return {}
    from core.services import quote_features
    vec = quote_features.vectorize(features, names)
    out = {}
    for name in ('price_ratio', 'cost_to_market_ratio', 'quoted_margin_pct'):
        if name in names:
            i = names.index(name)
            if scale[i]:
                out[name] = float((vec[i] - mean[i]) / scale[i])
    return out


def _attach_win_probabilities(combos: dict, default_key: str, payload: dict, user, company) -> dict:
    """Scores every combination with the EXISTING win-probability model
    (core.services.win_prediction). Only a real trained model counts — the
    heuristic fallback is never shown as a probability — and only when this
    quote looks like what it was trained on. Never raises."""
    def unavailable(reason, **extra):
        for combo in combos.values():
            combo['win_probability'] = None
        return {'available': False, 'scope': extra.get('scope'), 'training_samples': extra.get('samples', 0),
                'reason': reason}

    try:
        from core.services.win_prediction import resolve_prediction_context
        ctx = resolve_prediction_context(user, company)
    except Exception as exc:
        logger.warning('AI price analysis: win model resolve failed: %s', exc)
        return unavailable('not_enough_history')
    if not ctx.available or company is None or user is None:
        return unavailable('not_enough_history')
    model_info = {'scope': ctx.scope, 'samples': ctx.sample_count}
    try:
        from core.services import quote_features
        from core.services.lane_benchmark import resolve_market_rate

        # The market rate definition the model was TRAINED on (platform
        # median / company tier), not the benchmark mean the page displays.
        market_rate, _ = resolve_market_rate(payload.get('origin'), payload.get('destination'),
                                             payload.get('vehicle_type'), company=company)
        market_rate = _f(market_rate) or None
        if market_rate is None:
            # Without a market rate price_ratio is a filler, so the probability
            # wouldn't depend on the price at all.
            return unavailable('no_market_rate', **model_info)
        default = combos[default_key]
        v = default['values']
        base = quote_features.compute_features(
            company=company, customer_id=payload.get('customer_id') or None,
            created_by_user_id=getattr(user, 'id', None),
            origin=payload.get('origin'), destination=payload.get('destination'),
            vehicle_type=payload.get('vehicle_type'),
            total_amount=default['price_zar'], base_rate=v['base_rate'], fuel_surcharge=v['fuel'],
            toll_charges=v['tolls'], driver_allowance=v['driver_allowance'],
            additional_charges=_f(payload.get('cross_border_cost'), 0.0) or 0.0,
            weight_kg=_f(payload.get('weight')), is_round_trip=_legs(payload) == 2,
            distance_km=_f(payload.get('one_way_distance_km')) or _f(payload.get('distance_km')),
            pickup_date=_parse_date(payload.get('pickup_date')),
            market_rate=market_rate,
        )
        scored = {}
        for key, combo in combos.items():
            price = combo['price_zar']
            features = dict(base)
            # Only the price-dependent features change between combinations.
            # The price is the sum of its cost lines, so direct cost == price
            # and the quoted margin is 0 by compute_features' own definition.
            features['price_ratio'] = price / market_rate
            features['cost_to_market_ratio'] = price / market_rate
            features['quoted_margin_pct'] = 0.0
            z = _training_z_scores(ctx.predict_proba, features)
            if any(abs(val) > WIN_FEATURE_Z_LIMIT for val in z.values()):
                return unavailable('outside_training_range', **model_info)
            scored[key] = round(float(ctx.predict_proba(features)), 3)
    except Exception as exc:
        logger.warning('AI price analysis: win probability failed: %s', exc)
        return unavailable('prediction_failed', **model_info)
    for key, combo in combos.items():
        combo['win_probability'] = scored[key]
    return {'available': True, 'scope': ctx.scope, 'training_samples': ctx.sample_count, 'reason': None}


def compute_pricing(extracted, payload: dict, sources_by_id: dict, pages: dict, today: date = None, *,
                    official_fuel: dict = None, benchmark: dict = None, company=None) -> dict:
    """All verdicts and price arithmetic. `extracted` is the structuring
    call's JSON for tolls and driver (or None when there was nothing
    citable); `pages` is {url: {'text', 'error'}} from source_verification.
    Fuel comes from `official_fuel` (official_fuel_price) and the base rate
    from `benchmark` (lane_benchmark); both are looked up when not given."""
    today = today or timezone.localdate()
    extracted = extracted or {}
    if official_fuel is None:
        official_fuel = official_fuel_price(payload.get('fuel_type'), payload.get('fuel_zone'), today)
    if benchmark is None:
        benchmark = lane_benchmark(payload, company)
    fuel = _fuel_item(official_fuel, payload)
    tolls = _tolls_item(extracted.get('tolls'), payload, sources_by_id, pages, today, company)
    driver = _driver_item(extracted.get('driver_allowance'), payload, sources_by_id, pages, today)
    cross_border = _f(payload.get('cross_border_cost'), 0.0) or 0.0
    pass_through_market = fuel['ai_value_zar'] + tolls['ai_value_zar'] + driver['ai_value_zar'] + cross_border
    base = _base_rate_item(benchmark, payload, pass_through_market)
    items = {'fuel': fuel, 'tolls': tolls, 'driver_allowance': driver, 'base_rate': base}

    verified = sum(1 for t in TOPICS if items[t]['verdict'] != 'could_not_verify')
    status, confidence = (('unverified', 'low') if verified == 0 else
                          ('verified', 'high') if verified == len(TOPICS) else ('partially_verified', 'medium'))

    parts = []
    for t in TOPICS:
        verdict = items[t]['verdict']
        parts.append(f'{ITEM_LABELS[t]} ' + ('adjusted to market' if verdict == 'needs_adjustment'
                                              else 'at market' if verdict == 'accurate' else 'not verified'))
    unverified = [f'{ITEM_LABELS[t].lower()} ({items[t]["verification_note"]})'
                  for t in TOPICS if items[t]['verdict'] == 'could_not_verify']

    references, seen = [], set()
    for t in TOPICS:
        for src in items[t]['sources']:
            if src['url'] not in seen:
                seen.add(src['url'])
                references.append(src)

    combos = _combinations(items, payload)
    default_key = choice_key({t: 'ai' if items[t]['toggleable'] else 'mine' for t in TOPICS})
    return {
        'verification_status': status,
        'confidence': confidence,
        'price_reasoning': '; '.join(parts) + '.',
        'honesty_note': ('Not verified: ' + ', '.join(unverified) + '.') if unverified else None,
        'distance_km': round(_f(payload.get('distance_km'), 0.0) or 0.0, 1),
        'legs': _legs(payload),
        'cross_border_zar': _money(cross_border),
        'cost_breakdown': items,
        'toggleable_items': [t for t in TOPICS if items[t]['toggleable']],
        'combinations': combos,
        'default_choice_key': default_key,
        'references': references,
        'return_leg': _return_leg(items, payload),
    }


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------

def _usd_cost(usage, *, model=None, web_search_calls=0) -> dict:
    """usage: response.usage. Reasoning tokens bill at the ordinary output
    rate (already inside output_tokens). Web search adds $10/1k calls, plus —
    for fixed-block models, only if usage doesn't already count it — 8,000
    input tokens per search."""
    model = model or AI_QUOTE_ANALYSIS_MODEL
    pricing = OPENAI_PRICING.get(model)
    if pricing is None:
        # Never record an unknown model as free: that would also let it slip
        # under the daily budget. Charge it at the dearest known rates.
        logger.warning('AI price analysis: no OPENAI_PRICING entry for model %r; costing it at the '
                       'most expensive known rates until the table is updated', model)
        pricing = {k: max(p[k] for p in OPENAI_PRICING.values())
                   for k in ('input_per_1m', 'cached_input_per_1m', 'output_per_1m')}

    cached = getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', 0) or 0
    input_tokens = getattr(usage, 'input_tokens', 0) or 0
    output_tokens = getattr(usage, 'output_tokens', 0) or 0
    billable_input = max(input_tokens - cached, 0)

    input_cost = billable_input / 1e6 * pricing['input_per_1m'] + cached / 1e6 * pricing['cached_input_per_1m']
    output_cost = output_tokens / 1e6 * pricing['output_per_1m']
    search_cost = web_search_calls / 1000 * WEB_SEARCH_COST_PER_1000_CALLS
    if model in FIXED_SEARCH_BLOCK_TOKENS and not SEARCH_BLOCK_INCLUDED_IN_USAGE:
        search_cost += web_search_calls * FIXED_SEARCH_BLOCK_TOKENS[model] / 1e6 * pricing['input_per_1m']
    return {
        'input_cost_usd': round(input_cost, 6),
        'output_cost_usd': round(output_cost, 6),
        'search_cost_usd': round(search_cost, 6),
        'total_cost_usd': round(input_cost + output_cost + search_cost, 6),
    }


def _sum_usage(responses) -> dict:
    total = {'input_tokens': 0, 'cached_tokens': 0, 'output_tokens': 0, 'reasoning_tokens': 0}
    for r in responses:
        usage = getattr(r, 'usage', None)
        total['input_tokens'] += getattr(usage, 'input_tokens', 0) or 0
        total['cached_tokens'] += getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', 0) or 0
        total['output_tokens'] += getattr(usage, 'output_tokens', 0) or 0
        total['reasoning_tokens'] += getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', 0) or 0
    return total


def _usage_row(usage, prefix, extra=None):
    row = {
        f'{prefix}_input_tokens': getattr(usage, 'input_tokens', 0) or 0,
        f'{prefix}_cached_tokens': getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', 0) or 0,
        f'{prefix}_output_tokens': getattr(usage, 'output_tokens', 0) or 0,
        f'{prefix}_reasoning_tokens': getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', 0) or 0,
    }
    if extra:
        row.update(extra)
    return row


class _Usage:
    """Adapter so summed usage dicts can go through _usd_cost/_usage_row."""
    def __init__(self, d):
        self.input_tokens = d['input_tokens']
        self.output_tokens = d['output_tokens']
        self.input_tokens_details = type('D', (), {'cached_tokens': d['cached_tokens']})()
        self.output_tokens_details = type('D', (), {'reasoning_tokens': d['reasoning_tokens']})()


def _cost_fields(research_cost: dict, structuring_cost: dict) -> dict:
    token_cost = round(research_cost['input_cost_usd'] + research_cost['output_cost_usd']
                       + structuring_cost['input_cost_usd'] + structuring_cost['output_cost_usd'], 6)
    web_search_cost = research_cost['search_cost_usd']
    return {
        'token_cost_usd': Decimal(str(token_cost)),
        'web_search_cost_usd': Decimal(str(web_search_cost)),
        'total_cost_usd': Decimal(str(round(token_cost + web_search_cost, 6))),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def analyze_quote_price(*, payload: dict, user=None, company=None, quote=None, today: date = None) -> dict:
    """Never raises for anticipated OpenAI/parsing failures — degrades to an
    honest {'success': False, ...} and STILL writes an AIQuotePriceAnalysis
    row so spent tokens are never dropped from cost tracking. The whole run
    is held to AI_QUOTE_ANALYSIS_DEADLINE_SECONDS."""
    from core.models import AIQuotePriceAnalysis

    started = time.monotonic()
    deadline = started + AI_QUOTE_ANALYSIS_DEADLINE_SECONDS
    today = today or timezone.localdate()

    context = build_condensed_context(payload, today, company)
    trigger_type = payload.get('trigger_type') if payload.get('trigger_type') in ('auto', 'manual') else 'auto'
    row_fields = {
        'quote': quote,
        'company': company,
        'triggered_by': user if user is not None and getattr(user, 'is_authenticated', True) else None,
        'trigger_type': trigger_type,
        'model': AI_QUOTE_ANALYSIS_MODEL,
        'reasoning_effort': AI_QUOTE_ANALYSIS_REASONING_EFFORT if _reasoning_kwargs(AI_QUOTE_ANALYSIS_MODEL) else '',
        'request_context': context,
    }

    def remaining():
        return deadline - time.monotonic()

    def elapsed_ms():
        return int((time.monotonic() - started) * 1000)

    client = _client()

    # ---- 1. Web research for tolls + driver, in parallel (network only) ----
    responses, errors = {}, {}
    research_timeout = max(5.0, min(AI_QUOTE_ANALYSIS_RESEARCH_TIMEOUT_SECONDS, remaining() - 15))
    pool = ThreadPoolExecutor(max_workers=len(RESEARCH_TOPICS))
    futures = {t: pool.submit(_run_topic_research, client, t, context, research_timeout) for t in RESEARCH_TOPICS}
    # Meanwhile, in this thread (DB access stays here): the official fuel
    # price and the lane benchmark, which need no search at all.
    official_fuel = official_fuel_price(payload.get('fuel_type'), payload.get('fuel_zone'), today)
    benchmark = lane_benchmark(payload, company)
    for topic, future in futures.items():
        try:
            responses[topic] = future.result(timeout=max(remaining() - 12, 1.0))
        except FutureTimeout:
            errors[topic] = 'timed out'
        except Exception as exc:
            logger.warning('AI price analysis: %s research call failed: %s', topic, exc)
            errors[topic] = str(exc)[:500]
    pool.shutdown(wait=False, cancel_futures=True)

    # Even with every search failed, fuel and base rate are still checked
    # (tolls and driver just come back not verified).
    research_by_topic, numbered, url_to_id = {}, [], {}
    for topic in RESEARCH_TOPICS:
        response = responses.get(topic)
        if response is None:
            research_by_topic[topic] = {'text': 'NOT FOUND (research call failed)', 'source_ids': []}
            continue
        ids = []
        for cite in _extract_citations(response):
            if cite['url'] not in url_to_id:
                if len(numbered) >= AI_QUOTE_ANALYSIS_MAX_REFERENCES * len(RESEARCH_TOPICS):
                    continue
                url_to_id[cite['url']] = f'S{len(numbered) + 1}'
                numbered.append({'id': url_to_id[cite['url']], **cite})
            ids.append(url_to_id[cite['url']])
        research_by_topic[topic] = {'text': getattr(response, 'output_text', '') or '', 'source_ids': ids}
    sources_by_id = {c['id']: c for c in numbered}

    research_usage = _sum_usage(responses.values())
    web_search_calls = sum(_count_web_search_calls(r) for r in responses.values())
    research_cost = _usd_cost(_Usage(research_usage), web_search_calls=web_search_calls)
    row_fields.update(_usage_row(_Usage(research_usage), 'research', {'research_web_search_calls': web_search_calls}))
    structuring_cost = {'input_cost_usd': 0, 'output_cost_usd': 0, 'search_cost_usd': 0, 'total_cost_usd': 0}
    raw_base = {'research': research_by_topic, 'research_errors': errors, 'citations': numbered,
                'structuring_model': AI_QUOTE_ANALYSIS_STRUCTURING_MODEL}

    def failed(stage, exc, pages=None):
        if pages is None:
            fetch.result(timeout=0)
        return AIQuotePriceAnalysis.objects.create(
            **row_fields, status='failed', failed_at_call=stage, error_message=str(exc)[:2000],
            **_cost_fields(research_cost, structuring_cost), raw_result=raw_base, duration_ms=elapsed_ms(),
        )

    # ---- 2+3. Start fetching cited pages, then extract figures ----
    fetch = source_verification.SourceFetchBatch([c['url'] for c in numbered])
    extracted = None
    if numbered:
        try:
            if remaining() < 6:
                raise TimeoutError('no time left for the structuring call')
            structuring_response = _run_structuring_call(client, research_by_topic, numbered,
                                                         timeout=min(15.0, remaining() - 4))
            # Record what was billed BEFORE looking at the content, so a
            # refusal or bad JSON still shows up in cost tracking.
            structuring_cost = _usd_cost(structuring_response.usage, model=AI_QUOTE_ANALYSIS_STRUCTURING_MODEL)
            row_fields.update(_usage_row(structuring_response.usage, 'structuring'))
            raw_text = getattr(structuring_response, 'output_text', None)
            if not raw_text:
                raise ValueError('structuring call returned no output_text (possible refusal)')
            extracted = json.loads(raw_text)
        except Exception as exc:
            # Tolls and driver can't be verified without it; fuel and base
            # rate still can. The spend is recorded either way.
            logger.warning('AI price analysis: structuring call failed: %s', exc)
            errors['structuring'] = str(exc)[:500]
            extracted = None
    pages = fetch.result(timeout=max(remaining() - 1, 0.5))

    # ---- 4+5. Verdicts, pricing combinations, win probability ----
    try:
        pricing = compute_pricing(extracted, payload, sources_by_id, pages, today,
                                  official_fuel=official_fuel, benchmark=benchmark)
        pricing['win_model'] = _attach_win_probabilities(
            pricing['combinations'], pricing['default_choice_key'], payload, user, company)
        default = pricing['combinations'][pricing['default_choice_key']]
    except Exception as exc:
        logger.exception('AI price analysis: pricing failed')
        row = failed('pricing', exc, pages=pages)
        return {'success': False, 'verification_status': 'unverified',
                'message': UNAVAILABLE_MESSAGE, 'usage_log_id': row.id}

    row = AIQuotePriceAnalysis.objects.create(
        **row_fields, status='success',
        error_message='; '.join(f'{t}: {e}' for t, e in errors.items())[:2000],
        **_cost_fields(research_cost, structuring_cost),
        suggested_price_zar=Decimal(str(default['price_zar'])),
        verification_status=pricing['verification_status'],
        confidence=pricing['confidence'],
        raw_result={
            **raw_base,
            'extracted': extracted,
            'official_fuel': official_fuel,
            'benchmark': benchmark,
            'page_checks': {url: {'readable': bool(r.get('text')), 'error': r.get('error')} for url, r in pages.items()},
            'pricing': {k: v for k, v in pricing.items() if k != 'references'},
            'references': pricing['references'],
        },
        duration_ms=elapsed_ms(),
    )
    return {'success': True, 'usage_log_id': row.id, **pricing}
