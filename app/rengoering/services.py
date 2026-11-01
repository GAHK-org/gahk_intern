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
from .models import (
    AssignmentStatus,
    CleaningArea,
    CleaningAssignment,
    CleaningTask,
    Inspection,
    Rota,
    default_fix_deadline,
)

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


# ---- the alumne's side ----

# How far back an unresolved mangler keeps outranking the next Sunday on "Min rengøring" and the
# dashboard. Long enough to cover a missed deadline being chased; short enough that one old miss
# does not sit at the top of the page for the rest of the semester.
OPEN_MANGLER_WINDOW = datetime.timedelta(days=14)


def mine(resident: Resident) -> "QuerySet[CleaningAssignment]":
    return resident.cleaning_assignments.select_related("area", "rota").prefetch_related("inspections")


def next_for(resident: Resident, now: datetime.datetime | None = None) -> CleaningAssignment | None:
    """What the alumne should look at first: a recent mangler still to fix, else the next Sunday."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    recent = mine(resident).filter(
        date__gte=today - OPEN_MANGLER_WINDOW, inspections__outcome=Inspection.Outcome.MANGLER
    )
    for a in recent.distinct().order_by("date"):
        if a.status(now) in (AssignmentStatus.MANGLER, AssignmentStatus.OVERSKREDET):
            return a
    return mine(resident).filter(date__gte=today).order_by("date").first()


def can_sign_off(a: CleaningAssignment, now: datetime.datetime | None = None) -> bool:
    """From the day itself, until the cleaning has been judged."""
    now = now or timezone.now()
    return not a.signed_off_at and timezone.localdate(now) >= a.date and a.latest_decided() is None


def can_undo_sign_off(a: CleaningAssignment) -> bool:
    return bool(a.signed_off_at) and a.latest_decided() is None


def can_mark_fixed(a: CleaningAssignment) -> bool:
    latest = a.latest_decided()
    return latest is not None and latest.outcome == Inspection.Outcome.MANGLER and not latest.fixed_at


TaskGroups = list[tuple[str, list[tuple[str, list[CleaningTask]]]]]


def group_tasks(tasks: Iterable[CleaningTask]) -> TaskGroups:
    """[(zone, [(group, [task, ...]), ...]), ...] in task order — the checklist's shape on screen."""
    zones: TaskGroups = []
    for task in tasks:
        if not zones or zones[-1][0] != task.zone:
            zones.append((task.zone, []))
        groups = zones[-1][1]
        if not groups or groups[-1][0] != task.group:
            groups.append((task.group, []))
        groups[-1][1].append(task)
    return zones


# ---- inspection ----


class RoundClosed(Exception):
    """The assignment was approved; reopen the decision before changing its ticks."""


def checked_ids(a: CleaningAssignment) -> set[int]:
    """What the checklist starts from: the round in progress, else the last decided round (so a
    follow-up starts from where the Sunday check left off)."""
    current = a.current_round() or a.latest_decided()
    return set(current.checked_tasks.values_list("pk", flat=True)) if current else set()


@transaction.atomic
def save_round(a: CleaningAssignment, inspector: Resident, task_ids: Iterable[int], note: str) -> Inspection:
    """Store the ticks and note of the round being worked on, opening one if needed: round 1 on the
    Sunday, round n+1 after a Mangler. An approved assignment is closed (RoundClosed)."""
    current = a.current_round()
    if current is None:
        latest = a.latest_decided()
        if latest and latest.outcome == Inspection.Outcome.GODKENDT:
            raise RoundClosed
        current = Inspection.objects.create(
            assignment=a, round=(latest.round + 1) if latest else 1, inspector=inspector
        )
        a.forget_rounds()
    current.inspector = inspector
    current.note = note
    current.save(update_fields=["inspector", "note"])
    visible = set(visible_tasks(a).values_list("pk", flat=True))
    current.checked_tasks.set(visible & set(task_ids))
    return current


@transaction.atomic
def decide(
    a: CleaningAssignment,
    inspector: Resident,
    outcome: str,
    task_ids: Iterable[int],
    note: str,
    deadline: datetime.datetime | None = None,
    now: datetime.datetime | None = None,
) -> Inspection:
    """Godkendt or Mangler for the round in progress. Godkendt ticks everything; Mangler freezes the
    unticked tasks into `missing` and sets the fix deadline (22.00 the next day unless given)."""
    now = now or timezone.now()
    tasks = list(visible_tasks(a))
    if outcome == Inspection.Outcome.GODKENDT:
        task_ids = [t.pk for t in tasks]
    current = save_round(a, inspector, task_ids, note)
    ticked = set(current.checked_tasks.values_list("pk", flat=True))
    current.outcome = outcome
    current.decided_at = now
    current.missing = [{"zone": t.zone, "text": t.text} for t in tasks if t.pk not in ticked]
    current.deadline = (
        (deadline or default_fix_deadline(now)) if outcome == Inspection.Outcome.MANGLER else None
    )
    current.save(update_fields=["outcome", "decided_at", "missing", "deadline"])
    return current


def reopen(a: CleaningAssignment) -> bool:
    """Undo the last decision (a mis-tap), as long as no later round has started."""
    if a.current_round():
        return False
    latest = a.latest_decided()
    if latest is None:
        return False
    latest.outcome = ""
    latest.decided_at = latest.deadline = latest.fixed_at = None
    latest.missing = []
    latest.save(update_fields=["outcome", "decided_at", "deadline", "fixed_at", "missing"])
    return True


def inspection_dates() -> list[datetime.date]:
    return list(CleaningAssignment.objects.order_by("date").values_list("date", flat=True).distinct())


def default_inspection_date(today: datetime.date | None = None) -> datetime.date | None:
    """The latest day with assignments that is not in the future — on a Monday you are still
    following up Sunday's mangler — else the first one coming up."""
    today = today or timezone.localdate()
    dates = inspection_dates()
    past = [d for d in dates if d <= today]
    if past:
        return past[-1]
    return dates[0] if dates else None


# ---- history ----


@dataclass
class Record:
    """How one resident (or area) has done — what Inspektionen looks at for repeat offenders."""

    total: int = 0  # Sundays that have been
    approved_first_time: int = 0
    with_mangler: int = 0
    late: int = 0  # a mangler deadline passed before it was fixed
    not_signed_off: int = 0


def record(assignments: Iterable[CleaningAssignment], now: datetime.datetime | None = None) -> Record:
    now = now or timezone.now()
    today = timezone.localdate(now)
    r = Record()
    for a in assignments:
        if a.date > today:
            continue
        r.total += 1
        rounds = [i for i in a.rounds() if i.outcome]
        if rounds and rounds[0].outcome == Inspection.Outcome.GODKENDT:
            r.approved_first_time += 1
        if any(i.outcome == Inspection.Outcome.MANGLER for i in rounds):
            r.with_mangler += 1
        if any(
            _was_late(i, rounds[n + 1] if n + 1 < len(rounds) else None, now) for n, i in enumerate(rounds)
        ):
            r.late += 1
        if not a.signed_off_at:
            r.not_signed_off += 1
    return r


def _was_late(i: Inspection, following: Inspection | None, now: datetime.datetime) -> bool:
    """A mangler round is late when it was not resolved by its deadline. Resolved = the alumne
    pressed "udbedret", or else the next round was decided (Inspektionen re-checked unprompted)."""
    if i.outcome != Inspection.Outcome.MANGLER or not i.deadline:
        return False
    resolved = i.fixed_at or (following.decided_at if following else None) or now
    return resolved > i.deadline
