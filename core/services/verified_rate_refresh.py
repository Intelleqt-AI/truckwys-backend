"""Monthly refresh of the stored, verified figures the AI price check uses
(SANRAL toll tariffs and the driver night-out allowance).

This is the ONLY place the price-check feature talks to OpenAI. The
per-quote check (core.services.quote_ai_pricing) makes no web or OpenAI
calls: it compares a quote against the stored figures. The figures change
about once a year (SANRAL tariffs every 1 March; the NBCRFLI allowance
yearly), so looking them up once a month instead of twice per quote costs a
few US cents a month instead of about US$0.023 per check.

Pipeline, per lookup (one per SANRAL class, one for the allowance):
  1. RESEARCH call, forced to web-search (tool_choice='required', capped
     with max_tool_calls).
  2. STRUCTURING call (strict json_schema, no tools) that only EXTRACTS the
     figures and their start dates, each tied to source ids S1..Sn from a
     per-run enum. (web_search + strict json_schema in the same call has a
     documented truncation bug, hence two stages.)
  3. SOURCE CHECK (core.services.source_verification): the figure must
     appear on a page it cites, dated for the period in force (1 March).
  4. Compare with the approved figure. Only a figure that DIFFERS and is
     confirmed on its source becomes a PENDING proposal
     (core.services.verified_rates.propose). Nothing is ever applied here:
     a superuser approves or rejects it. A figure that matches refreshes
     the stored figure's verified_at date.

Every lookup writes an AIQuotePriceAnalysis row (trigger_type='refresh',
company=None) with its tokens and cost, so the admin AI-usage page and the
platform daily budget (AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD) see it.
Honours the kill switch (AI_PRICE_ANALYSIS_ENABLED); without an OpenAI key it
skips cleanly. Never raises for an anticipated failure.
"""
import json
import logging
import os
import re
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from core.services import source_verification
from core.services.quote_ai_pricing import (
    DATE_NEAR_CHARS, DRIVER_ALLOWANCE_TYPES, DRIVER_RATE_MAX_PER_DAY, PLAZA_TARIFF_MAX, SANRAL_CLASS_LABELS,
    _f, _freshness, _long_date, _schedule_date_forms, _toll_schedule_start,
)

logger = logging.getLogger(__name__)

AI_QUOTE_ANALYSIS_MODEL = getattr(settings, 'AI_QUOTE_ANALYSIS_MODEL', 'gpt-4o-mini')
AI_QUOTE_ANALYSIS_STRUCTURING_MODEL = getattr(settings, 'AI_QUOTE_ANALYSIS_STRUCTURING_MODEL', 'gpt-4o-mini')
AI_QUOTE_ANALYSIS_REASONING_EFFORT = getattr(settings, 'AI_QUOTE_ANALYSIS_REASONING_EFFORT', 'low')
AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE = getattr(settings, 'AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE', 'medium')
AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS = float(getattr(settings, 'AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS', 60))
AI_QUOTE_ANALYSIS_MAX_REFERENCES = int(getattr(settings, 'AI_QUOTE_ANALYSIS_MAX_REFERENCES', 8))
# Hard cap on web_search calls per research call: API-enforced, not a prompt
# request. A toll lookup lists every plaza, so it gets one extra search.
VERIFIED_RATES_MAX_WEB_SEARCH_CALLS = int(getattr(settings, 'VERIFIED_RATES_MAX_WEB_SEARCH_CALLS', 2))
# SANRAL classes looked up each run (1 light, 2-4 heavy).
VERIFIED_RATES_TOLL_CLASSES = tuple(getattr(settings, 'VERIFIED_RATES_TOLL_CLASSES', (1, 2, 3, 4)))
# How long to wait for the cited pages (in total, per lookup).
SOURCE_FETCH_WAIT_SECONDS = 30.0

JOB_NAME = 'refresh_verified_rates'


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


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(('gpt-5', 'o1', 'o3', 'o4'))


def _reasoning_kwargs(model: str) -> dict:
    """Only reasoning models accept `reasoning`; gpt-4o-mini rejects it."""
    if _is_reasoning_model(model):
        return {'reasoning': {'effort': AI_QUOTE_ANALYSIS_REASONING_EFFORT}}
    return {}


def _api_key() -> str:
    return (os.environ.get('OPENAI_API_KEY') or getattr(settings, 'OPENAI_API_KEY', '') or '').strip()


