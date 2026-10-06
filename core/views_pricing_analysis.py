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
