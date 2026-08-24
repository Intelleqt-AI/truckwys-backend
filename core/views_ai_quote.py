"""
AI Quote & Revenue Guard API endpoints for Phase 2 desktop quoting.
Sprint 1: AI Quoting Engine Upgrade with feedback loop, fuel alerts, win probability.
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from django.utils import timezone
from django.db.models import Avg, Count, Q, Min, Max, F
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from core.models import Quote, QuoteOutcome, FuelPrice, Customer, Invoice
from core.services.fuel_price import fetch_fuel_prices
from core.services.quote_ml import QuoteMLModel

logger = logging.getLogger(__name__)


def _sg_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _sg_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _sg_clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _sg_extract_json(text):
    """Best-effort parse of the first JSON object in an LLM reply (or None)."""
    if not text:
        return None
    import json
    import re
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


class FuelPriceCurrentView(APIView):
    """GET /api/v1/fuel-prices/current/ — returns current diesel price with staleness check.
    Pass ?force=true (e.g. a manual "Fetch Now" button) to bypass the normal
    once-per-hour live-retry gate and re-check the live sources immediately."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            force = request.query_params.get('force', '').lower() == 'true'
            fuel_price = fetch_fuel_prices(force_update=force)

            # Stale if: source is a fallback (live scrape failed), or data is >35 days old
            days_old = (timezone.now().date() - fuel_price.date).days
            is_fallback = fuel_price.source in ('FALLBACK', 'FALLBACK_LATEST')
            is_stale = is_fallback or days_old > 35

            if is_fallback:
                # Don't hand over a substituted number dressed up as current —
                # the live sources (AA SA, SAPIA, DMRE) are all presently
                # broken (moved page / 404 / unreachable, not a transient
                # blip — see fuel_price.py's scraper functions), so silently
                # substituting an old figure would read as "the price" to
                # anyone glancing at it. Leave the fields empty and say so
                # plainly instead; a human can enter today's real price below.
                return Response({
                    'success': True,
                    'inland_price': None,
                    'coastal_price': None,
                    'last_updated': None,
                    # When we actually last checked a live source — distinct
                    # from `last_updated`/`date`, which is just the calendar
                    # month a price represents. Shown even on a fallback so
                    # "checked 20 seconds ago and got nothing live" reads
                    # differently from "hasn't been checked in days."
                    'last_checked_at': fuel_price.fetched_at.isoformat(),
                    'is_stale': True,
                    'source': fuel_price.source,
                    'stale_warning': "Couldn't reach any live fuel-price source right now — enter today's price manually below.",
                    'date': None,
                    'diesel_inland': None,
                    'diesel_coastal': None,
                    'petrol_95': None,
                    'petrol_93': None,
                })

            stale_warning = f"Last update {days_old} days ago; consider manual refresh" if is_stale else None

            return Response({
                'success': True,
                'inland_price': float(fuel_price.diesel_inland),
                'coastal_price': float(fuel_price.diesel_coastal),
                'last_updated': fuel_price.date.isoformat(),
                'last_checked_at': fuel_price.fetched_at.isoformat(),
                'is_stale': is_stale,
                'source': fuel_price.source,
                'stale_warning': stale_warning,
                # Legacy fields for backwards compatibility
                'date': fuel_price.date.isoformat(),
                'diesel_inland': float(fuel_price.diesel_inland),
                'diesel_coastal': float(fuel_price.diesel_coastal),
                'petrol_95': float(fuel_price.petrol_95) if fuel_price.petrol_95 else 0,
                'petrol_93': float(fuel_price.petrol_93) if fuel_price.petrol_93 else 0,
            })
        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        """Admin override: POST {diesel_inland, diesel_coastal} to set current month's price."""
        if not request.user.is_staff:
            return Response({'error': 'Staff only'}, status=status.HTTP_403_FORBIDDEN)

        diesel_inland = request.data.get('diesel_inland')
        diesel_coastal = request.data.get('diesel_coastal')
        if not diesel_inland:
            return Response({'error': 'diesel_inland is required'}, status=status.HTTP_400_BAD_REQUEST)

        from decimal import Decimal
        from datetime import date
        from core.models.fuel_price import FuelPrice

        today = date.today().replace(day=1)
        FuelPrice.objects.update_or_create(
            date=today,
            defaults={
                'diesel_inland': Decimal(str(diesel_inland)),
                'diesel_coastal': Decimal(str(diesel_coastal or diesel_inland)),
                'source': 'MANUAL',
            }
        )
        return Response({'success': True, 'date': today.isoformat(), 'diesel_inland': float(diesel_inland)})