def _client():
    """Fresh client per run so a rotated key takes effect without a restart.
    No SDK retries: a failed lookup just proposes nothing this month.
    THIS is the test mock point: patch 'core.services.verified_rate_refresh._client'."""
    from openai import OpenAI
    return OpenAI(api_key=_api_key(), timeout=AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS, max_retries=0)



def refresh_unavailable_reason():
    """Why the refresh can't run at all right now, or None if it can."""
    if not getattr(settings, 'AI_PRICE_ANALYSIS_ENABLED', True):
        return 'disabled'
    if not _api_key():
        return 'no_api_key'
    return None


def budget_exhausted(now=None) -> bool:
    """True once today's recorded OpenAI spend (every AIQuotePriceAnalysis
    row, local day) reaches AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD. Per-quote
    checks record $0, so in practice this is the refresh job's own spend.
    0 (or less) switches the cap off."""
    from core.models import AIQuotePriceAnalysis

    budget = float(getattr(settings, 'AI_PRICE_ANALYSIS_GLOBAL_DAILY_BUDGET_USD', 5) or 0)
    if budget <= 0:
        return False
    now = now or timezone.now()
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    spent = (AIQuotePriceAnalysis.objects.filter(created_at__gte=day_start)
             .aggregate(total=Sum('total_cost_usd'))['total'] or 0)
    if float(spent) >= budget:
        logger.warning('refresh_verified_rates: platform daily budget of $%.2f reached ($%.4f spent)', budget, spent)
        return True
    return False


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM_PROMPT = """You are a South African road-freight pricing researcher. Use web \
search to find the figure asked for AS IT IS IN FORCE TODAY, from an official or authoritative \
source. Report each figure EXACTLY as printed on the source (same digits, same unit — do not \
round or convert), the source's name, and the date the figure took effect exactly as printed \
(day, month and year). Never fall back to an older figure or a different kind of figure: if you \
cannot find the one asked for, in force today, say NOT FOUND. Do not estimate, average, or \
calculate. Keep it short: a few lines."""

