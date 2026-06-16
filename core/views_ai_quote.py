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


class FuelPriceCurrentView(APIView):
    """GET /api/v1/fuel-prices/current/ — returns current diesel price with staleness check."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            fuel_price = fetch_fuel_prices()

            # Check staleness: if fuel price is >7 days old
            days_old = (timezone.now().date() - fuel_price.date).days
            is_stale = days_old > 7
            stale_warning = None
            if is_stale:
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
            model = QuoteMLModel()
            if not model.is_trained():
                return Response({
                    'success': False,
                    'error': 'AI model training — try again later',
                }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            data = request.data
            actual_cost = float(data.get('actual_cost', 0))
            if actual_cost <= 0:
                return Response({
                    'success': False,
                    'error': 'actual_cost must be > 0',
                }, status=status.HTTP_400_BAD_REQUEST)

            # Build feature dict — fill in defaults for missing features
            features = {
                'route_id': int(data.get('route_id', 0)),
                'distance_km': float(data.get('distance_km', 0)),
                'truck_type': int(data.get('truck_type', 0)),
                'load_type': int(data.get('load_type', 0)),
                'load_weight': float(data.get('load_weight', 0)),
                'fuel_price': float(data.get('fuel_price', 22.0)),
                'toll_cost': float(data.get('toll_cost', 0)),
                'driver_cost': float(data.get('driver_cost', 0)),
                'client_tier': int(data.get('client_tier', 1)),
                'historical_acceptance_rate': float(data.get('historical_acceptance_rate', 0.7)),
                'day_of_week': int(date.today().weekday()),
                'month': int(date.today().month),
                'is_holiday': int(data.get('is_holiday', 0)),
                'is_return_load': int(data.get('is_return_load', 0)),
                'competitor_quote': float(data.get('competitor_quote', 0)),
                'urgency': int(data.get('urgency', 1)),
                'route_popularity': float(data.get('route_popularity', 0.5)),
                'weather_risk': float(data.get('weather_risk', 0)),
                'historical_margin_avg': float(data.get('historical_margin_avg', 0.18)),
                'fleet_utilization': float(data.get('fleet_utilization', 0.75)),
                'deadhead_prob': float(data.get('deadhead_prob', 0.3)),
                'load_value_zar': float(data.get('load_value_zar', 0)),
            }

            prediction = model.predict_optimal_margin(features, actual_cost, top_n=5)
            if not prediction:
                return Response({
                    'success': False,
                    'error': 'Prediction failed',
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Calculate win probability (Sprint 1)
            try:
                from core.services.quote_ml import WinProbabilityModel

                suggested_price = prediction.recommended_price
                market_rate = 43800  # Default market rate (JHB-CPT interlink)
                price_ratio = suggested_price / market_rate if market_rate > 0 else 1.0

                client_tier = int(data.get('client_tier', 1))
                days_until_departure = int(data.get('days_until_departure', 2))

                win_model = WinProbabilityModel()
                win_probability = win_model.predict_proba(
                    price_ratio=price_ratio,
                    client_tier=client_tier,
                    days_until_departure=days_until_departure,
                    historical_acceptance_rate=features['historical_acceptance_rate'],
                    month=features['month'],
                    day_of_week=features['day_of_week'],
                    route_popularity=features['route_popularity'],
                )

                # Calculate win probability at ±5%
                win_probability_at_lower_price = win_model.predict_proba(
                    price_ratio=(suggested_price * 0.95) / market_rate,
                    client_tier=client_tier,
                    days_until_departure=days_until_departure,
                    historical_acceptance_rate=features['historical_acceptance_rate'],
                    month=features['month'],
                    day_of_week=features['day_of_week'],
                    route_popularity=features['route_popularity'],
                )

                win_probability_at_higher_price = win_model.predict_proba(
                    price_ratio=(suggested_price * 1.05) / market_rate,
                    client_tier=client_tier,
                    days_until_departure=days_until_departure,
                    historical_acceptance_rate=features['historical_acceptance_rate'],
                    month=features['month'],
                    day_of_week=features['day_of_week'],
                    route_popularity=features['route_popularity'],
                )
            except Exception as win_err:
                # If win probability fails, continue without it
                win_probability = None
                win_probability_at_lower_price = None
                win_probability_at_higher_price = None

            response_data = {
                'success': True,
                'suggested_price': prediction.recommended_price,
                'margin_pct': prediction.predicted_margin_pct * 100,
                'confidence': prediction.confidence,
                'margin_range': {
                    'lower': prediction.margin_lower * 100,
                    'upper': prediction.margin_upper * 100,
                },
                'top_features': prediction.feature_importances,
            }

            # Add win probability fields if available
            if win_probability is not None:
                response_data['win_probability'] = round(win_probability, 2)
                response_data['win_probability_at_lower_price'] = round(win_probability_at_lower_price, 2)
                response_data['win_probability_at_higher_price'] = round(win_probability_at_higher_price, 2)

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

            margin = (quote_price - total_cost) / quote_price
            margin_pct = margin * 100

            # Initialize explanations and suggestions
            explanations = []
            suggestions = []

            # Risk thresholds
            if margin_pct < 5:
                risk_level = 'AT_RISK'
                color = 'danger'
                explanations.append(f"Margin is below 8% safety threshold ({margin_pct:.1f}%)")
            elif margin_pct < 12:
                risk_level = 'CAUTION'
                color = 'warning'
                explanations.append(f"Margin is below 12% — limited buffer for unexpected costs ({margin_pct:.1f}%)")
            else:
                risk_level = 'SAFE'
                color = 'success'
                explanations.append(f"Margin is healthy at {margin_pct:.1f}%")

            # Enhanced analysis if quote_id provided
            if quote_id:
                try:
                    quote = Quote.objects.get(id=quote_id, company=request.user.company)

                    # Fuel risk analysis
                    if quote.fuel_price_at_creation:
                        current_fuel = fetch_fuel_prices()
                        fuel_current = float(current_fuel.diesel_inland)
                        fuel_at_creation = float(quote.fuel_price_at_creation)
                        if fuel_at_creation > 0:
                            delta_pct = ((fuel_current - fuel_at_creation) / fuel_at_creation) * 100
                            if delta_pct > 3:
                                delta_zar = fuel_current - fuel_at_creation
                                explanations.append(f"Fuel cost has increased R{delta_zar:.2f}/L since your last quote on this route")
                                surcharge = int(fuel_cost * (delta_pct / 100))
                                suggestions.append(f"Add a fuel surcharge of R{surcharge} to restore margin to 12%")

                    # Client payment history risk
                    if quote.customer:
                        late_invoices = Invoice.objects.filter(
                            customer=quote.customer,
                            status='paid',
                            actual_payment_date__gt=F('due_date')
                        ).count()
                        total_invoices = Invoice.objects.filter(
                            customer=quote.customer,
                            status='paid'
                        ).count()

                        if late_invoices > 2 and total_invoices > 0:
                            explanations.append(f"This client has paid late on {late_invoices} of last {total_invoices} invoices")
                            suggestions.append("Require 50% upfront deposit given client payment history")

                    # CPK analysis
                    if distance_km > 0:
                        cpk = total_cost / distance_km
                        # Get fleet average (mock for now)
                        fleet_avg_cpk = 19.80
                        if cpk > fleet_avg_cpk * 1.1:
                            explanations.append(f"Your CPK on this route is R{cpk:.2f} — above fleet average of R{fleet_avg_cpk:.2f}")
                            suggestions.append("Review your cost model — this route may need a base rate increase")

                except Quote.DoesNotExist:
                    pass
                except Exception:
                    pass

            # Price adjustment suggestion
            if margin_pct < 5:
                increase_needed = total_cost * 0.10 / (1 - 0.10) - quote_price
                suggestions.append(f"Current margin is {margin_pct:.1f}%. Consider increasing price by R{int(increase_needed)} to reach 10% margin")

            # Margin floor calculation
            margin_floor = int(total_cost)
            margin_floor_display = f"R{margin_floor:,}"

            return Response({
                'success': True,
                'status': risk_level,
                'margin_pct': round(margin_pct, 2),
                'factors': [],  # Legacy field
                'explanations': explanations,
                'suggestions': suggestions,
                'margin_floor': margin_floor,
                'margin_floor_display': margin_floor_display,
                # Legacy fields
                'risk_level': risk_level,
                'color': color,
                'warnings': explanations if risk_level != 'SAFE' else [],
            })

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

            # Cargo description
            cargo_match = re.search(r'(?:of\s+)?([a-zA-Z\s]+?)\s+(?:from|to\s+\w)', message, re.IGNORECASE)
            if cargo_match:
                desc = cargo_match.group(1).strip()
                if len(desc) > 3 and desc.lower() not in ['move', 'transport', 'ship', 'send', 'deliver', 'take']:
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

            # Try OpenAI Whisper
            import os
            openai_key = os.environ.get('OPENAI_API_KEY')
            if openai_key:
                import openai
                client = openai.OpenAI(api_key=openai_key)
                transcript = client.audio.transcriptions.create(
                    model='whisper-1',
                    file=audio_file,
                )
                return Response({
                    'success': True,
                    'text': transcript.text,
                })
            else:
                return Response({
                    'success': False,
                    'error': 'Voice transcription not configured (no OpenAI key)',
                }, status=503)

        except Exception as e:
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
            except:
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
            except:
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
                customer = Customer.objects.get(id=client_id)
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
            except:
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
