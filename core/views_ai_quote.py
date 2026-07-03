"""
AI Quote & Revenue Guard API endpoints for Phase 2 desktop quoting.
Sprint 1: AI Quoting Engine Upgrade with feedback loop, fuel alerts, win probability.
"""

import logging
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
    """GET /api/v1/fuel-prices/current/ — returns current diesel price with staleness check."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            fuel_price = fetch_fuel_prices()

            # Stale if: source is a fallback (live scrape failed), or data is >35 days old
            days_old = (timezone.now().date() - fuel_price.date).days
            is_fallback = fuel_price.source in ('FALLBACK', 'FALLBACK_LATEST')
            is_stale = is_fallback or days_old > 35
            stale_warning = None
            if is_stale:
                if is_fallback:
                    stale_warning = "Live price fetch failed — showing estimated price. Update via Admin > Fuel Prices."
                else:
                    stale_warning = f"Last update {days_old} days ago; consider manual refresh"

            return Response({
                'success': True,
                'inland_price': float(fuel_price.diesel_inland),
                'coastal_price': float(fuel_price.diesel_coastal),
                'last_updated': fuel_price.date.isoformat(),
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
            from core.services.margin_optimizer import optimize_price
            opt = optimize_price(
                total_cost=actual_cost, market_rate=market_rate,
                client_tier=client_tier, days_until_departure=days_until,
                historical_acceptance_rate=hist,
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

                def _pw(price):
                    ratio = (price / market_rate) if market_rate > 0 else 1.0
                    return round(float(win_model.predict_proba(
                        price_ratio=ratio, client_tier=client_tier,
                        days_until_departure=days_until, historical_acceptance_rate=hist,
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

            company = getattr(request.user, 'company', None)
            quote = None
            if quote_id:
                try:
                    quote = Quote.objects.get(id=quote_id, company=company)
                except Quote.DoesNotExist:
                    quote = None

            from core.services.quote_analysis import assess_revenue_guard
            result = assess_revenue_guard(
                total_cost=total_cost, quote_price=quote_price,
                distance_km=distance_km, fuel_cost=fuel_cost,
                company=company, quote=quote,
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

    def post(self, request):
        try:
            data = request.data
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
                'client_tier': data.get('client_tier', 'standard'),
                'days_until_departure': data.get('days_until_departure', 7),
            }
            company = getattr(request.user, 'company', None)
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

    @staticmethod
    def _fallback_reply(merged):
        """Build a friendly reply from the fields captured so far."""
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
            return (
                f"Got it — {merged.get('cargo_description', 'your cargo')} from "
                f"{merged.get('pickup_location')} to {merged.get('delivery_location')}, "
                f"{merged.get('weight', 0) / 1000:.0f} tons. Ready to calculate your quote."
            )
        if len(missing) <= 2:
            return f"Almost there. Just need the {' and '.join(missing)} to complete the quote."
        return f"Thanks! I still need the {', '.join(missing[:-1])} and {missing[-1]} to build your quote."

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

            # Primary path: Claude-backed natural-language extraction.
            # Falls through to the regex extractor below when the LLM is
            # not configured or the call fails, so the endpoint never breaks.
            from core.services import llm_quote
            if llm_quote.is_enabled():
                try:
                    extracted, reply = llm_quote.extract(message, history, current_fields)
                    merged = {**current_fields, **extracted}
                    if not reply:
                        reply = self._fallback_reply(merged)
                    return Response({
                        'success': True,
                        'reply': reply,
                        'extracted_fields': extracted,
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

            # Vehicle type
            vehicle_map = {
                'flatbed': 'Flatbed', 'tautliner': 'Tautliner', 'curtainsider': 'Tautliner',
                'refrigerated': 'Refrigerated', 'reefer': 'Refrigerated', 'fridge': 'Refrigerated',
                'tanker': 'Tanker', 'box truck': 'Box Truck', 'danger': 'Danger Load', 'dg': 'Danger Load',
            }
            for key, val in vehicle_map.items():
                if key in msg_lower:
                    extracted['vehicle_type'] = val
                    break

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

            # Merge with current fields
            merged = {**current_fields, **extracted}

            # Build reply
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
                reply = f"Got it — {merged.get('cargo_description', 'your cargo')} from {merged.get('pickup_location')} to {merged.get('delivery_location')}, {merged.get('weight', 0)/1000:.0f} tons. Ready to calculate your quote."
            elif len(missing) <= 2:
                reply = f"Almost there. Just need the {' and '.join(missing)} to complete the quote."
            else:
                reply = f"Thanks! I still need the {', '.join(missing[:-1])} and {missing[-1]} to build your quote."

            return Response({
                'success': True,
                'reply': reply,
                'extracted_fields': extracted,
            })

        except Exception as e:
            return Response({
                'success': False,
                'reply': "I had trouble understanding that. Can you describe the load again? For example: '20 tons of pallets from Johannesburg to Cape Town, flatbed.'",
                'extracted_fields': {},
            })


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
            # Explicit (name, bytes, content_type) tuple so Whisper detects the
            # format from the extension. The frontend always sends a webm blob.
            try:
                transcript = client.audio.transcriptions.create(
                    model='whisper-1',
                    file=('recording.webm', audio_bytes, 'audio/webm'),
                )
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

            return Response({
                'success': True,
                'text': transcript.text,
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

        # Update quote fields
        quote.outcome = outcome
        quote.rejection_reason = rejection_reason if outcome == 'rejected' else ''

        if outcome == 'accepted':
            quote.accepted_at = timezone.now()
            quote.rejected_at = None
        else:
            quote.rejected_at = timezone.now()
            quote.accepted_at = None

        quote.save()

        # Create QuoteOutcome record for ML training
        final_price_val = Decimal(final_price) if final_price else quote.total_amount
        margin_pct = ((final_price_val - (quote.base_rate + quote.fuel_surcharge + quote.toll_charges + quote.driver_allowance + quote.additional_charges)) / final_price_val * 100) if final_price_val > 0 else Decimal('0')

        # Determine client tier based on quote history
        client_tier = 'new'
        if quote.customer:
            customer_quote_count = Quote.objects.filter(customer=quote.customer, outcome='accepted').count()
            if customer_quote_count >= 10:
                client_tier = 'vip'
            elif customer_quote_count >= 3:
                client_tier = 'regular'

        QuoteOutcome.objects.create(
            quote=quote,
            outcome=outcome,
            rejection_reason=rejection_reason if outcome == 'rejected' else '',
            final_price=final_price_val,
            margin_pct=margin_pct,
            distance_km=quote.distance,
            vehicle_type=quote.vehicle_type,
            origin=quote.origin,
            destination=quote.destination,
            weight_kg=quote.weight,
            client_tier=client_tier,
            fuel_price=quote.fuel_price_at_creation,
        )

        # Close the ML flywheel: a fresh outcome may be enough to (re)train the
        # win-probability model. Fire-and-forget so it never delays the response.
        try:
            from core.services.quote_training import maybe_retrain_win_model_async
            maybe_retrain_win_model_async()
        except Exception:
            pass

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
            real_quotes_count = QuoteOutcome.objects.filter(
                outcome__in=['accepted', 'rejected']
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
                win = win_model_status()
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
            origin = request.query_params.get('origin', '').upper()
            destination = request.query_params.get('destination', '').upper()
            vehicle_type = request.query_params.get('vehicle_type', '').lower()

            if not origin or not destination or not vehicle_type:
                return Response({
                    'success': False,
                    'error': 'origin, destination, and vehicle_type are required'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Cross-platform anonymized benchmark first (pools won quotes across
            # ALL operators, k-anonymity enforced so no single operator's pricing
            # is exposed). Falls back to own-company data, then hardcoded estimates.
            from core.services.lane_benchmark import compute_lane_benchmark
            platform = compute_lane_benchmark(origin, destination, vehicle_type)
            if not platform.get('available'):
                # Retry at lane level (all vehicle types) before falling back.
                platform = compute_lane_benchmark(origin, destination)

            # Query this operator's own accepted quotes on this lane (fallback layer)
            lane_quotes = Quote.objects.filter(
                company=request.user.company,
                origin__iexact=origin,
                destination__iexact=destination,
                vehicle_type__icontains=vehicle_type,
                outcome='accepted',
                created_at__gte=timezone.now() - timedelta(days=90)
            )

            data_points = lane_quotes.count()
            source = 'company'
            distinct_operators = None

            # Fallback to hardcoded SA market averages
            SA_MARKET_BENCHMARKS = {
                ('JHB', 'CPT', 'interlink'): {'avg': 43800, 'low': 38000, 'high': 52000},
                ('JHB', 'DBN', 'interlink'): {'avg': 17000, 'low': 14000, 'high': 20000},
                ('CPT', 'DBN', 'interlink'): {'avg': 52000, 'low': 45000, 'high': 90000},
                ('JHB', 'CPT', 'truck'): {'avg': 38900, 'low': 34000, 'high': 46000},
                ('JHB', 'DBN', 'truck'): {'avg': 15000, 'low': 12000, 'high': 18000},
            }

            lane_key = (origin, destination, vehicle_type)

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
            elif lane_key in SA_MARKET_BENCHMARKS:
                # Fallback to hardcoded
                benchmark = SA_MARKET_BENCHMARKS[lane_key]
                market_avg_rate = benchmark['avg']
                market_range_low = benchmark['low']
                market_range_high = benchmark['high']
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