class AIQuoteSuggestionView(APIView):
    """POST /api/v1/quotes/suggest/ — AI-suggested margin and price."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Expects:
        {
          "distance_km": 1400,
          "truck_type": 0,  # e.g., 0 = Flatbed
          "load_type": 0,
          "load_weight": 15000,
          "fuel_cost": 5000,
          "toll_cost": 1200,
          "driver_cost": 800,
          "actual_cost": 10000
        }
        Returns AI suggestion or 503 if model not trained.
        """
        try:
            data = request.data
            actual_cost = float(data.get('actual_cost', 0))
            if actual_cost <= 0:
                return Response({
                    'success': False,
                    'error': 'actual_cost must be > 0',
                }, status=status.HTTP_400_BAD_REQUEST)

            # The LightGBM margin model is OPTIONAL. If its libraries are missing or
            # it isn't trained, we DON'T error — we ground the suggestion in the real
            # cost breakdown + real lane market rate + the expected-profit optimiser,
            # and let OpenAI produce/justify the price (validated so it stays real).
            distance_km = _sg_float(data.get('distance_km'), 0.0)
            fuel_cost = _sg_float(data.get('fuel_cost'), 0.0)
            toll_cost = _sg_float(data.get('toll_cost'), 0.0)
            driver_cost = _sg_float(data.get('driver_cost'), 0.0)
            client_tier = _sg_int(data.get('client_tier'), 1)
            days_until = _sg_int(data.get('days_until_departure'), 7)
            hist = _sg_clamp(_sg_float(data.get('historical_acceptance_rate'), 0.5), 0.0, 1.0)

            from core.views import resolve_user_company
            company = resolve_user_company(request.user)

            # 1) Real lane market rate (cross-platform -> own quotes -> SA estimate -> cost anchor).
            origin = str(data.get('origin') or '').strip()
            destination = str(data.get('destination') or '').strip()
            vehicle_type = str(data.get('vehicle_type') or '').strip()
            market_rate, market_rate_source = 0.0, 'none'
            try:
                from core.services.lane_benchmark import resolve_market_rate
                rate, src = resolve_market_rate(origin, destination, vehicle_type or None, company=company)
                if rate and rate > 0:
                    market_rate, market_rate_source = float(rate), src
            except Exception as exc:
                logger.warning('suggest: market-rate resolve failed: %s', exc)
            if market_rate <= 0:
                market_rate = actual_cost * 1.25
                market_rate_source = 'cost_anchor'

            # 2) Deterministic, grounded expected-profit optimum (anchor + sane band).
            from core.services.margin_optimizer import optimize_price, _route_popularity
            opt = optimize_price(
                total_cost=actual_cost, market_rate=market_rate,
                client_tier=client_tier, days_until_departure=days_until,
                historical_acceptance_rate=hist,
                origin=origin or None, destination=destination or None,
            )
            anchor_price = _sg_float(opt.get('optimal_price'), 0.0) or round(actual_cost * 1.18, 2)
            curve = opt.get('curve') or []
            band_low = min((p['price'] for p in curve), default=round(actual_cost * 1.05, 2))
            band_high = max((p['price'] for p in curve), default=round(actual_cost * 1.45, 2))

            suggested_price = anchor_price
            confidence = 0.7
            rationale = ''
            source = 'optimizer'

            # 3) OpenAI layer — reasons over the REAL numbers; output validated + clamped.
            try:
                from core.services import agent as agent_svc
                if agent_svc._provider():
                    import json
                    payload = {
                        'actual_cost': round(actual_cost, 2),
                        'distance_km': distance_km,
                        'fuel_cost': round(fuel_cost, 2),
                        'toll_cost': round(toll_cost, 2),
                        'driver_cost': round(driver_cost, 2),
                        'market_rate': round(market_rate, 2),
                        'market_rate_source': market_rate_source,
                        'optimizer_anchor_price': round(anchor_price, 2),
                        'price_band': {'low': round(band_low, 2), 'high': round(band_high, 2)},
                        'client_tier': client_tier,
                        'days_until_departure': days_until,
                    }
                    sys_prompt = (
                        "You are a pricing analyst for a South African road-freight operator. "
                        "Suggest ONE quote price in ZAR that balances winning the load against margin, "
                        "grounded ONLY in the numbers provided (real cost breakdown, real market rate, "
                        "and the optimiser anchor/band). Never invent figures. The price MUST be >= "
                        "actual_cost and SHOULD stay within price_band. Reply with STRICT JSON only: "
                        '{"suggested_price": number, "confidence": number between 0 and 1, '
                        '"rationale": "one or two sentences"}'
                    )
                    raw = agent_svc._llm_generate(
                        sys_prompt, [{'role': 'user', 'content': json.dumps(payload)}]
                    )
                    parsed = _sg_extract_json(raw)
                    if parsed and parsed.get('suggested_price') is not None:
                        lo = max(actual_cost, band_low, anchor_price * 0.90)
                        hi = max(lo, min(band_high, anchor_price * 1.10))
                        suggested_price = _sg_clamp(_sg_float(parsed.get('suggested_price'), anchor_price), lo, hi)
                        confidence = _sg_clamp(_sg_float(parsed.get('confidence'), 0.7), 0.3, 0.95)
                        rationale = str(parsed.get('rationale') or '').strip()[:400]
                        source = 'openai'
            except Exception as exc:
                logger.warning('suggest: OpenAI layer failed, using optimizer: %s', exc)

            # 4) Win probabilities at the chosen price (and +/-5%). Heuristic-safe.
            win_probability = win_low = win_high = None
            try:
                from core.services.quote_ml import WinProbabilityModel
                try:
                    win_model = WinProbabilityModel()
                except Exception:
                    class _HeuristicWin:
                        model = None
                    win_model = _HeuristicWin()
                    win_model.predict_proba = WinProbabilityModel.predict_proba.__get__(win_model)

                # Same context features the model saw in training — without
                # them a trained model would score every quote as a Tuesday in
                # March on an average lane.
                _now = timezone.now()
                _pop = _route_popularity(origin or None, destination or None)

                def _pw(price):
                    ratio = (price / market_rate) if market_rate > 0 else 1.0
                    return round(float(win_model.predict_proba(
                        price_ratio=ratio, client_tier=client_tier,
                        days_until_departure=days_until, historical_acceptance_rate=hist,
                        month=_now.month, day_of_week=_now.weekday(),
                        route_popularity=_pop,
                    )), 2)
                win_probability = _pw(suggested_price)
                win_low = _pw(suggested_price * 0.95)
                win_high = _pw(suggested_price * 1.05)
            except Exception as exc:
                logger.warning('suggest: win-probability failed: %s', exc)

            margin_pct = round((suggested_price - actual_cost) / actual_cost * 100, 1) if actual_cost else 0.0
            margin_lower = round((band_low - actual_cost) / actual_cost * 100, 1) if actual_cost else 5.0
            margin_upper = round((band_high - actual_cost) / actual_cost * 100, 1) if actual_cost else 45.0

            response_data = {
                'success': True,
                'suggested_price': round(suggested_price, 2),
                'margin_pct': margin_pct,
                'confidence': round(confidence, 2),
                'margin_range': {'lower': margin_lower, 'upper': margin_upper},
                'source': source,
                'rationale': rationale,
                'market_rate': round(market_rate, 2),
                'market_rate_source': market_rate_source,
            }
            if win_probability is not None:
                response_data['win_probability'] = win_probability
                response_data['win_probability_at_lower_price'] = win_low
                response_data['win_probability_at_higher_price'] = win_high

            return Response(response_data)

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RevenueGuardView(APIView):
    """POST /api/v1/quotes/guard/ — Revenue Guard with explanations & suggestions."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Expects:
        {
          "total_cost": 10000,
          "quote_price": 12000,
          "distance_km": 1400,
          "fuel_cost": 5000,
          "toll_cost": 1200,
          "quote_id": optional (for enhanced analysis)
        }
        Returns risk badge: SAFE, CAUTION, AT_RISK with explanations & suggestions
        """
        try:
            data = request.data
            total_cost = float(data.get('total_cost', 0))
            quote_price = float(data.get('quote_price', 0))
            distance_km = float(data.get('distance_km', 0))
            fuel_cost = float(data.get('fuel_cost', 0))
            quote_id = data.get('quote_id')

            if total_cost <= 0 or quote_price <= 0:
                return Response({
                    'success': False,
                    'error': 'total_cost and quote_price must be > 0',
                }, status=status.HTTP_400_BAD_REQUEST)

            from core.views import resolve_user_company
            company = resolve_user_company(request.user)
            quote = None
            if quote_id:
                try:
                    quote = Quote.objects.get(id=quote_id, company=company)
                except Quote.DoesNotExist:
                    quote = None

            customer = None
            if data.get('customer_id'):
                customer = Customer.objects.filter(
                    id=data['customer_id'], company=company,
                ).first()

            from core.services.quote_analysis import assess_revenue_guard
            result = assess_revenue_guard(
                total_cost=total_cost, quote_price=quote_price,
                distance_km=distance_km, fuel_cost=fuel_cost,
                company=company, quote=quote, customer=customer,
            )
            result.setdefault('factors', [])  # legacy field
            return Response(result)

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AIQuoteAnalyzeView(APIView):
    """POST /api/v1/quotes/analyze/ — one comprehensive AI analysis of a quote.

    Synthesises cost correctness, fuel freshness/usage, profit optimisation and
    market context into a single response, plus an LLM narrative + suggested
    price. Consumes the Step 1 + Step 2 data the New Quote flow already has.
    """
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _derive_client_features(company, customer_id):
        """Real per-customer signals from quote history: (tier, acceptance_rate).
        Falls back to ('standard', 0.5) when there is no usable history."""
        tier, hist_rate = 'standard', 0.5
        try:
            decided = Q(outcome__in=['accepted', 'rejected']) | Q(
                status__in=['ACCEPTED', 'IT', 'COMPLETED', 'DECLINED'])
            won = Q(outcome='accepted') | Q(status__in=['ACCEPTED', 'IT', 'COMPLETED'])
            base = Quote.objects.filter(company=company, customer_id=customer_id)
            total = base.filter(decided).distinct().count()
            accepted = base.filter(won).distinct().count()
            if total > 0:
                hist_rate = accepted / total
            if accepted >= 10:
                tier = 'vip'
            elif accepted >= 3:
                tier = 'regular'
            else:
                tier = 'new'
        except Exception as exc:
            logger.warning('analyze: client feature derivation failed: %s', exc)
        return tier, hist_rate

    def post(self, request):
        try:
            data = request.data
            from core.views import resolve_user_company
            company = resolve_user_company(request.user)

            # Derive the win-model features from real data server-side; the
            # client only names the customer and the pickup date.
            client_tier = data.get('client_tier') or 'standard'
            hist_rate = data.get('historical_acceptance_rate')
            customer_id = data.get('customer_id')
            if customer_id:
                client_tier, derived_rate = self._derive_client_features(company, customer_id)
                if hist_rate is None:
                    hist_rate = derived_rate

            days_until_departure = data.get('days_until_departure')
            if days_until_departure is None and data.get('pickup_date'):
                try:
                    pickup = date.fromisoformat(str(data['pickup_date'])[:10])
                    days_until_departure = max(0, (pickup - timezone.now().date()).days)
                except (TypeError, ValueError):
                    days_until_departure = None

            payload = {
                'quote_total': data.get('quote_total'),
                'direct_cost': data.get('direct_cost'),
                'distance_km': data.get('distance_km'),
                'origin': data.get('origin'),
                'destination': data.get('destination'),
                'vehicle_type': data.get('vehicle_type'),
                'weight': data.get('weight'),
                'fuel_cost': data.get('fuel_cost'),
                'toll_cost': data.get('toll_cost'),
                'driver_cost': data.get('driver_cost'),
                'fuel_usage_litres': data.get('fuel_usage_litres'),
                'fuel_price_used': data.get('fuel_price_used'),
                'market_rate': data.get('market_rate'),
                'client_tier': client_tier,
                'days_until_departure': days_until_departure if days_until_departure is not None else 7,
                'historical_acceptance_rate': hist_rate if hist_rate is not None else 0.5,
                'customer_id': customer_id,
                'skip_narrative': bool(data.get('skip_narrative')),
            }
            from core.services.quote_analysis import analyze_quote
            result = analyze_quote(payload, company=company)
            if not result.get('success'):
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
            return Response(result)
        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AIChatQuoteView(APIView):
    """POST /api/v1/ai/chat-quote/ — conversational quote extraction."""
    permission_classes = [IsAuthenticated]

    # Plain intro for a greeting / "what can you do" when no load details exist yet.
    INTRO_REPLY = (
        "Hi! I'm the TruckWys quoting assistant — describe a load in plain English "
        "(pickup, delivery, cargo and weight) and I'll turn it into a freight quote. "
        "What trip would you like to quote?"
    )

    _HELP_RE = re.compile(
        r'\b(how (can|do) you help|what can you (do|help)|what do you do|'
        r'how does this (work|help)|who are you|what are you|can you help)\b', re.IGNORECASE)
    _GREETING_RE = re.compile(
        r'(hi|hey|hello|howzit|hiya|yo|good\s*(morning|afternoon|evening))\b', re.IGNORECASE)

    @classmethod
    def _is_help_question(cls, message):
        return bool(cls._HELP_RE.search((message or '').strip()))

    @classmethod
    def _looks_conversational(cls, message):
        """True when the message is a greeting or a 'what can you do' style question
        rather than load details — so we answer it instead of nagging for fields."""
        m = (message or '').strip()
        if not m:
            return False
        return bool(cls._GREETING_RE.match(m) or cls._HELP_RE.search(m))

    @classmethod
    def _conversational_reply(cls, message, merged, lang=None):
        """Deterministic answer to a greeting / capability question, kept
        quote-focused: explains what the assistant does and/or what it still
        needs, instead of blankly repeating a field prompt. `lang`, when a
        confidently-detected non-English code, gets the final English string
        translated on the fly (see language_detect.translate_template)."""
        has_essentials = any(merged.get(k) for k in
                             ('pickup_location', 'delivery_location', 'cargo_description', 'weight'))
        if cls._is_help_question(message):
            cap = ("I turn a plain-English load description into a freight quote — give me the "
                   "pickup, delivery, cargo and weight and I'll price it.")
            reply = f"{cap} {cls._fallback_reply(merged)}" if has_essentials \
                else f"{cap} What trip would you like to quote?"
        else:
            # Plain greeting
            reply = f"Happy to help! {cls._fallback_reply(merged)}" if has_essentials else cls.INTRO_REPLY
        from core.services import language_detect
        return language_detect.translate_template(reply, lang)

    @staticmethod
    def _fallback_reply(merged, lang=None):
        """Build a friendly reply from the fields captured so far. `lang`, when
        a confidently-detected non-English code, translates the final string."""
        missing = []
        if not merged.get('pickup_location'):
            missing.append('pickup location')
        if not merged.get('delivery_location'):
            missing.append('delivery location')
        if not merged.get('cargo_description'):
            missing.append('cargo type')
        if not merged.get('weight'):
            missing.append('weight')

        if not missing:
            reply = (
                f"Got it — {merged.get('cargo_description', 'your cargo')} from "
                f"{merged.get('pickup_location')} to {merged.get('delivery_location')}, "
                f"{merged.get('weight', 0) / 1000:.0f} tons. Ready to calculate your quote."
            )
        elif len(missing) <= 2:
            reply = f"Almost there. Just need the {' and '.join(missing)} to complete the quote."
        else:
            reply = f"Thanks! I still need the {', '.join(missing[:-1])} and {missing[-1]} to build your quote."
        from core.services import language_detect
        return language_detect.translate_template(reply, lang)

    @classmethod
    def _reply_for(cls, message, merged, extracted, llm_reply='', lang=None):
        """Pick the reply. A pure greeting / help question (no new load details
        this turn) is answered deterministically here, overriding whatever the LLM
        returned — so the assistant never ignores a direct question by repeating a
        field prompt. Otherwise use the LLM's reply (already in `lang`, per the
        authoritative-language prompt directive) or the field-progress fallback."""
        if not extracted and cls._looks_conversational(message):
            return cls._conversational_reply(message, merged, lang)
        return (llm_reply or '').strip() or cls._fallback_reply(merged, lang)

    def post(self, request):
        """
        Extract quote fields from natural language message.
        Body: { message, history, current_fields }
        Returns: { reply, extracted_fields }
        """
        try:
            message = request.data.get('message', '')
            current_fields = request.data.get('current_fields', {})
            history = request.data.get('history', [])
            pending_entity = request.data.get('pending_entity')
            declined_entities = request.data.get('declined_entities') or []

            # Authoritative detected language: from Whisper (voice — passed
            # through by the frontend from /ai/voice-quote/'s response) when
            # present, else a dedicated text detector for typed messages (no
            # transcription step exists for those). None means uncertain/
            # unavailable — every reply-producing branch below then falls back
            # to today's unchanged default behavior, never inventing a language.
            from core.services import language_detect
            detected_language = request.data.get('detected_language') or None
            if not detected_language:
                detected_language = language_detect.detect_text_language(message)

            # The fleet's real vehicle types and customers — extraction matches
            # free text against these, not a hardcoded generic list (a company's
            # actual types like "Rigid Truck" or "Semi-Trailer Truck" otherwise
            # never match a fixed enum, and there'd be no way to capture a client).
            # Includes the shared (company=None) default catalog too, mirroring
            # VehicleTypeViewSet.get_queryset — otherwise a company that never had
            # the defaults seeded onto its own account gives the matcher a much
            # narrower (or empty) candidate pool than what the vehicle-type
            # dropdown itself actually shows the user.
            company = getattr(request.user, 'company', None)
            vehicle_types = customers = None
            if company is not None:
                from core.models import VehicleType, Customer
                vehicle_types = list(
                    VehicleType.objects.filter(Q(company=None) | Q(company=company))
                    .values_list('name', flat=True).distinct()
                )
                customers = list(
                    Customer.objects.filter(company=company).values('id', 'name')
                )

            from core.services import quote_entity_chat

            # A create-on-the-fly conversation is in progress — this turn is
            # entirely the user's answer to it, not new quote-field content.
            if pending_entity and company is not None:
                nxt, ent_reply, created, link, declined_name = quote_entity_chat.advance_pending(
                    pending_entity, message, company, request.user, detected_language=detected_language,
                )
                extracted = {}
                if created:
                    extracted = (
                        {'customer_id': created['id'], 'customer_name': created['name']}
                        if created['table'] == 'customers' else {'vehicle_type': created['name']}
                    )
                    ent_reply = f"{ent_reply} {self._fallback_reply({**current_fields, **extracted}, detected_language)}"
                return Response({
                    'success': True,
                    'reply': ent_reply,
                    'extracted_fields': extracted,
                    'pending_entity': nxt,
                    'link': link,
                    'declined_entity': declined_name,
                })

            # Primary path: Claude-backed natural-language extraction.
            # Falls through to the regex extractor below when the LLM is
            # not configured or the call fails, so the endpoint never breaks.
            from core.services import llm_quote
            if llm_quote.is_enabled():
                try:
                    extracted, reply, unmatched = llm_quote.extract(
                        message, history, current_fields,
                        vehicle_types=vehicle_types, customers=customers,
                        detected_language=detected_language,
                    )
                    if company is not None:
                        hit = quote_entity_chat.detect_unmatched(unmatched, declined_entities)
                        if hit:
                            table, raw_name = hit
                            pending, ask_reply, link = quote_entity_chat.start_pending(
                                table, raw_name, request.user, detected_language=detected_language)
                            return Response({
                                'success': True,
                                'reply': ask_reply,
                                'extracted_fields': extracted,
                                'pending_entity': pending,
                                'link': link,
                                'declined_entity': None,
                                'source': 'llm',
                            })
                    merged = {**current_fields, **extracted}
                    # Deterministically answer a pure greeting / help question even
                    # if the LLM returned a field-nag; otherwise keep the LLM reply
                    # (already in `detected_language`, per the extraction prompt's
                    # authoritative-language directive).
                    reply = self._reply_for(message, merged, extracted, llm_reply=reply, lang=detected_language)
                    return Response({
                        'success': True,
                        'reply': reply,
                        'extracted_fields': extracted,
                        'pending_entity': None,
                        'link': None,
                        'declined_entity': None,
                        'source': 'llm',
                    })
                except Exception as exc:
                    logger.warning('LLM quote extraction failed, using regex fallback: %s', exc)

            # Extract fields using regex patterns
            extracted = {}
            msg_lower = message.lower()

            # Origin/Destination
            import re
            # SA cities list for reliable extraction
            SA_CITIES = [
                'Johannesburg', 'JHB', 'Joburg', 'Cape Town', 'CPT',
                'Durban', 'DBN', 'Pretoria', 'PTA', 'Port Elizabeth', 'PE',
                'Bloemfontein', 'BFN', 'East London', 'Nelspruit', 'Polokwane',
                'Kimberley', 'Pietermaritzburg', 'Richards Bay', 'Beit Bridge',
                'Maputo', 'Harare', 'Lusaka', 'Windhoek', 'Gaborone',
            ]
            city_pattern = '|'.join(re.escape(c) for c in SA_CITIES)
            # "from X to Y" pattern with city names
            route_match = re.search(
                rf'from\s+({city_pattern})\s+to\s+({city_pattern})',
                message, re.IGNORECASE
            )
            if route_match:
                extracted['pickup_location'] = route_match.group(1).strip()
                extracted['delivery_location'] = route_match.group(2).strip()
            else:
                # Fallback: generic from/to
                from_match = re.search(r'from\s+([A-Za-z][A-Za-z\s]{1,25}?)\s+to\s+', message, re.IGNORECASE)
                to_match = re.search(r'\s+to\s+([A-Za-z][A-Za-z\s]{1,25}?)(?:\s*[,\.]|\s+(?:on|next|flatbed|tautliner|refrigerated|tanker|\d)|$)', message, re.IGNORECASE)
                if from_match:
                    extracted['pickup_location'] = from_match.group(1).strip()
                if to_match:
                    extracted['delivery_location'] = to_match.group(1).strip()

                # Dictated "label: value" style ("Collection location: Cape Town
                # - Delivery: Durban") has no "from ... to ..." at all — catch
                # it as a second attempt.
                if 'pickup_location' not in extracted:
                    m = re.search(r'(?:collection|pickup)(?:\s+location)?\s*[:\-]\s*([A-Za-z][A-Za-z\s]{1,25}?)(?:\s*[-,.]|\s+(?:total|weight|pickup|delivery|valid)\b|$)', message, re.IGNORECASE)
                    if m:
                        extracted['pickup_location'] = m.group(1).strip()
                if 'delivery_location' not in extracted:
                    m = re.search(r'delivery(?:\s+location)?\s*[:\-]\s*([A-Za-z][A-Za-z\s]{1,25}?)(?:\s*[-,.]|\s+(?:total|weight|pickup|delivery|valid|will)\b|$)', message, re.IGNORECASE)
                    if m:
                        extracted['delivery_location'] = m.group(1).strip()

            # Dates — pickup_date, delivery_date, valid_until, each resolved
            # against TODAY so relative phrases ("today", "tomorrow", "in 5
            # days", "5 days from now") become real ISO dates, matching what
            # the LLM path (llm_quote.py) does when it's configured.
            from datetime import date as _date, timedelta as _timedelta
            try:
                from dateutil import parser as _date_parser
            except ImportError:
                _date_parser = None
            today = _date.today()

            _MONTH_OR_WEEKDAY_WORDS = {
                'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                'september', 'october', 'november', 'december',
                'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec',
                'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
            }

            def _resolve_date_phrase(phrase: str):
                p = (phrase or '').strip().lower()
                if not p:
                    return None
                if 'today' in p:
                    return today
                if 'tomorrow' in p:
                    return today + _timedelta(days=1)
                if 'yesterday' in p:
                    return today - _timedelta(days=1)
                dm = re.search(r'(\d+)\s*days?\s*(?:from now|later|from today|out)', p)
                if not dm:
                    dm = re.search(r'in\s+(\d+)\s*days?', p)
                if dm:
                    return today + _timedelta(days=int(dm.group(1)))
                # Only hand ambiguous text (e.g. a city name that happened to be
                # captured, like "Durban") to dateutil's fuzzy parser when it
                # actually looks date-shaped — fuzzy mode otherwise silently
                # falls back to `default` (today) for plain non-date text,
                # which would misfire as a false date match.
                has_digit = bool(re.search(r'\d', p))
                has_date_word = any(w in p for w in _MONTH_OR_WEEKDAY_WORDS)
                if not _date_parser or not (has_digit or has_date_word):
                    return None
                try:
                    from datetime import datetime as _dt
                    return _date_parser.parse(phrase, fuzzy=True, default=_dt.combine(today, _dt.min.time())).date()
                except (ValueError, OverflowError, TypeError):
                    return None

            def _extract_date_field(labels: str):
                pattern = rf'(?:{labels})(?:\s+date)?\s*(?:is|will be|[:\-])?\s*([A-Za-z0-9][A-Za-z0-9\s]{{1,30}}?)(?:\s*[-,.]|$)'
                # Try every match, not just the first — a label can legitimately
                # appear twice (e.g. "Delivery: Durban" is the location, a later
                # "Delivery will be 5 days from now" is the date); skip whichever
                # match doesn't actually resolve to a date.
                for m in re.finditer(pattern, message, re.IGNORECASE):
                    resolved = _resolve_date_phrase(m.group(1))
                    if resolved:
                        return resolved
                return None

            d = _extract_date_field(r'pickup|collection')
            if d:
                extracted['pickup_date'] = d.isoformat()
            d = _extract_date_field(r'delivery(?:\s+will\s+be)?')
            if d:
                extracted['delivery_date'] = d.isoformat()
            d = _extract_date_field(r'valid(?:ate)?(?:\s+until)?')
            if d:
                extracted['valid_until'] = d.isoformat()

            # Trip type
            if re.search(r'\bone[\s-]way\b|\bsingle\s+trip\b', message, re.IGNORECASE):
                extracted['trip_type'] = 'ONE_WAY'
            elif re.search(r'\bround[\s-]trip\b|\breturn\s+trip\b|\bthere\s+and\s+back\b', message, re.IGNORECASE):
                extracted['trip_type'] = 'ROUND_TRIP'

            # Weight — with unit suffix
            weight_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:ton|t\b|tons|tonne|tonnes|kg|kgs|kilogram)', message, re.IGNORECASE)
            if weight_match:
                val = float(weight_match.group(1))
                unit = weight_match.group(0).lower()
                if 'kg' in unit:
                    extracted['weight'] = val
                else:
                    extracted['weight'] = val * 1000  # convert tons to kg
            elif not current_fields.get('weight'):
                # Bare number fallback — if weight is still missing and user sends just a number, treat as kg
                bare_number_match = re.search(r'^\s*(\d+(?:\.\d+)?)\s*$', message.strip())
                if bare_number_match:
                    val = float(bare_number_match.group(1))
                    # Heuristic: if < 100, likely tons; if >= 100, likely kg
                    extracted['weight'] = val * 1000 if val < 100 else val

            unmatched = {'customer_name': None, 'vehicle_type': None}

            # Vehicle type — only ever set from the fleet's actual configured
            # names (e.g. "Rigid Truck", "Semi-Trailer Truck"). A generic keyword
            # ("flatbed") that doesn't match any real company VehicleType must
            # NOT be silently written in as if it existed — surface it as
            # unmatched instead so the caller can offer to create it.
            matched_vt = None
            for vt in (vehicle_types or []):
                vt_lc = vt.lower()
                significant = [w for w in vt_lc.split() if w not in ('truck', 'vehicle')]
                if vt_lc in msg_lower or any(w in msg_lower for w in significant):
                    matched_vt = vt
                    break
            if matched_vt:
                extracted['vehicle_type'] = matched_vt
            else:
                _VEHICLE_KEYWORDS = [
                    'flatbed', 'tautliner', 'curtainsider', 'refrigerated', 'reefer', 'fridge',
                    'tanker', 'box truck', 'danger load', 'cargo truck', 'rigid truck',
                    'semi-trailer truck', 'semi trailer', 'interlink',
                ]
                for kw in _VEHICLE_KEYWORDS:
                    if kw in msg_lower:
                        unmatched['vehicle_type'] = kw.title()
                        break

            # Client / customer — "client is X", "customer will be X", etc.,
            # fuzzy-matched against this company's real customer records. A name
            # that's mentioned but doesn't match anything real is surfaced as
            # unmatched rather than silently dropped.
            if customers:
                m = re.search(r'(?:client|customer)(?:\s+will\s+be|\s+is)?\s*[:\-]?\s*([A-Za-z][A-Za-z\s]{1,40}?)(?:\s*[-,.]|$)', message, re.IGNORECASE)
                if m:
                    from core.services.llm_quote import _fuzzy_match
                    raw_name = m.group(1).strip()
                    names = [c['name'] for c in customers]
                    matched_name = _fuzzy_match(raw_name, names)
                    if matched_name:
                        match = next(c for c in customers if c['name'] == matched_name)
                        extracted['customer_id'] = match['id']
                        extracted['customer_name'] = matched_name
                    elif raw_name:
                        unmatched['customer_name'] = raw_name

            # Cargo description — the noun AFTER "of" (e.g. "20 tons of steel from JHB"
            # -> "steel"; "of palletised goods to ..." -> "palletised goods").
            cargo_match = (
                re.search(r'\bof\s+([a-zA-Z][a-zA-Z\s]*?)\s+(?:from|to|on|for|by|via)\b', message, re.IGNORECASE)
                or re.search(r'\bof\s+([a-zA-Z][a-zA-Z\s]*?)\s*[,.]', message, re.IGNORECASE)
                or re.search(r'\bof\s+([a-zA-Z][a-zA-Z\s]*?)$', message.strip(), re.IGNORECASE)
            )
            if cargo_match:
                desc = cargo_match.group(1).strip()
                # Drop a leading unit word if it slipped in ("tonnes of frozen fish").
                desc = re.sub(r'^(tons?|tonnes?|kgs?|kilograms?|pallets?|units?|loads?|crates?)\s+',
                              '', desc, flags=re.IGNORECASE).strip()
                stop = {'move', 'transport', 'ship', 'send', 'deliver', 'take', 'need', 'i'}
                if len(desc) > 2 and desc.lower() not in stop:
                    extracted['cargo_description'] = desc

            if company is not None:
                hit = quote_entity_chat.detect_unmatched(unmatched, declined_entities)
                if hit:
                    table, raw_name = hit
                    pending, ask_reply, link = quote_entity_chat.start_pending(
                        table, raw_name, request.user, detected_language=detected_language)
                    return Response({
                        'success': True,
                        'reply': ask_reply,
                        'extracted_fields': extracted,
                        'pending_entity': pending,
                        'link': link,
                        'declined_entity': None,
                    })

            # Merge with current fields
            merged = {**current_fields, **extracted}

            # Answer a greeting / "how can you help" instead of nagging for fields;
            # otherwise report progress on the still-missing essentials.
            reply = self._reply_for(message, merged, extracted, lang=detected_language)

            return Response({
                'success': True,
                'reply': reply,
                'extracted_fields': extracted,
                'pending_entity': None,
                'link': None,
                'declined_entity': None,
            })

        except Exception as e:
            error_reply = ("I had trouble understanding that. Can you describe the load again? "
                            "For example: '20 tons of pallets from Johannesburg to Cape Town, flatbed.'")
            try:
                from core.services import language_detect
                error_reply = language_detect.translate_template(
                    error_reply, request.data.get('detected_language') or None)
            except Exception:
                pass
            return Response({
                'success': False,
                'reply': error_reply,
                'extracted_fields': {},
                'pending_entity': None,
                'link': None,
                'declined_entity': None,
            })


