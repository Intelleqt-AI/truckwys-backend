"""AI insights endpoints: LLM-backed executive briefing + explainable risk."""
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle

from core.services.llm_insights import executive_briefing, explain_risk_score
from core.services.agent import agent_respond, generate_conversation_title

logger = logging.getLogger(__name__)


class AgentChatView(APIView):
    """POST /api/v1/agent/chat/ — conversational fleet-finance copilot.

    Body: { "messages": [{"role": "user"|"assistant", "content": str}, ...] }
    Returns: { reply, source, ai_available, actions: [{label, route}] }
    Grounded in the company's live data; read-only (suggests, never mutates).
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'copilot'

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
            # Pass the acting user so build_agent_context strips the snapshot to
            # the caller's role — otherwise a DRIVER/VIEWER could read banking,
            # driver PII and customer contacts they're denied everywhere else.
            result = agent_respond(company, messages, user=request.user)
        except Exception:
            logger.exception('agent chat failed')
            return Response({'error': 'The copilot is unavailable right now.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
                    conv.title = generate_conversation_title(str(last_user))
            reply = result.get('reply')
            if reply:
                CopilotMessage.objects.create(user=request.user, conversation=conv, role='assistant', content=str(reply)[:8000])
            conv.save()  # bump updated_at + title
            result['conversation_id'] = conv.id
        except Exception:
            pass

        return Response(result)


class ConversationChatView(APIView):
    """POST /api/v1/agent/conversations/<id>/chat/ — send one message to a specific
    conversation and get a RAG-grounded reply.

    Each conversation has its own endpoint. The server owns the history (loaded from
    the DB), so the client only sends the new message. Strictly account-scoped:
    the conversation must belong to request.user and retrieval is scoped to their
    company.

    Body: { "message": str }  (also accepts { "content": str })
    Returns: { reply, source, ai_available, actions, proposed_action, conversation_id }
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'copilot'

    def post(self, request, pk):
        from core.views import resolve_user_company
        from core.models import CopilotConversation, CopilotMessage

        company = resolve_user_company(request.user)
        if company is None:
            return Response(
                {'error': 'No company associated with this account'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        conv = CopilotConversation.objects.filter(id=pk, user=request.user).first()
        if conv is None:
            return Response({'error': 'Conversation not found'}, status=status.HTTP_404_NOT_FOUND)

        text = request.data.get('message') or request.data.get('content')
        if not text or not str(text).strip():
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)
        text = str(text).strip()

        # Server-side history: the 30 MOST RECENT turns (guided data entry spans
        # many short turns), oldest-first for the model. Slice newest in SQL then
        # reverse — the old `order_by('created_at')[:300][-30:]` took the OLDEST
        # 300 and kept 271-300, so >300-message threads froze at stale context.
        history = [
            {'role': m.role, 'content': m.content}
            for m in reversed(list(conv.messages.order_by('-created_at')[:30]))
        ]
        history.append({'role': 'user', 'content': text})

        # Keep this account's invoice index reasonably fresh, but NOT on every
        # message: loading all invoices+embeddings per turn is O(N). Throttle to
        # once per 5 min per company via the shared cache (the Celery beat task
        # reindex_copilot_rag is the backstop when this path is quiet).
        try:
            from django.core.cache import cache
            from core.services import rag
            _rag_key = f'rag_indexed_{company.id}'
            if not cache.get(_rag_key):
                rag.index_company_invoices(company)
                cache.set(_rag_key, 1, 300)
        except Exception:
            pass

        try:
            # enable_tools=True lets the agent query the database and prepare
            # confirm-first create/update/delete proposals from chat.
            result = agent_respond(
                company, history, query=text, user=request.user,
                enable_tools=True, conversation=conv,
            )
        except Exception:
            logger.exception('conversation chat failed')
            return Response({'error': 'The copilot is unavailable right now.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Persist the turn.
        try:
            CopilotMessage.objects.create(user=request.user, conversation=conv, role='user', content=text[:8000])
            if not conv.title:
                conv.title = generate_conversation_title(text)
            reply = result.get('reply')
            if reply:
                metadata = {}
                if result.get('proposal'):
                    metadata['proposal_id'] = result['proposal']['id']
                CopilotMessage.objects.create(
                    user=request.user, conversation=conv, role='assistant',
                    content=str(reply)[:8000], metadata=metadata,
                )
            conv.save()  # bump updated_at + title
        except Exception:
            pass

        result['conversation_id'] = conv.id
        return Response(result)


class CopilotConversationsView(APIView):
    """GET/POST /api/v1/agent/conversations/ — list past threads or start a new one."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import CopilotConversation
        from django.db.models import Count
        # Only surface threads that actually have messages — an unused "New chat"
        # (or one whose first message failed) is never shown in history.
        convs = (CopilotConversation.objects.filter(user=request.user)
                 .annotate(n=Count('messages')).filter(n__gt=0).order_by('-updated_at')[:50])
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
        from core.models import CopilotConversation, CopilotProposal
        from core.services.copilot_tools import proposal_public
        conv = CopilotConversation.objects.filter(id=pk, user=request.user).first()
        if not conv:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        msgs = list(conv.messages.order_by('created_at')[:300])

        # Rehydrate proposal cards with their LIVE status (pending/executed/…).
        proposal_ids = [m.metadata.get('proposal_id') for m in msgs
                        if isinstance(m.metadata, dict) and m.metadata.get('proposal_id')]
        proposals = {p.id: proposal_public(p)
                     for p in CopilotProposal.objects.filter(id__in=proposal_ids, user=request.user)}

        out = []
        for m in msgs:
            row = {'role': m.role, 'content': m.content, 'created_at': m.created_at.isoformat()}
            meta = m.metadata if isinstance(m.metadata, dict) else {}
            pid = meta.get('proposal_id')
            if pid and pid in proposals and not meta.get('is_outcome'):
                row['proposal'] = proposals[pid]
            if meta.get('action'):
                row['actions'] = [meta['action']]
            out.append(row)
        return Response({'id': conv.id, 'title': conv.title, 'messages': out})

    def delete(self, request, pk):
        from core.models import CopilotConversation
        CopilotConversation.objects.filter(id=pk, user=request.user).delete()
        return Response({'deleted': True})


class ProposalExecuteView(APIView):
    """POST /api/v1/agent/proposals/<id>/execute/ — perform a confirmed copilot write.

    The proposal id is the whole contract: the client never supplies an endpoint
    or payload. Owner + company scoped; RBAC is re-checked inside execute_proposal.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from core.views import resolve_user_company
        from core.models import CopilotProposal, CopilotMessage
        from core.services.copilot_tools import execute_proposal

        company = resolve_user_company(request.user)
        proposal = CopilotProposal.objects.filter(
            id=pk, user=request.user, company=company
        ).first()
        if proposal is None:
            return Response({'error': 'Proposal not found'}, status=status.HTTP_404_NOT_FOUND)
        if proposal.status != 'PENDING':
            # proposal_status lets the client show the true state (e.g. a second
            # tab confirming a card that already executed must not render "Failed").
            return Response(
                {'error': f'This proposal was already {proposal.status.lower()}',
                 'proposal_status': proposal.status.lower()},
                status=status.HTTP_409_CONFLICT,
            )

        ok, payload = execute_proposal(proposal, request.user, company)
        if not ok:
            proposal.refresh_from_db()
            return Response(
                {'status': 'failed', 'proposal_status': proposal.status.lower(), **payload},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Record the outcome in the thread so the LLM's history replay knows the
        # write actually happened (and reload shows it).
        try:
            if proposal.conversation_id:
                metadata = {'proposal_id': proposal.id, 'is_outcome': True}
                # Persist the nav chip (e.g. "Open Quote QT-…") so it survives a
                # reload — the HTTP response alone doesn't outlive this request.
                if payload.get('action'):
                    metadata['action'] = payload['action']
                CopilotMessage.objects.create(
                    user=request.user, conversation=proposal.conversation,
                    role='assistant', content=payload['message'],
                    # is_outcome: history replay still sees the write happened, but
                    # the detail GET won't inline a second card for the same proposal.
                    metadata=metadata,
                )
                proposal.conversation.save()  # bump updated_at
        except Exception:
            pass

        return Response({'status': 'executed', **payload})


class ProposalDismissView(APIView):
    """POST /api/v1/agent/proposals/<id>/dismiss/ — decline a pending proposal."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from core.views import resolve_user_company
        from core.models import CopilotProposal

        company = resolve_user_company(request.user)
        proposal = CopilotProposal.objects.filter(
            id=pk, user=request.user, company=company
        ).first()
        if proposal is None:
            return Response({'error': 'Proposal not found'}, status=status.HTTP_404_NOT_FOUND)
        if proposal.status == 'PENDING':
            proposal.status = 'DISMISSED'
            proposal.save(update_fields=['status'])
        return Response({'status': 'dismissed'})


class DashboardBriefingView(APIView):
    """GET /api/v1/dashboard/briefing/ — an AI executive briefing over the
    company's live finance/cash/insights data (Claude when configured, else a
    deterministic summary)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.views import resolve_user_company
        from datetime import datetime, date
        company = resolve_user_company(request.user)

        # Reporting window from the Insights filter (?from=&to=, YYYY-MM-DD).
        # Defaults to month-to-date, matching the finance dashboard contract.
        def _parse(s):
            try:
                return datetime.strptime(s, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                return None

        to_date = _parse(request.query_params.get('to')) or date.today()
        from_date = _parse(request.query_params.get('from')) or to_date.replace(day=1)
        try:
            return Response(executive_briefing(company, from_date, to_date))
        except Exception:
            logger.exception('dashboard briefing failed')
            return Response({'error': 'The briefing is unavailable right now.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RiskScoreExplainView(APIView):
    """GET /api/v1/risk/score/<id>/explain/ — plain-English underwriting rationale."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from core.models import RiskScore
        from core.views import resolve_user_company
        qs = RiskScore.objects.all()
        if not getattr(request.user, 'is_superuser', False):
            # Always tenant-scope for non-superusers. A user with no company
            # (legacy/seed accounts have company_id=NULL) must see NOTHING —
            # never the unscoped queryset, which would leak other tenants' scores.
            company = resolve_user_company(request.user)
            if company is None:
                return Response({'error': 'Risk score not found'}, status=status.HTTP_404_NOT_FOUND)
            qs = qs.filter(invoice__company=company)
        score = qs.filter(pk=pk).first()
        if not score:
            return Response({'error': 'Risk score not found'}, status=status.HTTP_404_NOT_FOUND)
        try:
            return Response(explain_risk_score(score))
        except Exception:
            logger.exception('risk score explain failed')
            return Response({'error': 'The explanation is unavailable right now.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
