"""Whose roles are what: an account's own, a request's (an administrator may preview others'), and
who holds a permission, as a queryset for notification audiences and lists.

An account's roles:

  * the kitchen staff's account: KITCHEN, and nothing else;
  * every other account: RESIDENT, and the role of each embedsgruppe it holds in the month in effect
    (residents.models.active_period);
  * a superuser: ADMINISTRATOR as well.
"""

from django.db.models import Q, QuerySet
from django.http import HttpRequest

from residents.models import Resident, RoleAssignment, active_period

from .features import parse
from .permissions import Permissions, permissions_of
from .policy import ADMINISTRATOR, KITCHEN, RESIDENT, ROLE_LABELS, roles_with

# The session key of "Se side som": the roles the site is shown as, a list of role codes.
PREVIEW_SESSION_KEY = "preview_roles"

# Attributes the answers are memoised under, on the request's user instance and the request. Both
# live exactly one request, so a snapshot can never outlive the request that took it, and a request
# cannot end up half-authorised because roles changed while it was handled.
_ROLES_MEMO = "_access_roles"
_REQUEST_MEMO = "_access_request_roles"


def _load(user: Resident) -> frozenset[str]:
    if not user.is_active:
        return frozenset()
    if user.is_kitchen:
        return frozenset({KITCHEN})
    year, month = active_period()
    held = set(user.role_assignments.filter(year=year, month=month).values_list("role", flat=True))
    if user.is_superuser:
        held.add(ADMINISTRATOR)
    return frozenset({RESIDENT, *held})


def real_roles(user: object) -> frozenset[str]:
    """The account's own roles, never a preview's. None for anyone not logged in."""
    if not isinstance(user, Resident):
        return frozenset()
    cached = getattr(user, _ROLES_MEMO, None)
    if cached is None:
        cached = _load(user)
        setattr(user, _ROLES_MEMO, cached)
    return cached


def forget(user: object) -> None:
    """Drop the memo, for code that changes the current account's roles and must see it at once."""
    if hasattr(user, _ROLES_MEMO):
        delattr(user, _ROLES_MEMO)


def can_preview(user: object) -> bool:
    """Only a real administrator (or superuser) may look at the site as someone else."""
    return ADMINISTRATOR in real_roles(user)


def previewed_roles(request: HttpRequest) -> frozenset[str] | None:
    """The roles "Se side som" shows the site as, or None when it is off. Checked against the real
    roles every time, so a forged or stale session on anyone else is ignored."""
    if PREVIEW_SESSION_KEY not in request.session or not can_preview(request.user):
        return None
    chosen = {role for role in request.session.get(PREVIEW_SESSION_KEY) or [] if role in ROLE_LABELS}
    if KITCHEN in chosen:
        return frozenset({KITCHEN})
    return frozenset({RESIDENT, *chosen})


def request_roles(request: HttpRequest) -> frozenset[str]:
    """The roles this request is treated as: the preview's, or the account's own."""
    cached = getattr(request, _REQUEST_MEMO, None)
    if cached is None:
        previewed = previewed_roles(request)
        cached = previewed if previewed is not None else real_roles(request.user)
        setattr(request, _REQUEST_MEMO, cached)
    return cached


def request_permissions(request: HttpRequest) -> Permissions:
    return permissions_of(request_roles(request))


def holders(permission: str) -> QuerySet[Resident]:
    """The active accounts that hold `permission` as their own (no preview), in SQL. What a
    notification audience or a list of the people who run a feature is narrowed by."""
    feature, action = parse(permission)
    roles = roles_with(feature, action) | {ADMINISTRATOR}
    year, month = active_period()
    match = Q(is_superuser=True, is_kitchen=False)
    if RESIDENT in roles:
        match |= Q(is_kitchen=False)
    if KITCHEN in roles:
        match |= Q(is_kitchen=True)
    groups = roles - {RESIDENT, KITCHEN}
    match |= Q(
        is_kitchen=False,
        pk__in=RoleAssignment.objects.filter(role__in=groups, year=year, month=month).values("resident_id"),
    )
    return Resident.objects.filter(match, is_active=True)