_LANGUAGE_CONFIDENCE_MARGIN = 0.10


def _mean_avg_logprob(transcript):
    """Mean per-segment avg_logprob from a verbose_json transcription response
    — Whisper's own token-decoding confidence signal, used to arbitrate
    between two forced-language transcriptions of the SAME audio (the hosted
    API exposes no language-confidence score directly). None when there are
    no scorable segments (e.g. silence) — a scoreless pass always loses the
    comparison rather than crashing on an empty average."""
    segments = getattr(transcript, 'segments', None) or []
    scores = [s.avg_logprob for s in segments if getattr(s, 'avg_logprob', None) is not None]
    if not scores:
        return None
    return sum(scores) / len(scores)


class AIVoiceQuoteView(APIView):
    """POST /api/v1/ai/voice-quote/ — transcribe audio and return text."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Accepts audio file, returns transcription.
        Uses OpenAI Whisper if available, otherwise returns error.
        """
        try:
            audio_file = request.FILES.get('audio')
            if not audio_file:
                return Response({'success': False, 'error': 'No audio file provided'}, status=400)

            import os
            openai_key = os.environ.get('OPENAI_API_KEY')
            if not openai_key:
                return Response({
                    'success': False,
                    'error': 'Voice transcription not configured (no OpenAI key)',
                }, status=503)

            # Read the upload up front so we can reject empty/too-short clips with a
            # clear message instead of letting Whisper 400 (a common cause: the mic
            # button was tapped and released before any audio was captured).
            audio_bytes = audio_file.read()
            if not audio_bytes:
                return Response({
                    'success': False,
                    'error': 'Recording was empty — hold the mic, speak, then stop.',
                }, status=400)

            import openai
            client = openai.OpenAI(api_key=openai_key)
            # Whisper keys off the filename's extension, not the multipart
            # content-type, to detect the format — so the name passed here must
            # match what the bytes actually are. Mobile sends real M4A/AAC;
            # Safari's MediaRecorder falls back to MP4/AAC (it doesn't support
            # audio/webm); only Chrome-family browsers actually send WebM. A
            # filename hardcoded to "recording.webm" made every non-WebM
            # upload undecodable to Whisper regardless of the bytes being
            # perfectly valid audio — derive the extension from the upload's
            # own (reliable, browser/RN-set) content-type instead.
            content_type = (getattr(audio_file, 'content_type', '') or '').lower()
            ext = {
                'audio/webm': 'webm',
                'audio/mp4': 'mp4',
                'audio/m4a': 'm4a',
                'audio/x-m4a': 'm4a',
                'audio/mpeg': 'mp3',
                'audio/mp3': 'mp3',
                'audio/wav': 'wav',
                'audio/wave': 'wav',
                'audio/x-wav': 'wav',
                'audio/ogg': 'ogg',
                'audio/flac': 'flac',
            }.get(content_type, 'webm')
            # Fully automatic, no picker — but scoped to a CLOSED pair of
            # candidates (English, Afrikaans) rather than Whisper's own
            # open-ended auto-detect. Open-ended detection is what caused
            # repeated real-world failures (clear English speech confidently
            # mis-identified as Bengali, with no confidence score from the
            # hosted API to catch it) — by only ever forcing the audio through
            # these two known-plausible languages and comparing decode
            # confidence, an unrelated third language can never win by
            # mistake, which is the actual failure mode this closes.
            def _transcribe(language):
                return client.audio.transcriptions.create(
                    model='whisper-1',
                    file=(f'recording.{ext}', audio_bytes, content_type or 'audio/webm'),
                    language=language, response_format='verbose_json',
                )

            try:
                en_transcript = _transcribe('en')
            except openai.OpenAIError as oe:
                # Whisper rejected the audio (too short, undecodable format, etc.).
                # Surface its message and log the details for diagnosis.
                msg = getattr(oe, 'message', None) or str(oe)
                logger.warning(
                    'Whisper rejected audio (%d bytes, upload content_type=%s): %s',
                    len(audio_bytes), getattr(audio_file, 'content_type', None), msg,
                )
                return Response({
                    'success': False,
                    'error': f'Could not transcribe the recording: {msg}',
                }, status=502)

            text, detected_language = en_transcript.text, 'en'
            try:
                af_transcript = _transcribe('af')
                en_score = _mean_avg_logprob(en_transcript)
                af_score = _mean_avg_logprob(af_transcript)
                # Afrikaans must clearly beat English (not just any amount)
                # to be trusted — English wins every tie/near-tie, the safer
                # default between exactly these two known options.
                if af_score is not None and (en_score is None or af_score >= en_score + _LANGUAGE_CONFIDENCE_MARGIN):
                    text, detected_language = af_transcript.text, 'af'
            except openai.OpenAIError as oe:
                # Best-effort second pass — if it fails, the English result
                # already in hand is a perfectly good answer on its own.
                logger.warning('Whisper Afrikaans comparison pass failed: %s', oe)

            return Response({
                'success': True,
                'text': text,
                'detected_language': detected_language,
            })

        except Exception as e:
            logger.exception('Voice transcription failed')
            return Response({
                'success': False,
                'error': str(e),
            }, status=500)


