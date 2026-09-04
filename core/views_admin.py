"""Platform-wide admin dashboard — superuser only (IsSuperUser, core/views.py),
completely separate from a company's own ADMIN role. Cross-tenant visibility
plus write actions for company lifecycle, billing, user accounts, support
search, and platform job/integration health. Every state-changing view here
calls AuditLog.log_action (core/models/audit_log.py, an existing generic
audit model — not reinvented) so "who did what, when" is always answerable.

Impersonation ("log in as") was explicitly dropped from scope.
"""
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import AuditLog, Company, User, Quote, Load, TaskRunLog
from core.services.demo_seed import IDLE_RESET_AFTER, reset_demo_company
from core.views import IsSuperUser


def _log(request, action, resource_type, resource_id, **details):
    AuditLog.log_action(
        action=action, resource_type=resource_type, resource_id=resource_id,
        user=request.user, details=details or {},
    )


class AdminOverviewView(APIView):
    """Platform snapshot: company counts by subscription status, total
    users/quotes/loads (all-time and this month), and an MRR estimate."""
    permission_classes = [IsSuperUser]

    def get(self, request):
        real_companies = Company.objects.filter(is_demo=False, is_deleted=False)
        by_status = dict(
            real_companies.values('subscription_status')
            .annotate(n=Count('id'))
            .values_list('subscription_status', 'n')
        )

        from core.services.paystack import MONTHLY_FEE
        active_count = by_status.get('active', 0) + by_status.get('grace_period', 0)
        mrr_estimate = float(MONTHLY_FEE) * active_count

        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        return Response({
            'companies_by_status': {
                'active': by_status.get('active', 0),
                'trialing': by_status.get('trialing', 0),
                'grace_period': by_status.get('grace_period', 0),
                'suspended': by_status.get('suspended', 0),
                'cancelled': by_status.get('cancelled', 0),
                'none': by_status.get('none', 0),
            },
            'total_companies': real_companies.count(),
            'has_demo_company': Company.objects.filter(is_demo=True).exists(),
            'total_users': User.objects.filter(company__is_demo=False, company__is_deleted=False).count(),
            'total_quotes': Quote.objects.filter(company__is_demo=False, company__is_deleted=False).count(),
            'quotes_this_month': Quote.objects.filter(company__is_demo=False, company__is_deleted=False, created_at__gte=month_start).count(),
            'total_loads': Load.objects.filter(company__is_demo=False, company__is_deleted=False).count(),
            'loads_this_month': Load.objects.filter(company__is_demo=False, company__is_deleted=False, created_at__gte=month_start).count(),
            # Estimate only — active/grace_period count x flat fee. Doesn't
            # reconcile against actual Paystack charges or the 0.25%
            # delivery take-rate.
            'mrr_estimate': mrr_estimate,
        })


class AdminCompaniesView(APIView):
    """Every company on the platform (including the demo one, flagged via
    is_demo), searchable by name. Read-only."""
    permission_classes = [IsSuperUser]

    def get(self, request):
        qs = Company.objects.annotate(
            user_count=Count('users', distinct=True),
            quote_count=Count('quotes', distinct=True),
            load_count=Count('loads', distinct=True),
        ).order_by('-created_at')

        if not request.query_params.get('include_deleted'):
            qs = qs.filter(is_deleted=False)

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(company_name__icontains=search)

        status_filter = request.query_params.get('status', '').strip()
        if status_filter:
            qs = qs.filter(subscription_status__in=status_filter.split(','))

        results = [{
            'id': c.id,
            'company_name': c.company_name,
            'subscription_status': c.subscription_status,
            'is_demo': c.is_demo,
            'is_deleted': c.is_deleted,
            'created_at': c.created_at,
            'next_billing_date': c.next_billing_date,
            'grace_period_expires_at': c.grace_period_expires_at,
            'user_count': c.user_count,
            'quote_count': c.quote_count,
            'load_count': c.load_count,
        } for c in qs[:200]]

        return Response({'count': qs.count(), 'results': results})


