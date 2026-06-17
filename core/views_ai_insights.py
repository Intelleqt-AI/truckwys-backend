"""AI insights endpoints: LLM-backed executive briefing + explainable risk."""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from core.services.llm_insights import executive_briefing, explain_risk_score
from core.services.agent import agent_respond


class AgentChatView(APIView):
    """POST /api/v1/agent/chat/ — conversational fleet-finance copilot.

    Body: { "messages": [{"role": "user"|"assistant", "content": str}, ...] }
    Returns: { reply, source, ai_available, actions: [{label, route}] }
    Grounded in the company's live data; read-only (suggests, never mutates).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from core.views import resolve_user_company
        company = resolve_user_company(request.user)
        if company is None:
            return Response(
                {'error': 'No company associated with this account'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        messages = request.data.get('messages')
        if messages is None:
            # also accept a single { "message": "..." }
            single = request.data.get('message')
            messages = [{'role': 'user', 'content': single}] if single else []
        if not isinstance(messages, list):
            return Response({'error': 'messages must be a list'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = agent_respond(company, messages)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Persist this turn into a conversation (new chats are kept, not destroyed).
        try:
            from core.models import CopilotMessage, CopilotConversation
            conv_id = request.data.get('conversation_id')
            conv = None
            if conv_id:
                conv = CopilotConversation.objects.filter(id=conv_id, user=request.user).first()
            if conv is None:
                conv = (CopilotConversation.objects.filter(user=request.user).first()
                        or CopilotConversation.objects.create(user=request.user))
            last_user = next((m.get('content') for m in reversed(messages)
                              if isinstance(m, dict) and m.get('role') == 'user'), None)
            if last_user:
                CopilotMessage.objects.create(user=request.user, conversation=conv, role='user', content=str(last_user)[:8000])
                if not conv.title:
                    conv.title = str(last_user)[:80]
            reply = result.get('reply')
            if reply:
                CopilotMessage.objects.create(user=request.user, conversation=conv, role='assistant', content=str(reply)[:8000])
            conv.save()  # bump updated_at + title
            result['conversation_id'] = conv.id
        except Exception:
            pass

        return Response(result)


class CopilotConversationsView(APIView):
    """GET/POST /api/v1/agent/conversations/ — list past threads or start a new one."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import CopilotConversation
        from django.db.models import Count
        convs = (CopilotConversation.objects.filter(user=request.user)
                 .annotate(n=Count('messages')).order_by('-updated_at')[:50])
        return Response({'conversations': [
            {'id': c.id, 'title': c.title or 'New conversation',
             'updated_at': c.updated_at.isoformat(), 'message_count': c.n}
            for c in convs
        ]})

    def post(self, request):
        from core.models import CopilotConversation
        conv = CopilotConversation.objects.create(user=request.user)
        return Response({'id': conv.id, 'title': ''}, status=status.HTTP_201_CREATED)


class CopilotConversationDetailView(APIView):
    """GET/DELETE /api/v1/agent/conversations/<id>/ — a thread's messages, or delete it."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from core.models import CopilotConversation
        conv = CopilotConversation.objects.filter(id=pk, user=request.user).first()
        if not conv:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        msgs = conv.messages.order_by('created_at')[:300]
        return Response({
            'id': conv.id, 'title': conv.title,
            'messages': [{'role': m.role, 'content': m.content, 'created_at': m.created_at.isoformat()} for m in msgs],
        })

    def delete(self, request, pk):
        from core.models import CopilotConversation
        CopilotConversation.objects.filter(id=pk, user=request.user).delete()
        return Response({'deleted': True})


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
