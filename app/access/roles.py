"""Whose roles are what: an account's own, a request's (an administrator may preview others'), many
accounts' at once, and who holds a permission, as a queryset for notification audiences and lists.

An account's roles:

  * its stage, from the alumneliste (access.stages): moving in, resident, moved out …;
  * the role of its embedsgruppe on the list in effect (residents.models.active_period and
    WORKGROUP_ROLE), read from the list itself;
  * every role given to it by hand that holds today (access.models.Grant), each from a first day
    through a last one: administrator, a role from the role page, the kitchen staff's, an exception
    the Indstilling made.

An administrator, by grant or as a superuser, is a resident whatever their months say.

An account with none of these has no access at all, and the front page says so.
"""

from collections.abc import Iterable

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.http import HttpRequest

from residents.models import WORKGROUP_ROLE, Residency, Resident, active_period

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
# In a preview's list: "person:<pk>", the site as that account's own roles open it.
PREVIEW_PERSON = "person:"

# Attributes the answers are memoised under, on the request's user instance and the request. Both
# live exactly one request, so a snapshot can never outlive the request that took it, and a request
# cannot end up half-authorised because roles changed while it was handled.
_ROLES_MEMO = "_access_roles"
_REQUEST_MEMO = "_access_request_roles"


# Each role's embedsgrupper on the list, the inverse of WORKGROUP_ROLE.
_WORKGROUPS: dict[str, list[str]] = {}
for _name, _role in WORKGROUP_ROLE.items():
    _WORKGROUPS.setdefault(_role, []).append(_name)


def workgroups_of(roles: Iterable[str]) -> list[str]:
    """The names of the embedsgrupper that give any of `roles` on the list."""
    return sorted(name for role in roles for name in _WORKGROUPS.get(role, ()))


def _combine(
    stage: Stage | None, workgroup: str | None, granted: Iterable[str], superuser: bool
) -> frozenset[str]:
    roles = set(granted)
    listed = WORKGROUP_ROLE.get(workgroup or "")
    if listed:
        roles.add(listed)
    if superuser:
        roles.add(ADMINISTRATOR)
    if ADMINISTRATOR in roles:
        stage = Stage.RESIDENT
    elif stage is None and KITCHEN not in roles and not roles & STAGES:
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
    workgroup = Residency.objects.filter(resident_id=OuterRef("person_id"), year=year, month=month).values(
        "workgroup__name"
    )[:1]
    granted = Grant.objects.active().filter(resident_id=OuterRef("pk"))
    rows = (
        with_stage(Resident.objects.filter(pk__in=ids, is_active=True), now=year * 12 + month - 1)
        .annotate(
            access_workgroup=Subquery(workgroup),
            access_granted=ArraySubquery(granted.values("role")),
        )
        .values_list("pk", "is_superuser", "access_stage", "access_workgroup", "access_granted")
    )
    return {
        pk: _combine(Stage(stage) if stage else None, workgroup_name, given, superuser)
        for pk, superuser, stage, workgroup_name, given in rows
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
    person = previewed_person_id(request)
    if person is not None:
        return roles_of_many([person]).get(person, frozenset())
    if KITCHEN in chosen:
        return frozenset({KITCHEN})
    roles = {role for role in chosen if role in ROLE_LABELS}
    if NO_STAGE not in chosen and not roles & STAGES:
        roles.add(RESIDENT)
    return frozenset(roles)


def previewed_person_id(request: HttpRequest) -> int | None:
    """The account "Se side som" shows the site as, when it shows one person's roles."""
    for entry in request.session.get(PREVIEW_SESSION_KEY) or []:
        if (
            isinstance(entry, str)
            and entry.startswith(PREVIEW_PERSON)
            and entry[len(PREVIEW_PERSON) :].isdigit()
        ):
            return int(entry[len(PREVIEW_PERSON) :])
    return None


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
    granted = Grant.objects.active()
    administrators = Q(pk__in=granted.filter(role=ADMINISTRATOR).values("resident_id")) | Q(is_superuser=True)
    match = Q(pk__in=granted.filter(role__in=wanted).values("resident_id"))
    names = workgroups_of(wanted & GROUPS)
    if names:
        listed = Residency.objects.filter(year=year, month=month, workgroup__name__in=names)
        match |= Q(person_id__in=listed.values("resident_id"))  # the list names people
    if ADMINISTRATOR in wanted or RESIDENT in wanted:
        # Administrators are residents, whatever their months say.
        match |= administrators
    by_stage = [stage.value for stage in Stage if stage.value in wanted]
    if by_stage:
        match |= Q(access_stage__in=by_stage)
    if stages.NEVER_LISTED is not None and stages.NEVER_LISTED.value in wanted:
        apart = granted.filter(role__in=STAGES | {KITCHEN}).values("resident_id")
        match |= Q(access_stage__isnull=True) & ~Q(pk__in=apart) & ~Q(is_superuser=True)
    return with_stage(Resident.objects.filter(is_active=True)).filter(match)


def holders(permission: str) -> QuerySet[Resident]:
    """The active accounts that hold `permission` as their own (no preview), in SQL. What a
    notification audience or a list of the people who run a feature is narrowed by."""
    feature, action = parse(permission)
    return holding(roles_with(feature, action) | {ADMINISTRATOR})