class AdminUsersView(APIView):
    """Every user on the platform, searchable by name/email/company. Read-only."""
    permission_classes = [IsSuperUser]

    def get(self, request):
        qs = User.objects.select_related('company').order_by('-date_joined')

        # AdminUserActionView's 'delete' action soft-deletes the same way
        # DeleteAccountView (core/views.py) already does for self-service —
        # there's no is_deleted field on User, the email/username get a
        # deleted-<tag>+ prefix instead. Hide those by default, same as
        # AdminCompaniesView hides is_deleted=True companies.
        if not request.query_params.get('include_deleted'):
            qs = qs.exclude(email__startswith='deleted-')

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(email__icontains=search) | Q(username__icontains=search) |
                Q(first_name__icontains=search) | Q(last_name__icontains=search) |
                Q(company__company_name__icontains=search)
            )

        status_filter = request.query_params.get('status', '').strip()
        if status_filter == 'active':
            qs = qs.filter(is_active=True)
        elif status_filter == 'inactive':
            qs = qs.filter(is_active=False)

        results = [{
            'id': u.id,
            'name': f'{u.first_name} {u.last_name}'.strip() or u.username,
            'email': u.email,
            'company_id': u.company_id,
            'company_name': u.company.company_name if u.company_id else None,
            'role': u.role,
            'is_active': u.is_active,
            'is_superuser': u.is_superuser,
            'is_deleted': u.email.startswith('deleted-'),
            'last_login': u.last_login,
        } for u in qs[:200]]

        return Response({'count': qs.count(), 'results': results})


class AdminDemoStatusView(APIView):
    """Live state of the shared public demo company, plus a manual reset."""
    permission_classes = [IsSuperUser]

    def get(self, request):
        company = Company.objects.filter(is_demo=True).first()
        if not company:
            return Response({'exists': False})

        from django.db.models import Max
        latest = Quote.objects.filter(company=company).aggregate(m=Max('updated_at'))['m']
        latest_load = Load.objects.filter(company=company).aggregate(m=Max('updated_at'))['m']
        if latest_load and (not latest or latest_load > latest):
            latest = latest_load

        idle_eligible = bool(
            latest and (not company.demo_last_reset_at or latest > company.demo_last_reset_at)
            and timezone.now() - latest >= IDLE_RESET_AFTER
        )

        return Response({
            'exists': True,
            'company_id': company.id,
            'demo_quota_used': company.demo_quota_used,
            'demo_last_reset_at': company.demo_last_reset_at,
            'last_activity_at': latest,
            'idle_eligible_for_auto_reset': idle_eligible,
        })

    def post(self, request):
        """Force a reset right now, bypassing the idle wait — an explicit
        manual action, not the automatic hourly-idle check."""
        summary = reset_demo_company()
        _log(request, 'OTHER', 'Company', summary['company'].pk, admin_action='reset_demo_company')
        return Response({
            'reset': True,
            'company_id': summary['company'].pk,
            'vehicles': summary['vehicles'],
            'drivers': summary['drivers'],
            'customers': summary['customers'],
            'quotes': summary['quotes'],
            'loads': summary['loads'],
        })


class AdminCompanyActionView(APIView):
    """POST {action: 'suspend'|'reactivate'|'delete'} — company lifecycle
    controls. 'reactivate' is the "unlock a company" action: unlike the
    automated record_charge_success (core/services/subscription_billing.py),
    which deliberately only un-graces an active/grace_period company, this
    reaches suspended/cancelled too — that's the whole point of a manual
    override."""
    permission_classes = [IsSuperUser]

    def post(self, request, company_id):
        action = request.data.get('action')
        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            return Response({'error': 'Company not found'}, status=status.HTTP_404_NOT_FOUND)

        if action == 'suspend':
            company.subscription_status = 'suspended'
            company.save(update_fields=['subscription_status', 'updated_at'])
        elif action == 'reactivate':
            company.subscription_status = 'active'
            company.grace_period_expires_at = None
            company.save(update_fields=['subscription_status', 'grace_period_expires_at', 'updated_at'])
        elif action == 'delete':
            company.is_deleted = True
            company.deleted_at = timezone.now()
            company.save(update_fields=['is_deleted', 'deleted_at', 'updated_at'])
        else:
            return Response({'error': f'Unknown action "{action}"'}, status=status.HTTP_400_BAD_REQUEST)

        _log(request, 'UPDATE', 'Company', company.pk, admin_action=action)
        return Response({'id': company.id, 'subscription_status': company.subscription_status, 'is_deleted': company.is_deleted})