TOPIC_INSTRUCTIONS = {
    'tolls': ("Today is {today}. Find the SANRAL toll tariffs for {sanral_class_label} in force today "
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


def topic_prompt(topic: str, today: date, *, sanral_class_label: str = None, plazas=()) -> str:
    start = _toll_schedule_start(today)
    return TOPIC_INSTRUCTIONS[topic].format(
        today=_long_date(today),
        tariff_year=start.year,
        next_year=start.year + 1,
        sanral_class_label=sanral_class_label or 'the applicable heavy-vehicle class',
        plazas=', '.join(plazas) or 'all mainline plazas',
    )


def _run_topic_research(client, prompt: str):
    return client.responses.create(
        model=AI_QUOTE_ANALYSIS_MODEL,
        input=[
            {'role': 'system', 'content': RESEARCH_SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt},
        ],
        tools=[{'type': 'web_search', 'search_context_size': AI_QUOTE_ANALYSIS_SEARCH_CONTEXT_SIZE}],
        # Must search: an answer from the model's memory is an assumption.
        tool_choice='required',
        max_tool_calls=VERIFIED_RATES_MAX_WEB_SEARCH_CALLS,
        timeout=AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS,
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


def _run_structuring_call(client, research_by_topic: dict, numbered_citations: list,
                          timeout: float = AI_QUOTE_ANALYSIS_TIMEOUT_SECONDS):
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
# One lookup: research -> structuring -> fetch cited pages. Records its spend.
# ---------------------------------------------------------------------------

def _lookup(client, *, topic: str, prompt: str, context: dict, triggered_by=None) -> dict:
    """{'extracted', 'sources_by_id', 'pages', 'research_text', 'error',
    'usage_row'}. Never raises; the usage row is written whatever happens,
    so spent tokens are never dropped from cost tracking."""
    from core.models import AIQuotePriceAnalysis

    started = timezone.now()
    row_fields = {
        'company': None,
        'triggered_by': triggered_by if getattr(triggered_by, 'is_authenticated', False) else None,
        'trigger_type': 'refresh',
        'model': AI_QUOTE_ANALYSIS_MODEL,
        'reasoning_effort': AI_QUOTE_ANALYSIS_REASONING_EFFORT if _reasoning_kwargs(AI_QUOTE_ANALYSIS_MODEL) else '',
        'request_context': {'job': JOB_NAME, 'topic': topic, **context},
    }
    zero = {'input_cost_usd': 0, 'output_cost_usd': 0, 'search_cost_usd': 0, 'total_cost_usd': 0}
    research_cost, structuring_cost = dict(zero), dict(zero)
    out = {'extracted': None, 'sources_by_id': {}, 'pages': {}, 'research_text': '', 'error': None, 'usage_row': None}
    failed_at = ''

    def ms():
        return int((timezone.now() - started).total_seconds() * 1000)

    try:
        failed_at = 'research'
        response = _run_topic_research(client, prompt)
        web_search_calls = _count_web_search_calls(response)
        research_cost = _usd_cost(response.usage, web_search_calls=web_search_calls)
        row_fields.update(_usage_row(response.usage, 'research', {'research_web_search_calls': web_search_calls}))
        out['research_text'] = getattr(response, 'output_text', '') or ''
        numbered = []
        for cite in _extract_citations(response)[:AI_QUOTE_ANALYSIS_MAX_REFERENCES]:
            numbered.append({'id': f'S{len(numbered) + 1}', **cite})
        out['sources_by_id'] = {c['id']: c for c in numbered}
        if not numbered:
            raise ValueError('the research cited no sources')
        # Fetch the cited pages while the figures are extracted.
        fetch = source_verification.SourceFetchBatch([c['url'] for c in numbered])
        failed_at = 'structuring'
        research = {t: {'text': 'NOT FOUND', 'source_ids': []} for t in ('tolls', 'driver_allowance')}
        research[topic] = {'text': out['research_text'], 'source_ids': [c['id'] for c in numbered]}
        try:
            structuring = _run_structuring_call(client, research, numbered)
        finally:
            out['pages'] = fetch.result(timeout=SOURCE_FETCH_WAIT_SECONDS)
        structuring_cost = _usd_cost(structuring.usage, model=AI_QUOTE_ANALYSIS_STRUCTURING_MODEL)
        row_fields.update(_usage_row(structuring.usage, 'structuring'))
        raw_text = getattr(structuring, 'output_text', None)
        if not raw_text:
            raise ValueError('structuring call returned no output_text (possible refusal)')
        out['extracted'] = json.loads(raw_text)
        failed_at = ''
    except Exception as exc:
        logger.warning('refresh_verified_rates: %s lookup failed at %s: %s', topic, failed_at, exc)
        out['error'] = str(exc)[:500]

    out['usage_row'] = AIQuotePriceAnalysis.objects.create(
        **row_fields,
        status='failed' if out['error'] else 'success',
        failed_at_call=failed_at if out['error'] else '',
        error_message=out['error'] or '',
        **_cost_fields(research_cost, structuring_cost),
        raw_result={
            'research_text': out['research_text'][:4000],
            'citations': list(out['sources_by_id'].values()),
            'extracted': out['extracted'],
            'page_checks': {u: {'readable': bool(r.get('text')), 'error': r.get('error')}
                            for u, r in out['pages'].items()},
        },
        duration_ms=ms(),
    )
    return out


def _confirming_source(value, sources, pages, **check_kwargs):
    """(source dict, note) for the first cited page that shows `value` (dated
    as check_kwargs require), else (None, why not)."""
    note = source_verification.REASON_NO_SOURCE
    for src in sources:
        ok, note = source_verification.check_figure(value, [src['url']], pages, **check_kwargs)
        if ok:
            return src, note
    return None, note


# ---------------------------------------------------------------------------
# Tolls
# ---------------------------------------------------------------------------

def _refresh_toll_class(client, sanral_class: int, plazas: list, today: date, summary: dict, outcomes: dict,
                        triggered_by=None):
    """Look up one SANRAL class for every active plaza; propose changes."""
    from core.models import VerifiedRate
    from core.services import verified_rates
    from core.services.toll_calculator import tariff_excl_vat

    label = SANRAL_CLASS_LABELS[sanral_class]
    names = [p.name for p in plazas]
    result = _lookup(client, topic='tolls', triggered_by=triggered_by,
                     prompt=topic_prompt('tolls', today, sanral_class_label=label, plazas=names),
                     context={'sanral_class': sanral_class, 'plazas': names})
    summary['runs'] += 1
    summary['cost_usd'] += float(result['usage_row'].total_cost_usd)
    if result['error']:
        summary['errors'].append(f'class {sanral_class}: {result["error"]}')
        return
    found = [p for p in (((result['extracted'] or {}).get('tolls') or {}).get('plazas') or []) if isinstance(p, dict)]
    column = f'tariff_class_{sanral_class + 1}'
    for plaza in plazas:
        key = verified_rates.toll_key(plaza.id, sanral_class)
        candidates = _plaza_candidates(plaza.name, found)
        tariffs = {_f(found[i].get('tariff_zar')) for i in candidates}
        if len(tariffs) != 1:
            summary['unverified'].append({'key': key, 'note': 'not found' if not tariffs else 'ambiguous name'})
            continue
        fp = found[candidates[0]]
        tariff = _f(fp.get('tariff_zar'))
        fresh, fresh_note, _, eff = _freshness(fp.get('effective_date'), today)
        if tariff is None or not (0 < tariff <= PLAZA_TARIFF_MAX):
            summary['unverified'].append({'key': key, 'note': 'failed sanity check'})
            continue
        if not fresh:
            summary['unverified'].append({'key': key, 'note': fresh_note})
            continue
        # SANRAL prints tariffs incl. VAT: matched on the page as printed.
        src, note = _confirming_source(tariff, _map_sources(fp.get('sources'), result['sources_by_id']),
                                       result['pages'], near_any=_schedule_date_forms(eff))
        if src is None:
            summary['unverified'].append({'key': key, 'note': note})
            continue
        published = Decimal(str(tariff)).quantize(Decimal('0.01'))
        stored = getattr(plaza, column)
        state = outcomes.setdefault(plaza.id, {'plaza': plaza, 'confirmed': [], 'changed': False})
        if published == stored:
            state['confirmed'].append({'effective_from': eff, 'source': src})
            summary['confirmed'].append(key)
            continue
        state['changed'] = True
        row, outcome = verified_rates.propose(
            kind=VerifiedRate.KIND_TOLL_TARIFF, key=key, label=f'{plaza.name} ({plaza.route}) {label}',
            value=tariff_excl_vat(published), published_value=published, previous_value=tariff_excl_vat(stored),
            unit='per_passage', effective_from=eff, source_url=src['url'], source_name=src['title'],
            verified_at=today, proposed_by=JOB_NAME, toll_plaza=plaza, sanral_class=sanral_class,
            refresh_run=result['usage_row'],
            evidence={'published_incl_vat': float(published), 'stored_incl_vat': float(stored),
                      'as_extracted': fp, 'page_check': note},
        )
        summary['proposals'].append({'id': row.id, 'key': key, 'outcome': outcome})


def _record_confirmed_tolls(outcomes: dict, today: date):
    """A plaza whose looked-up tariffs all matched (none differed) is marked
    verified today on the page that confirmed them. Metadata only: no
    tariff changes here."""
    for state in outcomes.values():
        if state['changed'] or not state['confirmed']:
            continue
        plaza = state['plaza']
        newest = max(state['confirmed'], key=lambda c: c['effective_from'])
        plaza.tariff_verified_at = today
        plaza.tariff_source_url = newest['source']['url']
        plaza.tariff_source_name = newest['source']['title']
        if plaza.tariff_effective_from is None or newest['effective_from'] > plaza.tariff_effective_from:
            plaza.tariff_effective_from = newest['effective_from']
        plaza.save(update_fields=['tariff_verified_at', 'tariff_source_url', 'tariff_source_name',
                                  'tariff_effective_from', 'updated_at'])


# ---------------------------------------------------------------------------
# Driver allowance
# ---------------------------------------------------------------------------

def _refresh_allowance(client, today: date, summary: dict, triggered_by=None):
    from core.models import VerifiedRate
    from core.services import verified_rates

    result = _lookup(client, topic='driver_allowance', triggered_by=triggered_by,
                     prompt=topic_prompt('driver_allowance', today), context={})
    summary['runs'] += 1
    summary['cost_usd'] += float(result['usage_row'].total_cost_usd)
    if result['error']:
        summary['errors'].append(f'driver allowance: {result["error"]}')
        return
    raw = (result['extracted'] or {}).get('driver_allowance') or {}
    rate, kind = _f(raw.get('rate_per_day_zar')), raw.get('allowance_type')

    def unverified(note):
        summary['unverified'].append({'key': f'driver_allowance:{kind}', 'note': note})

    if rate is None:
        return unverified('not found')
    if kind not in DRIVER_ALLOWANCE_TYPES:
        return unverified('not a recognised driver allowance')
    if not (0 < rate <= DRIVER_RATE_MAX_PER_DAY):
        return unverified('failed sanity check')
    fresh, fresh_note, _, eff = _freshness(raw.get('effective_date'), today)
    if not fresh:
        return unverified(fresh_note)
    sources = _map_sources(raw.get('sources'), result['sources_by_id'])
    if kind == 'sars_subsistence':
        # SARS tables label each row by the year the tax year ENDS.
        src, note = _confirming_source(rate, sources, result['pages'], preceding_year=eff.year + 1)
    else:
        src, note = _confirming_source(rate, sources, result['pages'],
                                       near_any=_schedule_date_forms(eff), near_chars=DATE_NEAR_CHARS)
    if src is None:
        return unverified(note)
    value = Decimal(str(rate)).quantize(Decimal('0.01'))
    current = verified_rates.current_allowance_row(kind, today)
    if current is not None and current.value == value:
        current.verified_at = today
        current.save(update_fields=['verified_at', 'updated_at'])
        summary['confirmed'].append(f'driver_allowance:{kind}')
        return
    row, outcome = verified_rates.propose(
        kind=VerifiedRate.KIND_DRIVER_ALLOWANCE, key=kind, label=DRIVER_ALLOWANCE_TYPES[kind], value=value,
        published_value=value, previous_value=current.value if current is not None else None, unit='per_night',
        effective_from=eff, source_url=src['url'], source_name=src['title'], verified_at=today,
        proposed_by=JOB_NAME, refresh_run=result['usage_row'], evidence={'as_extracted': raw, 'page_check': note},
    )
    summary['proposals'].append({'id': row.id, 'key': f'driver_allowance:{kind}', 'outcome': outcome})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

KINDS = ('toll_tariff', 'driver_allowance')


def run_refresh(*, today: date = None, kinds=KINDS, sanral_classes=None, triggered_by=None) -> dict:
    """Look up the current SANRAL tariffs and driver allowance and propose
    the ones that changed. Returns a summary:
    {'status': 'ok' | 'skipped' | 'stopped', 'reason', 'runs', 'cost_usd',
     'proposals': [{'id', 'key', 'outcome'}], 'confirmed': [keys],
     'unverified': [{'key', 'note'}], 'errors': [...]}.
    Never raises for an anticipated failure."""
    from core.models import TollPlaza

    today = today or timezone.localdate()
    summary = {'status': 'ok', 'reason': None, 'runs': 0, 'cost_usd': 0.0, 'proposals': [], 'confirmed': [],
               'unverified': [], 'errors': []}
    reason = refresh_unavailable_reason()
    if reason is None and budget_exhausted():
        reason = 'budget'
    if reason is not None:
        logger.warning('refresh_verified_rates skipped: %s', reason)
        return dict(summary, status='skipped', reason=reason)
    try:
        client = _client()
    except Exception as exc:
        logger.error('refresh_verified_rates: OpenAI client could not start: %s', type(exc).__name__)
        return dict(summary, status='skipped', reason='client_error')

    steps = []
    if 'toll_tariff' in kinds:
        plazas = list(TollPlaza.objects.filter(is_active=True).order_by('route', 'location_km'))
        outcomes = {}
        for sanral_class in (sanral_classes or VERIFIED_RATES_TOLL_CLASSES):
            if int(sanral_class) in SANRAL_CLASS_LABELS and plazas:
                steps.append(lambda c=int(sanral_class): _refresh_toll_class(
                    client, c, plazas, today, summary, outcomes, triggered_by))
    if 'driver_allowance' in kinds:
        steps.append(lambda: _refresh_allowance(client, today, summary, triggered_by))
    for step in steps:
        # Checked before every paid lookup, not just once.
        if budget_exhausted():
            summary.update(status='stopped', reason='budget')
            break
        step()
    if 'toll_tariff' in kinds:
        _record_confirmed_tolls(outcomes, today)
    summary['cost_usd'] = round(summary['cost_usd'], 6)
    logger.info('refresh_verified_rates: %s lookups, $%.4f, %d proposals, %d confirmed, %d unverified',
                summary['runs'], summary['cost_usd'], len(summary['proposals']), len(summary['confirmed']),
                len(summary['unverified']))
    return summary
