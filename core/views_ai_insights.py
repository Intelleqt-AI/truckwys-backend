"""AI insights endpoints: LLM-backed executive briefing + explainable risk."""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from core.services.llm_insights import executive_briefing, explain_risk_score


class DashboardBriefingView(APIView):
    """GET /api/v1/dashboard/briefing/ — an AI executive briefing over the
    company's live finance/cash/insights data (Claude when configured, else a
    deterministic summary)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        try:
            return Response(executive_briefing(company))
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RiskScoreExplainView(APIView):
    """GET /api/v1/risk/score/<id>/explain/ — plain-English underwriting rationale."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from core.models import RiskScore
        qs = RiskScore.objects.all()
        company = getattr(request.user, 'company', None)
        if company and not getattr(request.user, 'is_superuser', False):
            qs = qs.filter(invoice__company=company)
        score = qs.filter(pk=pk).first()
        if not score:
            return Response({'error': 'Risk score not found'}, status=status.HTTP_404_NOT_FOUND)
        try:
            return Response(explain_risk_score(score))
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
