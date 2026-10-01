"""Who may see and act on which funder's book.

* **Transporters** (tenant users) see only their own company's offers and
  advances. They never see a funder, a debtor score or another tenant.
* **Capital desk staff** (``is_staff``) see every funder's book and run the
  operations: limits, policy proposals (maker), disbursement and settlement.
  They approve only when the funder has delegated that in writing
  (``Funder.staff_may_approve``; the sandbox funder has it).
* **Funder members** (``FunderMembership``) see only their funder. APPROVERs
  approve or decline advances and approve policy versions (checker).
* **Funder API keys** (LENDER ``IntegrationAPIKey`` with ``funder`` set) act as
  an approver for that funder, narrowed to its ``allowed_companies``.

Segregation of duties: whoever approved an advance cannot also disburse it,
and the maker of a policy version cannot approve it.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.exceptions import NotFound, PermissionDenied

STAFF = 'STAFF'
APPROVER = 'APPROVER'
VIEWER = 'VIEWER'


def is_lender(user) -> bool:
    return type(user).__name__ == 'LenderUser'


def lender_funder(user):
    key = getattr(user, 'key_obj', None)
    return getattr(key, 'funder', None) if key is not None else None


def desk_role(user, funder=None) -> str | None:
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    if is_lender(user):
        f = lender_funder(user)
        if f is None or (funder is not None and f.pk != funder.pk):
            return None
        return APPROVER
    if getattr(user, 'is_staff', False):
        return STAFF
    from core.models import FunderMembership
    qs = FunderMembership.objects.filter(user=user)
    if funder is not None:
        qs = qs.filter(funder=funder)
    roles = set(qs.values_list('role', flat=True))
    if APPROVER in roles:
        return APPROVER
    if VIEWER in roles:
        return VIEWER
    return None


def visible_funders(user):
    from core.models import Funder
    if user is None or not getattr(user, 'is_authenticated', False):
        return Funder.objects.none()
    if is_lender(user):
        f = lender_funder(user)
        return Funder.objects.filter(pk=f.pk) if f else Funder.objects.none()
    if getattr(user, 'is_staff', False):
        return Funder.objects.all()
    return Funder.objects.filter(memberships__user=user).distinct()


def resolve_funder(request):
    """The funder named by ``?funder=`` (or body ``funder``), else the first visible."""
    funders = visible_funders(request.user).order_by('id')
    if not funders.exists():
        raise PermissionDenied('Capital desk access is limited to TruckWys staff and funder members.')
    raw = request.query_params.get('funder') or (request.data.get('funder') if hasattr(request, 'data')
                                                 and isinstance(request.data, dict) else None)
    if raw:
        try:
            f = funders.filter(pk=int(raw)).first()
        except (TypeError, ValueError):
            f = None
        if f is None:
            raise NotFound('Funder not found')
        return f
    return funders.first()


def company_scope_q(user, field='facility__company_id') -> Q:
    """Extra narrowing for API keys bound to specific transporters."""
    if is_lender(user):
        return Q(**{f'{field}__in': list(getattr(user, 'company_ids', ()) or ())})
    return Q()


def can_approve(user, funder) -> tuple[bool, str]:
    role = desk_role(user, funder)
    if role == APPROVER:
        return True, ''
    if role == STAFF:
        if funder.staff_may_approve:
            return True, ''
        return False, ('This funder approves every advance itself (Mode A). The capital desk cannot approve '
                       'without a written delegation (Funder.staff_may_approve).')
    return False, 'Only the funder\'s approvers can approve or decline advances.'


def require_staff(user):
    if not getattr(user, 'is_staff', False) or is_lender(user):
        raise PermissionDenied('Only the TruckWys capital desk can do this.')


def require_desk(user, funder):
    role = desk_role(user, funder)
    if role is None:
        raise PermissionDenied('Capital desk access is limited to TruckWys staff and funder members.')
    return role