# ============================================================================
# Sprint 1: Quote Feedback Loop & Enhanced Features
# ============================================================================

class QuoteOutcomeView(APIView):
    """PATCH /api/v1/quotes/{id}/outcome/ — Mark quote as accepted or rejected."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, quote_id):
        """
        Mark a quote outcome for ML training.
        Body: {
            "outcome": "accepted" | "rejected",
            "rejection_reason": "optional text",
            "final_price": optional decimal
        }
        """
        try:
            quote = Quote.objects.get(id=quote_id, company=request.user.company)
        except Quote.DoesNotExist:
            return Response({
                'success': False,
                'error': 'Quote not found'
            }, status=status.HTTP_404_NOT_FOUND)

        outcome = request.data.get('outcome')
        if outcome not in ['accepted', 'rejected']:
            return Response({
                'success': False,
                'error': 'outcome must be "accepted" or "rejected"'
            }, status=status.HTTP_400_BAD_REQUEST)

        rejection_reason = request.data.get('rejection_reason', '')
        final_price = request.data.get('final_price')

        # One shared capture path (also used by the public accept link and
        # status updates) — updates the quote fields, upserts the single
        # QuoteOutcome row per quote, and snapshots the ML features.
        # allow_flip: this is the deliberate operator-correction path, so it
        # may overwrite an existing opposite label.
        from core.services.quote_outcome_capture import record_quote_outcome
        record = record_quote_outcome(
            quote, outcome,
            rejection_reason=rejection_reason, final_price=final_price,
            allow_flip=True,
        )
        if record is None:
            return Response({
                'success': False,
                'error': 'Failed to record outcome',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'success': True,
            'id': quote.id,
            'outcome': quote.outcome,
            'updated_at': quote.updated_at.isoformat(),
        })


class QuoteModelStatsView(APIView):
    """GET /api/v1/quotes/model-stats/ — Returns ML model training statistics."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Return current state of the quoting model."""
        try:
            from core.views import resolve_user_company
            company = resolve_user_company(request.user)
            # Tenant-scoped: never expose other operators' outcome volume.
            real_quotes_count = QuoteOutcome.objects.filter(
                outcome__in=['accepted', 'rejected'],
                quote__company=company,
            ).count()

            # Report ONLY what is real. A model exists when its metadata file has
            # been written by a successful train(); otherwise we are honestly untrained.
            from core.services.quote_ml import QuoteMLModel, ML_AVAILABLE

            metadata = {}
            try:
                model = QuoteMLModel()
                metadata = getattr(model, 'metadata', {}) or {}
            except Exception:
                metadata = {}

            # QuoteMLModel.train() writes 'training_date' + 'sample_count' + 'metrics'
            last_trained = metadata.get('training_date') or metadata.get('trained_at')
            trained = bool(last_trained)
            metrics = metadata.get('metrics', {}) if isinstance(metadata, dict) else {}

            # Win-probability model status — this is the one that drives the
            # profit sweet-spot curve, and it learns on the installed sklearn stack.
            try:
                from core.services.quote_training import win_model_status
                win = win_model_status(company=company)
            except Exception:
                win = None

            return Response({
                'success': True,
                'ml_available': bool(ML_AVAILABLE),
                'trained': trained,
                'real_quotes_count': real_quotes_count,
                'training_sample_count': metadata.get('sample_count'),
                'last_trained': last_trained,
                'accuracy_r2': metrics.get('r2'),
                'metrics': metrics or None,
                'model_version': metadata.get('version'),
                'win_model': win,
                'message': (
                    'Model trained.' if trained
                    else ('ML libraries not installed — quoting model unavailable.'
                          if not ML_AVAILABLE
                          else 'Model not yet trained — run the retrain command.')
                ),
            })
        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class FuelPriceSurchargeCheckView(APIView):
    """POST /api/v1/fuel-prices/surcharge-check/ — Calculate recommended fuel surcharge."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Given a quote ID, calculate fuel delta and recommended surcharge.
        Body: { "quote_id": 123 }
        """
        try:
            quote_id = request.data.get('quote_id')
            if not quote_id:
                return Response({
                    'success': False,
                    'error': 'quote_id is required'
                }, status=status.HTTP_400_BAD_REQUEST)

            quote = Quote.objects.get(id=quote_id, company=request.user.company)

            # Get current fuel price
            try:
                current_fuel = fetch_fuel_prices()
                fuel_current = float(current_fuel.diesel_inland)
            except Exception:
                fuel_current = 20.0  # fallback

            fuel_at_creation = float(quote.fuel_price_at_creation) if quote.fuel_price_at_creation else fuel_current

            if fuel_at_creation == 0:
                delta_pct = 0
                delta_zar = 0
            else:
                delta_pct = ((fuel_current - fuel_at_creation) / fuel_at_creation) * 100
                delta_zar = fuel_current - fuel_at_creation

            surcharge_required = delta_pct > 3.0

            # Calculate recommended surcharge
            # Formula: delta_pct × original fuel_surcharge
            original_fuel_cost = float(quote.fuel_surcharge) if quote.fuel_surcharge else 0
            if surcharge_required and original_fuel_cost > 0:
                recommended_surcharge_zar = original_fuel_cost * (delta_pct / 100)
            else:
                recommended_surcharge_zar = 0

            distance = float(quote.distance) if quote.distance else 0
            fuel_impact_message = f"Diesel price increase costs ~R{int(recommended_surcharge_zar)} more for this {int(distance)} km job" if surcharge_required else "No significant fuel price change"

            return Response({
                'success': True,
                'fuel_at_creation': fuel_at_creation,
                'fuel_current': fuel_current,
                'delta_pct': round(delta_pct, 2),
                'delta_zar': round(delta_zar, 2),
                'surcharge_required': surcharge_required,
                'recommended_surcharge_zar': round(recommended_surcharge_zar, 2),
                'fuel_impact_on_total': fuel_impact_message,
            })

        except Quote.DoesNotExist:
            return Response({
                'success': False,
                'error': 'Quote not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class QuoteFuelAlertView(APIView):
    """GET /api/v1/quotes/{id}/fuel-alert/ — Returns fuel price alert for a quote."""
    permission_classes = [IsAuthenticated]

    def get(self, request, quote_id):
        """Check if fuel price has changed significantly since quote creation."""
        try:
            quote = Quote.objects.get(id=quote_id, company=request.user.company)

            # Get current fuel price
            try:
                current_fuel = fetch_fuel_prices()
                fuel_current = float(current_fuel.diesel_inland)
            except Exception:
                fuel_current = 20.0

            fuel_at_creation = float(quote.fuel_price_at_creation) if quote.fuel_price_at_creation else fuel_current

            if fuel_at_creation == 0:
                return Response({
                    'success': True,
                    'has_alert': False,
                })

            delta_pct = ((fuel_current - fuel_at_creation) / fuel_at_creation) * 100
            delta_zar = fuel_current - fuel_at_creation

            has_alert = abs(delta_pct) > 3.0

            if not has_alert:
                return Response({
                    'success': True,
                    'has_alert': False,
                })

            # Calculate cost impact
            distance = float(quote.distance) if quote.distance else 0
            original_fuel_cost = float(quote.fuel_surcharge) if quote.fuel_surcharge else 0
            estimated_cost_impact = int(original_fuel_cost * (abs(delta_pct) / 100))

            alert_type = 'FUEL_INCREASE' if delta_zar > 0 else 'FUEL_DECREASE'
            message = f"Diesel {'up' if delta_zar > 0 else 'down'} R{abs(delta_zar):.2f}/L since this quote was created. This job now costs ~R{estimated_cost_impact} {'more' if delta_zar > 0 else 'less'}."
            action = 'Consider requesting a surcharge adjustment' if delta_zar > 0 else 'You may have extra margin to offer a discount'

            return Response({
                'success': True,
                'has_alert': True,
                'alert_type': alert_type,
                'fuel_delta_pct': round(delta_pct, 2),
                'fuel_delta_zar': round(delta_zar, 2),
                'estimated_cost_impact': estimated_cost_impact,
                'message': message,
                'action': action,
            })

        except Quote.DoesNotExist:
            return Response({
                'success': False,
                'error': 'Quote not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class QuoteBenchmarkView(APIView):
    """GET /api/v1/quotes/benchmark/ — Market benchmark for a lane + vehicle type."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Query params: origin, destination, vehicle_type
        Returns market average, range, and recommendation.
        """
        try:
            from core.services.lane_benchmark import (
                compute_lane_benchmark, canon_code, lookup_sa_estimate, _lane_q,
            )
            origin = canon_code(request.query_params.get('origin', ''))
            destination = canon_code(request.query_params.get('destination', ''))
            vehicle_type = request.query_params.get('vehicle_type', '').lower()

            if not origin or not destination or not vehicle_type:
                return Response({
                    'success': False,
                    'error': 'origin, destination, and vehicle_type are required'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Cross-platform anonymized benchmark first (pools won quotes across
            # ALL operators, k-anonymity enforced so no single operator's pricing
            # is exposed). Falls back to own-company data, then hardcoded estimates.
            platform = compute_lane_benchmark(origin, destination, vehicle_type)
            if not platform.get('available'):
                # Retry at lane level (all vehicle types) before falling back.
                platform = compute_lane_benchmark(origin, destination)

            # Query this operator's own accepted quotes on this lane (fallback
            # layer). _lane_q matches historical alias spellings (DUR/DURBAN)
            # against the canonical query code.
            from core.views import resolve_user_company
            lane_quotes = Quote.objects.filter(
                _lane_q('origin', origin),
                _lane_q('destination', destination),
                company=resolve_user_company(request.user),
                vehicle_type__icontains=vehicle_type,
                outcome='accepted',
                created_at__gte=timezone.now() - timedelta(days=90)
            )

            data_points = lane_quotes.count()
            source = 'company'
            distinct_operators = None

            # Hardcoded SA market averages live in lane_benchmark (single source).
            sa_estimate = lookup_sa_estimate(origin, destination, vehicle_type)

            if platform.get('available'):
                # Real cross-platform benchmark (preferred)
                market_avg_rate = round(platform['market_avg_rate'])
                market_range_low = round(platform.get('p25') or platform['market_avg_rate'])
                market_range_high = round(platform.get('p75') or platform['market_avg_rate'])
                data_points = platform['sample_size']
                distinct_operators = platform.get('distinct_operators')
                confidence = 'high'
                source = 'platform'
            elif data_points >= 10:
                # Use this operator's own real data
                stats = lane_quotes.aggregate(
                    avg_price=Avg('total_amount'),
                    min_price=Min('total_amount'),
                    max_price=Max('total_amount'),
                )
                market_avg_rate = int(stats['avg_price'] or 0)
                market_range_low = int(stats['min_price'] or 0)
                market_range_high = int(stats['max_price'] or 0)
                confidence = 'high'
                source = 'company'
            elif sa_estimate:
                # Fallback to hardcoded
                market_avg_rate = sa_estimate['avg']
                market_range_low = sa_estimate['low']
                market_range_high = sa_estimate['high']
                confidence = 'medium' if data_points >= 5 else 'low'
                source = 'estimate'
            else:
                # No data available
                return Response({
                    'success': True,
                    'origin': origin,
                    'destination': destination,
                    'vehicle_type': vehicle_type,
                    'market_avg_rate': None,
                    'data_points': 0,
                    'confidence': 'none',
                    'recommendation': 'Market data not available for this lane yet.',
                })

            # Calculate recommendation (mock for now)
            your_rate = request.query_params.get('your_rate', market_avg_rate)
            your_rate = float(your_rate)
            your_vs_market_pct = ((your_rate - market_avg_rate) / market_avg_rate) * 100 if market_avg_rate > 0 else 0

            if your_vs_market_pct < -10:
                recommendation = f"Your quote is {abs(your_vs_market_pct):.0f}% below market. Consider R{int(market_avg_rate * 0.9)}-R{int(market_avg_rate)} for better margin."
            elif your_vs_market_pct > 10:
                recommendation = f"Your quote is {your_vs_market_pct:.0f}% above market. May be difficult to win at this price."
            else:
                recommendation = "Your price is competitive and within market range."

            return Response({
                'success': True,
                'origin': origin,
                'destination': destination,
                'vehicle_type': vehicle_type,
                'market_avg_rate': market_avg_rate,
                'market_range_low': market_range_low,
                'market_range_high': market_range_high,
                'data_points': data_points,
                'confidence': confidence,
                'source': source,
                'distinct_operators': distinct_operators,
                'your_rate': your_rate,
                'your_vs_market_pct': round(your_vs_market_pct, 1),
                'recommendation': recommendation,
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class QuoteWinProbabilityView(APIView):
    """POST /api/v1/quotes/win-probability/ — Predict win probability for a quote."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        Given quote parameters, predict win probability.
        Body: {
            "price": 42000,
            "distance": 1580,
            "vehicle_type": "interlink",
            "client_id": 42,
            "origin": "JHB",
            "destination": "CPT",
            "days_until_departure": 2
        }
        """
        try:
            from core.services.quote_ml import WinProbabilityModel

            price = float(request.data.get('price', 0))
            distance = float(request.data.get('distance', 0))
            client_id = request.data.get('client_id')
            days_until_departure = int(request.data.get('days_until_departure', 2))

            if not price or not client_id:
                return Response({
                    'success': False,
                    'error': 'price and client_id are required'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Get market rate for benchmark (simplified)
            market_rate = 43800  # Default JHB-CPT interlink
            price_ratio = price / market_rate if market_rate > 0 else 1.0

            # Get client historical acceptance rate
            try:
                customer = Customer.objects.get(id=client_id, company=request.user.company)
                accepted_count = Quote.objects.filter(
                    customer=customer,
                    outcome='accepted'
                ).count()
                total_count = Quote.objects.filter(
                    customer=customer,
                    outcome__in=['accepted', 'rejected']
                ).count()
                historical_acceptance_rate = accepted_count / total_count if total_count > 0 else 0.7

                # Determine client tier
                if total_count >= 10:
                    client_tier = 2  # VIP
                elif total_count >= 3:
                    client_tier = 1  # Regular
                else:
                    client_tier = 0  # New
            except Exception:
                historical_acceptance_rate = 0.7
                client_tier = 0

            # Calculate route popularity
            route_popularity = 0.5  # Default

            # Predict win probability
            win_model = WinProbabilityModel()
            win_probability = win_model.predict_proba(
                price_ratio=price_ratio,
                client_tier=client_tier,
                days_until_departure=days_until_departure,
                historical_acceptance_rate=historical_acceptance_rate,
                month=timezone.now().month,
                day_of_week=timezone.now().weekday(),
                route_popularity=route_popularity,
            )

            # Calculate win probability at ±5%
            price_lower = price * 0.95
            price_higher = price * 1.05

            win_probability_lower = win_model.predict_proba(
                price_ratio=price_lower / market_rate,
                client_tier=client_tier,
                days_until_departure=days_until_departure,
                historical_acceptance_rate=historical_acceptance_rate,
                month=timezone.now().month,
                day_of_week=timezone.now().weekday(),
                route_popularity=route_popularity,
            )

            win_probability_higher = win_model.predict_proba(
                price_ratio=price_higher / market_rate,
                client_tier=client_tier,
                days_until_departure=days_until_departure,
                historical_acceptance_rate=historical_acceptance_rate,
                month=timezone.now().month,
                day_of_week=timezone.now().weekday(),
                route_popularity=route_popularity,
            )

            return Response({
                'success': True,
                'win_probability': round(win_probability, 2),
                'win_probability_lower': round(win_probability_lower, 2),
                'win_probability_higher': round(win_probability_higher, 2),
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
