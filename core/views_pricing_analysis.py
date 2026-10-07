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

        if not isinstance(request.data, dict):
            return Response({'success': False, 'code': 'invalid_input',
                             'message': 'Expected a JSON object of quote fields.'},
                            status=status.HTTP_400_BAD_REQUEST)
        data = request.data
        company = resolve_user_company(request.user)
        try:
            return Response(analyze_pricing(data, company=company, user=request.user))
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            # The service coerces most fields defensively (core.services.
            # pricing_analysis._f/_i), but an unusual shape (e.g. a list or
            # object where a scalar was expected) can still slip past that and
            # raise here. That's a bad-input problem, not a server fault — 400
            # with a plain message, not a 500 that looks like an outage.
            logger.info('pricing analysis: rejected malformed input: %s', exc)
            return Response({'success': False, 'code': 'invalid_input',
                             'message': 'Some of the quote details could not be read. Please check the form and try again.'},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception('pricing analysis failed')
            return Response({'success': False, 'code': 'failed',
                             'message': 'Pricing analysis is unavailable right now. Please try again.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
