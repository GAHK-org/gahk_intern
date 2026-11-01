"""Søndagsrengøring — the rules that are not just a row: which tasks apply, seeding the areas, and
making a rota."""

import datetime
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.text import slugify

from residents.models import Resident, active_period

from .areas_data import AREAS
from .models import AssignmentStatus, CleaningArea, CleaningAssignment, CleaningTask, Rota

STAR_PREFIX = "*"


def visible_tasks(assignment: CleaningAssignment) -> "QuerySet[CleaningTask]":
    """The checklist for one assignment: the area's active tasks, (*)-tasks only in (*)-uger."""
    tasks = assignment.area.tasks.filter(is_active=True)
    if not assignment.is_star_week:
        tasks = tasks.filter(star_only=False)
    return tasks


@transaction.atomic
def load_areas(reset: bool = False) -> tuple[int, int]:
    """Create the areas in areas_data.AREAS that do not exist yet, with their tasks.

    An existing area is left as Inspektionen has edited it, unless `reset`, which replaces its task
    list with the seed's (old tasks are deactivated, not deleted, so history keeps its checkmarks).
    Returns (areas created, tasks created).
    """
    areas_created = tasks_created = 0
    for order, (name, description, aliases, zones) in enumerate(AREAS):
        area, created = CleaningArea.objects.get_or_create(
            slug=slugify(name),
            defaults={
                "name": name,
                "description": description,
                "import_aliases": "\n".join(aliases),
                "order": order,
            },
        )
        areas_created += created
        if not created and not reset:
            continue
        area.tasks.filter(is_active=True).update(is_active=False)
        position = 0
        for zone, groups in zones:
            for group, texts in groups:
                for raw in texts:
                    star = raw.startswith(STAR_PREFIX)
                    CleaningTask.objects.create(
                        area=area,
                        zone=zone,
                        group=group,
                        text=raw.removeprefix(STAR_PREFIX),
                        star_only=star,
                        order=position,
                    )
                    position += 1
                    tasks_created += 1
    return areas_created, tasks_created


# ---- rotas ----


def rota_residents() -> "QuerySet[Resident]":
    """Who can be put on a rota: residents on the active month's alumneliste or a later one (a rota
    runs for months, and Indstillingen prepares next month's list ahead of time)."""
    year, month = active_period()
    return (
        Resident.objects.filter(
            Q(residencies__year__gt=year) | Q(residencies__year=year, residencies__month__gte=month)
        )
        .distinct()
        .order_by("first_name", "last_name")
    )


def sundays(start: datetime.date, weeks: int) -> list[datetime.date]:
    return [start + datetime.timedelta(weeks=i) for i in range(weeks)]


def is_star(week_index: int, star_every: int) -> bool:
    """Every `star_every`th Sunday is a (*)-uge, counting the first as 1 (E26: every 5th → 29/11)."""
    return star_every > 0 and (week_index + 1) % star_every == 0


def plan_rota(
    start: datetime.date,
    weeks: int,
    star_every: int,
    rotations: Mapping[CleaningArea, Sequence[Resident | None]],
) -> list[CleaningAssignment]:
    """One unsaved assignment per area per Sunday, taking each area's residents in turn."""
    planned = []
    for i, day in enumerate(sundays(start, weeks)):
        for area, people in rotations.items():
            planned.append(
                CleaningAssignment(
                    area=area,
                    date=day,
                    resident=people[i % len(people)] if people else None,
                    is_star_week=is_star(i, star_every),
                )
            )
    return planned


class RotaClash(Exception):
    """Some planned (area, date) pairs already belong to another rota."""

    def __init__(self, clashes: list[CleaningAssignment]) -> None:
        self.clashes = clashes
        super().__init__(", ".join(f"{a.area} {a.date:%d/%m/%Y}" for a in clashes))


def clashes(planned: Iterable[CleaningAssignment]) -> list[CleaningAssignment]:
    """The existing assignments that occupy any of `planned`'s (area, date) slots."""
    planned = list(planned)
    if not planned:
        return []
    dates = {a.date for a in planned}
    wanted = {(a.area_id, a.date) for a in planned}
    existing = CleaningAssignment.objects.filter(date__in=dates).select_related("area")
    return [a for a in existing if (a.area_id, a.date) in wanted]


@transaction.atomic
def save_rota(name: str, planned: list[CleaningAssignment], created_by: Resident | None) -> Rota:
    """Save a rota and its assignments as one unit, or nothing if any slot is taken (RotaClash)."""
    taken = clashes(planned)
    if taken:
        raise RotaClash(taken)
    rota = Rota.objects.create(name=name, start_date=min(a.date for a in planned), created_by=created_by)
    for a in planned:
        a.rota = rota
    CleaningAssignment.objects.bulk_create(planned)
    return rota


def replace_resident(rota: Rota, old: Resident, new: Resident | None, from_date: datetime.date) -> int:
    """Give `old`'s Sundays from `from_date` on to `new` (moving out). Sundays already inspected are
    history and are left alone. Returns how many changed."""
    return rota.assignments.filter(resident=old, date__gte=from_date, inspections__isnull=True).update(
        resident=new, signed_off_at=None, signed_off_by=None
    )


def rota_is_deletable(rota: Rota) -> bool:
    """A rota can be deleted while nothing has happened on it yet — no sign-off, no inspection."""
    return not rota.assignments.filter(Q(signed_off_at__isnull=False) | Q(inspections__isnull=False)).exists()


@dataclass
class Cell:
    assignment: CleaningAssignment
    status: AssignmentStatus

    @property
    def chip(self) -> str:
        return STATUS_CHIP[self.status]


# The chip variant (frontend/src/styles.css) each status is shown with.
STATUS_CHIP = {
    AssignmentStatus.KOMMENDE: "chip-quiet",
    AssignmentStatus.AFVENTER: "chip-quiet",
    AssignmentStatus.IKKE_TAGET: "chip-neg",
    AssignmentStatus.TAGET: "chip-brass",
    AssignmentStatus.GODKENDT: "",
    AssignmentStatus.MANGLER: "chip-neg",
    AssignmentStatus.UDBEDRET: "chip-brass",
    AssignmentStatus.OVERSKREDET: "chip-neg",
}


@dataclass
class GridRow:
    date: datetime.date
    is_star_week: bool
    cells: list[Cell | None]


def grid(
    assignments: Iterable[CleaningAssignment], now: datetime.datetime | None = None
) -> tuple[list[CleaningArea], list[GridRow]]:
    """Sundays × areas, for the rota page and the overview. Works on saved or planned assignments;
    pass saved ones with prefetch_related("inspections")."""
    now = now or timezone.now()
    assignments = list(assignments)
    areas = sorted({a.area for a in assignments}, key=lambda area: (area.order, area.name))
    by_slot = {(a.date, a.area): a for a in assignments}
    rows = []
    for day in sorted({a.date for a in assignments}):
        cells: list[Cell | None] = []
        for area in areas:
            a = by_slot.get((day, area))
            cells.append(Cell(a, a.status(now) if a.pk else AssignmentStatus.KOMMENDE) if a else None)
        rows.append(GridRow(day, any(c and c.assignment.is_star_week for c in cells), cells))
    return areas, rows