class AdminCompanyBillingView(APIView):
    """Full charge history for one company (admin side of BillingHistoryView,
    core/views_billing.py — same merge logic, parameterized by company id
    instead of request.user.company), plus a direct next_billing_date edit."""
    permission_classes = [IsSuperUser]

    def get(self, request, company_id):
        from core.models import DeliveryFeeCharge
        from core.services.paystack import MONTHLY_FEE_ITEM_NAME
        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            return Response({'error': 'Company not found'}, status=status.HTTP_404_NOT_FOUND)

        transactions = company.billing_transactions.all()
        results = [{
            'id': f'sub-{t.id}', 'kind': 'subscription', 'label': MONTHLY_FEE_ITEM_NAME,
            'amount': str(t.amount), 'status': t.status,
            'reference': t.gateway_transaction_id or t.payment_id, 'created_at': t.created_at,
        } for t in transactions]

        charges = DeliveryFeeCharge.objects.filter(company=company).select_related('invoice')
        results += [{
            'id': f'dfc-{c.id}', 'kind': 'delivery_fee',
            'label': f'Delivery fee · {c.invoice.invoice_number}',
            'amount': str(c.amount),
            # DeliveryFeeCharge/BillingTransaction use different status
            # vocabularies (charged/failed vs complete/failed) — normalise,
            # same as BillingHistoryView (core/views_billing.py) does.
            'status': 'complete' if c.status == 'charged' else c.status,
            'reference': c.invoice.invoice_number, 'created_at': c.created_at,
        } for c in charges]

        results.sort(key=lambda r: r['created_at'], reverse=True)
        return Response({'company_id': company.id, 'results': results})

    def patch(self, request, company_id):
        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            return Response({'error': 'Company not found'}, status=status.HTTP_404_NOT_FOUND)

        next_billing_date = request.data.get('next_billing_date')
        if not next_billing_date:
            return Response({'error': 'next_billing_date is required'}, status=status.HTTP_400_BAD_REQUEST)
        company.next_billing_date = next_billing_date
        company.save(update_fields=['next_billing_date', 'updated_at'])
        _log(request, 'UPDATE', 'Company', company.pk, admin_action='adjust_billing_date', next_billing_date=str(next_billing_date))
        return Response({'id': company.id, 'next_billing_date': company.next_billing_date})


