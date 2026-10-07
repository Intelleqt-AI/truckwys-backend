"""POST /api/v1/quotes/pricing-analysis/ — the quote builder's pricing
analysis (core.services.pricing_analysis). Deterministic and cheap: the
client calls it debounced as the price or costs change."""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.throttling import PricingAnalysisRateThrottle

logger = logging.getLogger(__name__)


class QuotePricingAnalysisView(APIView):
    permission_classes = [IsAuthenticated]
    # Own bucket: analysis calls must not consume 'user_write' (quote saves).
    throttle_classes = [PricingAnalysisRateThrottle]

    def post(self, request):
        from core.services.pricing_analysis import analyze_pricing
        from core.views import resolve_user_company

        data = request.data if isinstance(request.data, dict) else {}
        company = resolve_user_company(request.user)
        try:
            return Response(analyze_pricing(data, company=company, user=request.user))
        except Exception:
            logger.exception('pricing analysis failed')
            return Response({'success': False, 'code': 'failed',
                             'message': 'Pricing analysis is unavailable right now. Please try again.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class QuoteCostBreakdownView(APIView):
    """POST /api/v1/quotes/cost-breakdown/ — the authoritative cost lines,
    cost floor, target price and warnings (core.services.quote_costing), so
    clients can check their own calculation against the server's.

    Body: the builder's pricing payload (see quote_costing.build_inputs), or
    {"quote_id": N} for a saved quote (adds `send_check` and the stored
    snapshot's figures as `snapshot`)."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [PricingAnalysisRateThrottle]

    def post(self, request):
        from core.models import Quote
        from core.services.quote_costing import costing_for_payload, costing_for_quote
        from core.services.quote_snapshot import send_check
        from core.views import resolve_user_company

        data = request.data if isinstance(request.data, dict) else {}
        company = resolve_user_company(request.user)
        if company is None:
            return Response({'success': False, 'code': 'no_company', 'message': 'No company on this account.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            quote_id = data.get('quote_id')
            if quote_id not in (None, ''):
                quote = Quote.objects.filter(id=quote_id, company=company).first()
                if quote is None:
                    return Response({'success': False, 'code': 'not_found', 'message': 'Quote not found.'},
                                    status=status.HTTP_404_NOT_FOUND)
                out = costing_for_quote(quote)
                out['send_check'] = send_check(quote)
                from core.services.quote_costing import changes_since_priced
                out['changes_since_priced'] = changes_since_priced(
                    quote.total_amount, quote.cost_floor, out['floor'], quote.priced_at)
                from core.services.quote_snapshot import fuel_lines_delta
                out['changes_since_priced']['fuel_delta_zar'] = fuel_lines_delta(quote, costing_now=out)
                out['snapshot'] = {
                    'fuel_price_used': quote.fuel_price_used, 'fuel_price_source': quote.fuel_price_source,
                    'fuel_zone': quote.fuel_zone, 'fuel_effective_from': quote.fuel_effective_from,
                    'fuel_official_at_pricing': quote.fuel_official_at_pricing,
                    'fuel_litres': quote.fuel_litres, 'priced_at': quote.priced_at,
                    'cost_floor': quote.cost_floor, 'empty_return_included': quote.empty_return_included,
                    'priced_vehicle_type': quote.priced_vehicle_type_id,
                }
            else:
                out = costing_for_payload(data, company)
            return Response({'success': True, **out})
        except Exception:
            logger.exception('cost breakdown failed')
            return Response({'success': False, 'code': 'failed',
                             'message': 'The cost breakdown is unavailable right now. Please try again.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
