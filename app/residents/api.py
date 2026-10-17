"""The identity domain's public interface — the ONLY module other domains may import from `residents`.

Enforced by Tach (tach.toml): `from residents.models import Residency` anywhere outside this app
fails CI. See docs/domain-boundaries.md for the rules every `<domain>/api.py` follows.

Identity is the one domain every other one needs, so it is also the one exception to "DTOs, never
model instances": `Resident` is exported, because it is AUTH_USER_MODEL — Django already hands every
view one as `request.user`, and every domain holds a ForeignKey to it. Export it as a REFERENCE: for
type annotations, FK targets and reading who somebody is. Changing a resident (or their roster rows,
or their roles) goes through the commands below, which is what lets `residents` keep its invariants.
Tach cannot see a `.save()` on an instance somebody else handed you; that rule is a review rule.

What stays private: `Residency`, `RoleAssignment`, `AdminAccessGrant` and everything in
`residents.services`/`views`. The month-by-month roster is identity's business — other domains ask
the questions below about it rather than querying its table.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from django.db.models import Q, QuerySet
from django.http import HttpRequest

from core import domain_events
from core.danish import MONTHS

from .birthdays import Birthday
from .birthdays import in_span as birthdays_in_span
from .models import (
    Residency,
    Resident,
    Role,
    RoleAssignment,
    active_period,
    embedsgruppe_of,
    next_period,
    prev_period,
)
from .permissions import (
    CMS_EDITOR_ROLES,
    MODERATION_ROLES,
    View,
    can_preview,
    current_resident,
    effective_roles,
    has_active_role,
    request_has_role,
    role_required,
)

__all__ = [
    "CMS_EDITOR_ROLES",
    "MODERATION_ROLES",
    "Birthday",
    "MemberAreaVisited",
    "Period",
    "Resident",
    "Role",
    "RosterEntry",
    "View",
    "active_period",
    "birthdays_in_span",
    "can_preview",
    "carry_roster_forward",
    "current_resident",
    "effective_roles",
    "embedsgruppe_of",
    "has_active_role",
    "leavers",
    "months_on_roster",
    "next_period",
    "occupants",
    "parse_period",
    "period_options",
    "place_on_roster",
    "prev_period",
    "remove_from_roster",
    "request_has_role",
    "resident_ids_with_roles",
    "role_required",
    "room_numbers_on_roster",
    "roster_resident_ids",
    "search_residents",
    "workgroup_ids_of",
]

Period = tuple[int, int]  # (year, month) of a monthly alumneliste


# --- Events ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MemberAreaVisited(domain_events.DomainEvent):
    """A resident opened the internal landing page, in the given active period.

    The cheapest "somebody is using the site this month" signal there is. AK hangs the backstop for
    its monthly charge off it (ak.handlers) — which is why `residents` no longer imports `ak`.
    """

    resident_id: int
    year: int
    month: int


# --- DTOs -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RosterEntry:
    """One resident's place on one month's alumneliste."""

    resident: Resident
    room_number: int


# --- Queries --------------------------------------------------------------------------------------


def roster_resident_ids(period: Period | None = None) -> QuerySet:
    """Ids of everyone on the alumneliste for `period` (the active month by default) — what "bor her"
    means in this codebase. The whole Resident table includes alumni.

    A lazy queryset of ids, not a list: callers put it in an `__in`, and as a subquery that stays one
    SQL statement. Ids may cross a boundary; model instances of Residency may not.
    """
    year, month = period or active_period()
    return Residency.objects.filter(year=year, month=month).values_list("resident_id", flat=True)


def resident_ids_with_roles(roles: Collection[str], period: Period | None = None) -> QuerySet:
    """Ids of residents holding any of `roles` in `period` (the active month by default), as a lazy
    subquery like roster_resident_ids.

    Assignments only. Superusers hold every role with no assignment row, and `administrator` implies
    every role — callers that need either add it themselves, as they did before this existed.
    """
    year, month = period or active_period()
    return RoleAssignment.objects.filter(role__in=list(roles), year=year, month=month).values_list(
        "resident_id", flat=True
    )


def workgroup_ids_of(resident_id: int, period: Period | None = None) -> list[int]:
    """The workgroup(s) a resident belongs to in `period`: zero or one id, as a list for `__in`."""
    year, month = period or active_period()
    return list(
        Residency.objects.filter(resident_id=resident_id, year=year, month=month, workgroup__isnull=False)
        .values_list("workgroup_id", flat=True)
        .distinct()
    )


def months_on_roster(resident_id: int) -> list[Period]:
    """Every month this resident was on the alumneliste."""
    return list(Residency.objects.filter(resident_id=resident_id).values_list("year", "month"))


def room_numbers_on_roster(period: Period) -> dict[int, int]:
    """resident id -> room number, for everyone on the `period` list."""
    year, month = period
    return dict(Residency.objects.filter(year=year, month=month).values_list("resident_id", "room__number"))


def occupants(room_ids: Iterable[int], period: Period, *, excluding: Iterable[int] = ()) -> list[RosterEntry]:
    """Who is listed in any of `room_ids` in `period`, minus the residents in `excluding`."""
    year, month = period
    rows = (
        Residency.objects.filter(room_id__in=list(room_ids), year=year, month=month)
        .exclude(resident_id__in=list(excluding))
        .select_related("resident", "room")
    )
    return [RosterEntry(r.resident, r.room.number) for r in rows]


def leavers(period: Period) -> list[RosterEntry]:
    """Who held a room the month before `period` but is not on the `period` list — the moved-out."""
    prior = Residency.objects.filter(year=prev_period(period)[0], month=prev_period(period)[1])
    still_here = set(roster_resident_ids(period))
    return [
        RosterEntry(r.resident, r.room.number)
        for r in prior.select_related("resident", "room")
        if r.resident_id not in still_here
    ]


def search_residents(q: str, *, limit: int = 50) -> list[Resident]:
    """Any resident, current or former, whose name or email contains `q`."""
    return list(
        Resident.objects.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
        ).order_by("first_name", "last_name")[:limit]
    )


def parse_period(request: HttpRequest) -> Period:
    """The (year, month) chosen via ?period=YYYY-M, or the active period as default."""
    try:
        ys, ms = (request.GET.get("period") or "").split("-")
        y, m = int(ys), int(ms)
        if 1 <= m <= 12:
            return y, m
    except ValueError:
        pass
    return active_period()


def period_options(selected: Period) -> list[dict[str, str | bool]]:
    """All months that have a published list, newest first, for a history picker."""
    periods = Residency.objects.values_list("year", "month").distinct().order_by("-year", "-month")
    return [
        {"value": f"{y}-{m}", "label": f"{MONTHS[m].capitalize()} {y}", "selected": (y, m) == selected}
        for y, m in periods
    ]


# --- Commands -------------------------------------------------------------------------------------


def carry_roster_forward(source: Period, target: Period) -> None:
    """Copy everyone on the `source` list who is not already on `target` forward — same room,
    embedsgruppe, rengøring and roles. Non-destructive: residents already on the target list (e.g. an
    already-copied next-month list) are left untouched."""
    sy, sm = source
    ty, tm = target
    existing = set(roster_resident_ids(target))
    for r in Residency.objects.filter(year=sy, month=sm):
        if r.resident_id in existing:
            continue
        Residency.objects.create(
            resident_id=r.resident_id,
            room_id=r.room_id,
            workgroup_id=r.workgroup_id,
            cleaning_id=r.cleaning_id,
            year=ty,
            month=tm,
        )
        for ra in RoleAssignment.objects.filter(resident_id=r.resident_id, year=sy, month=sm):
            RoleAssignment.objects.get_or_create(resident_id=r.resident_id, role=ra.role, year=ty, month=tm)
        existing.add(r.resident_id)


def place_on_roster(resident_id: int, period: Period, room_id: int) -> None:
    """Put a resident in `room_id` on the `period` list — MOVING their row if they already have one
    that month (the list is unique on resident + month), creating it if not."""
    year, month = period
    Residency.objects.update_or_create(
        resident_id=resident_id, year=year, month=month, defaults={"room_id": room_id}
    )


def remove_from_roster(resident_ids: Iterable[int], period: Period) -> None:
    """Take residents off the `period` list, together with their roles for that month."""
    ids = list(resident_ids)
    year, month = period
    Residency.objects.filter(resident_id__in=ids, year=year, month=month).delete()
    RoleAssignment.objects.filter(resident_id__in=ids, year=year, month=month).delete()