class AdminRecordPaymentView(APIView):
    """Manually record a payment that happened outside Paystack (bank
    transfer, EFT, etc.) — creates a real BillingTransaction, advances the
    billing cycle the same way an automated charge does, and unconditionally
    reactivates the company (unlike record_charge_success, which only
    un-graces an already-active/grace_period company). amount=0 doubles as
    "waive this charge" with a note, so a separate waive endpoint isn't
    needed."""
    permission_classes = [IsSuperUser]

    def post(self, request, company_id):
        from core.models import BillingTransaction
        from core.services.notify import notify_company, notify_company_billing_email
        from core.services.subscription_billing import compute_next_cycle
        from core.services.paystack import MONTHLY_FEE_ITEM_NAME

        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            return Response({'error': 'Company not found'}, status=status.HTTP_404_NOT_FOUND)

        amount = request.data.get('amount')
        note = (request.data.get('note') or '').strip()
        if amount is None:
            return Response({'error': 'amount is required (0 to record a waiver)'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return Response({'error': 'amount must be a number'}, status=status.HTTP_400_BAD_REQUEST)

        txn = BillingTransaction.objects.create(
            company=company, amount=amount, status='complete', plan='pro',
            payment_id=f'MANUAL-{request.user.id}-{int(timezone.now().timestamp())}',
            payment_status='manual',
            raw_gateway_response={'manual': True, 'note': note, 'recorded_by': request.user.email, 'amount': amount},
        )
        company.next_billing_date, company.next_billing_at = compute_next_cycle(company.next_billing_date)
        company.subscription_status = 'active'
        company.grace_period_expires_at = None
        company.save(update_fields=['next_billing_date', 'next_billing_at', 'subscription_status', 'grace_period_expires_at', 'updated_at'])

        title = 'Payment recorded'
        message = f'{MONTHLY_FEE_ITEM_NAME}: R{amount:,.2f} recorded manually.' + (f' Note: {note}' if note else '')
        notify_company(company.id, 'SUCCESS', title, message, link='/settings/billing', event='subscription.charged')
        notify_company_billing_email(company.id, title, message, link='/settings/billing')

        _log(request, 'UPDATE', 'Company', company.pk, admin_action='record_payment', amount=amount, note=note, transaction_id=txn.id)
        return Response({
            'transaction_id': txn.id, 'company_id': company.id,
            'subscription_status': company.subscription_status, 'next_billing_date': company.next_billing_date,
        })


def _send_password_reset_code(user):
    """Same mechanism PasswordResetRequestView (core/views.py) already uses
    for the self-service "Forgot password?" flow — reused verbatim so an
    admin-triggered reset (new account, or an existing user who's stuck)
    behaves identically to one the user requested themselves."""
    import secrets
    from django.core.cache import cache
    from core.tasks import send_password_reset_email_task

    code = str(secrets.randbelow(900000) + 100000)
    cache.set(f'pwd_reset_{user.email}', code, timeout=3600)
    send_password_reset_email_task(user.email, user.first_name or user.username, code)


class AdminCreateUserView(APIView):
    """Creates a bare account — no company. On first login the existing
    onboarding wizard (Onboarding.tsx, triggered by postLoginNavigate when
    company.onboarding_completed_at is empty) takes over: the new user sets
    up their own company themselves via resolve_user_company()'s existing
    auto-create-empty-company path. Nothing new needed on that side."""
    permission_classes = [IsSuperUser]

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        first_name = (request.data.get('first_name') or '').strip()
        last_name = (request.data.get('last_name') or '').strip()
        if not email:
            return Response({'error': 'email is required'}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email__iexact=email).exists():
            return Response({'error': 'An account with this email already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.create_user(
            username=email, email=email, first_name=first_name, last_name=last_name,
            role='ADMIN', company=None, is_active=True,
        )
        user.set_unusable_password()
        user.save(update_fields=['password'])
        _send_password_reset_code(user)

        _log(request, 'CREATE', 'User', user.pk, email=email)
        return Response({'id': user.id, 'email': user.email}, status=status.HTTP_201_CREATED)


class AdminUserActionView(APIView):
    """POST {action: 'lock'|'unlock'|'reset_password'|'delete'}. 'delete'
    mirrors DeleteAccountView's own soft-delete (core/views.py) — User has
    CASCADE relations (Driver, UserSession, Copilot data, IntegrationAPIKey,
    Webhook, InviteToken) a hard delete would destroy, so this deactivates
    and mangles the email/username instead (freeing the address for
    re-signup), same as a user deleting their own account would."""
    permission_classes = [IsSuperUser]

    def post(self, request, user_id):
        action = request.data.get('action')
        try:
            target = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        if action == 'lock':
            target.is_active = False
            target.save(update_fields=['is_active'])
        elif action == 'unlock':
            target.is_active = True
            target.save(update_fields=['is_active'])
        elif action == 'reset_password':
            _send_password_reset_code(target)
        elif action == 'delete':
            import uuid

            if target.is_superuser:
                return Response({'error': "Can't delete a superuser account from here."}, status=status.HTTP_400_BAD_REQUEST)
            original_email = target.email
            tag = uuid.uuid4().hex[:12]
            target.is_active = False
            target.status = 'INACTIVE'
            target.email = f'deleted-{tag}+{target.email}'
            target.username = f'deleted-{tag}-{target.username}'[:150]
            target.save(update_fields=['is_active', 'status', 'email', 'username'])
            target.sessions.all().delete()
            _log(request, 'DELETE', 'User', target.pk, admin_action='delete', original_email=original_email)
            return Response({'id': target.id, 'is_active': target.is_active, 'is_deleted': True})
        else:
            return Response({'error': f'Unknown action "{action}"'}, status=status.HTTP_400_BAD_REQUEST)

        _log(request, 'UPDATE', 'User', target.pk, admin_action=action)
        return Response({'id': target.id, 'is_active': target.is_active})


class AdminUserRoleView(APIView):
    """PATCH {role} — cross-tenant role change."""
    permission_classes = [IsSuperUser]

    def patch(self, request, user_id):
        role = (request.data.get('role') or '').strip().upper()
        valid_roles = {c[0] for c in User.ROLE_CHOICES}
        if role not in valid_roles:
            return Response({'error': f'"{role}" is not a valid role.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            target = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        old_role = target.role
        target.role = role
        target.save(update_fields=['role'])
        _log(request, 'UPDATE', 'User', target.pk, admin_action='change_role', old_role=old_role, new_role=role)
        return Response({'id': target.id, 'role': target.role})


class AdminSearchView(APIView):
    """GET ?q=... — find a quote/order by number across every tenant, with
    its company, for support tickets ("customer says quote #X isn't
    working")."""
    permission_classes = [IsSuperUser]

    def get(self, request):
        q = request.query_params.get('q', '').strip()
        if not q:
            return Response({'quotes': [], 'loads': []})

        quotes = Quote.objects.filter(quote_number__icontains=q).select_related('company')[:20]
        loads = Load.objects.filter(load_number__icontains=q).select_related('company')[:20]

        return Response({
            'quotes': [{
                'id': qt.id, 'quote_number': qt.quote_number, 'status': qt.status,
                'company_id': qt.company_id, 'company_name': qt.company.company_name if qt.company_id else None,
            } for qt in quotes],
            'loads': [{
                'id': ld.id, 'load_number': ld.load_number, 'status': ld.status,
                'company_id': ld.company_id, 'company_name': ld.company.company_name if ld.company_id else None,
            } for ld in loads],
        })


class AdminJobHealthView(APIView):
    """Latest TaskRunLog row per task_name — is Celery beat actually
    running each nightly sweep, and did it succeed. See
    core/services/task_run.py for what "succeed" can and can't detect."""
    permission_classes = [IsSuperUser]

    TRACKED_TASKS = [
        'reset_demo_company_task', 'refresh_fuel_price', 'run_monthly_subscription_billing',
        'check_grace_period_expirations', 'check_pending_cancellations',
        'retry_delivery_fee_charges', 'retrain_win_model',
    ]

    def get(self, request):
        results = []
        for task_name in self.TRACKED_TASKS:
            row = TaskRunLog.objects.filter(task_name=task_name).order_by('-started_at').first()
            results.append({
                'task_name': task_name,
                'last_started_at': row.started_at if row else None,
                'last_finished_at': row.finished_at if row else None,
                'last_success': row.success if row else None,
                'last_error': row.error if row else '',
            })
        return Response({'results': results})


class AdminIntegrationsHealthView(APIView):
    """Which companies have Xero/CtrlFleet actually connected — reuses the
    connection-timestamp fields already on Company (core/models/company.py),
    no new tracking needed."""
    permission_classes = [IsSuperUser]

    def get(self, request):
        base = Company.objects.filter(is_demo=False, is_deleted=False)
        xero = base.filter(xero_connected_at__isnull=False)
        ctrlfleet = base.filter(ctrlfleet_connected_at__isnull=False)
        return Response({
            'xero_connected_count': xero.count(),
            'xero_connected_companies': list(xero.values('id', 'company_name', 'xero_connected_at')),
            'ctrlfleet_connected_count': ctrlfleet.count(),
            'ctrlfleet_connected_companies': list(ctrlfleet.values('id', 'company_name', 'ctrlfleet_connected_at')),
        })


class AdminAuditLogView(APIView):
    """Recent admin-dashboard actions (every write view above logs here via
    _log()) — proxied as "superuser-authored AuditLog entries" since AuditLog
    itself is a shared, generic model also used elsewhere in the codebase."""
    permission_classes = [IsSuperUser]

    def get(self, request):
        rows = AuditLog.objects.filter(user__is_superuser=True).select_related('user').order_by('-created_at')[:100]
        return Response({'results': [{
            'id': r.id,
            'actor': r.user.email if r.user else None,
            'action': r.action,
            'resource_type': r.resource_type,
            'resource_id': r.resource_id,
            'details': r.details,
            'created_at': r.created_at,
        } for r in rows]})
