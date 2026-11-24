"""Whose roles are what: an account's own, a request's (an administrator may preview others'), many
accounts' at once, and who holds a permission, as a queryset for notification audiences and lists.

An account's roles:

  * its stage, from the alumneliste (access.stages): moving in, resident, moved out …;
  * the role of each embedsgruppe it holds in the month in effect (residents.models.active_period);
    holding one makes it a resident, whatever its months say, and so does being a superuser, who is
    an administrator as well;
  * every role granted to it by hand that holds today (access.models.Grant): the kitchen staff's,
    an exception the Indstilling made.

An account with none of these has no access at all, and the front page says so.
"""

from collections.abc import Iterable

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import OuterRef, Q, QuerySet
from django.http import HttpRequest

from residents.models import Resident, RoleAssignment, active_period

from . import stages
from .features import parse
from .models import Grant
from .permissions import Permissions, permissions_of
from .policy import ADMINISTRATOR, GROUPS, KITCHEN, RESIDENT, ROLE_LABELS, STAGES, roles_with
from .stages import Stage, with_stage

# The session key of "Se side som": the roles the site is shown as, a list of role codes.
PREVIEW_SESSION_KEY = "preview_roles"
# In a preview's list instead of a stage: the account has no stage at all.
NO_STAGE = "none"

# Attributes the answers are memoised under, on the request's user instance and the request. Both
# live exactly one request, so a snapshot can never outlive the request that took it, and a request
# cannot end up half-authorised because roles changed while it was handled.
_ROLES_MEMO = "_access_roles"
_REQUEST_MEMO = "_access_request_roles"


def _combine(
    stage: Stage | None, groups: Iterable[str], granted: Iterable[str], superuser: bool
) -> frozenset[str]:
    held, given = set(groups), set(granted)
    roles = held | given
    if superuser:
        roles.add(ADMINISTRATOR)
    if held or superuser:
        stage = Stage.RESIDENT
    elif stage is None and not given:
        stage = stages.NEVER_LISTED
    if stage is not None:
        roles.add(stage.value)
    return frozenset(roles)


def roles_of_many(resident_ids: Iterable[int]) -> dict[int, frozenset[str]]:
    """Each active account's own roles, in two queries however many (the month in effect, then one
    for the accounts with their months, groups and grants). Inactive ones are left out."""
    ids = list(dict.fromkeys(resident_ids))
    if not ids:
        return {}
    year, month = active_period()
    groups = RoleAssignment.objects.filter(resident_id=OuterRef("pk"), year=year, month=month)
    granted = Grant.objects.active().filter(resident_id=OuterRef("pk"))
    rows = (
        with_stage(Resident.objects.filter(pk__in=ids, is_active=True), now=year * 12 + month - 1)
        .annotate(
            access_groups=ArraySubquery(groups.values("role")),
            access_granted=ArraySubquery(granted.values("role")),
        )
        .values_list("pk", "is_superuser", "access_stage", "access_groups", "access_granted")
    )
    return {
        pk: _combine(Stage(stage) if stage else None, held, given, superuser)
        for pk, superuser, stage, held, given in rows
    }


def real_roles(user: object) -> frozenset[str]:
    """The account's own roles, never a preview's. None for anyone not logged in."""
    if not isinstance(user, Resident):
        return frozenset()
    cached = getattr(user, _ROLES_MEMO, None)
    if cached is None:
        cached = roles_of_many([user.pk]).get(user.pk, frozenset()) if user.is_active else frozenset()
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
    roles every time, so a forged or stale session on anyone else is ignored.

    The list names roles; with no stage in it the site is shown as a resident holding them, unless
    it says NO_STAGE. The kitchen is shown as the kitchen alone."""
    if PREVIEW_SESSION_KEY not in request.session or not can_preview(request.user):
        return None
    chosen = set(request.session.get(PREVIEW_SESSION_KEY) or [])
    if KITCHEN in chosen:
        return frozenset({KITCHEN})
    roles = {role for role in chosen if role in ROLE_LABELS}
    if NO_STAGE not in chosen and not roles & STAGES:
        roles.add(RESIDENT)
    return frozenset(roles)


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


def holding(roles: Iterable[str]) -> QuerySet[Resident]:
    """The active accounts holding any of `roles` as their own, in SQL: the same rule as above."""
    wanted = set(roles)
    year, month = active_period()
    in_groups = RoleAssignment.objects.filter(year=year, month=month).values("resident_id")
    granted = Grant.objects.active()
    match = Q(pk__in=granted.filter(role__in=wanted).values("resident_id"))
    if wanted & GROUPS:
        match |= Q(pk__in=in_groups.filter(role__in=wanted & GROUPS))
    if ADMINISTRATOR in wanted:
        match |= Q(pk__in=in_groups.filter(role=ADMINISTRATOR)) | Q(is_superuser=True)
    if RESIDENT in wanted:
        # Group holders and superusers are residents, whatever their months say.
        match |= Q(pk__in=in_groups) | Q(is_superuser=True)
    by_stage = [stage.value for stage in Stage if stage.value in wanted]
    if by_stage:
        match |= Q(access_stage__in=by_stage)
    if stages.NEVER_LISTED is not None and stages.NEVER_LISTED.value in wanted:
        match |= Q(access_stage__isnull=True) & ~Q(pk__in=granted.values("resident_id"))
    return with_stage(Resident.objects.filter(is_active=True)).filter(match)


def holders(permission: str) -> QuerySet[Resident]:
    """The active accounts that hold `permission` as their own (no preview), in SQL. What a
    notification audience or a list of the people who run a feature is narrowed by."""
    feature, action = parse(permission)
    return holding(roles_with(feature, action) | {ADMINISTRATOR})
