"""Køkkenvagter business rules (spec/features/kitchen-duty.md).

Every rule that moves points or changes who holds a shift lives here, so the views are just
permission gates + forms, and the scheduled tasks share exactly the same code paths.
"""

import datetime
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Count, F, QuerySet, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.clock import current_datetime
from core.danish import MONTHS, WEEKDAYS_SHORT
from notifications import categories
from notifications import services as notifications
from residents.models import Residency, Resident, active_period

from .models import KitchenAssignment, KitchenMonthlyState, KitchenPointEntry, KitchenShift

logger = logging.getLogger(__name__)

MARKET_URL = "/intern/koekkenvagter/"

MONTHLY_CHARGE = 4
CALENDAR_MONTHS_AHEAD = 3
FREE_UNENROLL_DAYS = 30
# A fresh self-enrolment may be undone shortly after, unless the shift is about to start.
REGRET_WINDOW = datetime.timedelta(minutes=10)
REGRET_MIN_BEFORE_START = datetime.timedelta(hours=1)
ABSENCE_WINDOW_DAYS = 7
MAX_SALE_BONUS = 3

Kind = KitchenShift.Kind

# weekday (Mon=0) -> [(kind, start, end, spots, points per spot)]
_WEEKDAY = [
    (Kind.MORNING, datetime.time(6, 30), datetime.time(7, 30), 1, 1),
    (Kind.MIDDAY, datetime.time(13, 0), datetime.time(14, 0), 1, 1),
    (Kind.EVENING, datetime.time(17, 30), datetime.time(21, 0), 2, 3),
]
_FRIDAY = [*_WEEKDAY[:2], (Kind.EVENING, datetime.time(17, 30), datetime.time(21, 0), 2, 4)]
_WEEKEND_DAY = [
    (Kind.MORNING, datetime.time(8, 0), datetime.time(10, 0), 1, 1),
    (Kind.MIDDAY, datetime.time(13, 0), datetime.time(14, 0), 1, 1),
]
STANDARD_WEEK = {
    0: _WEEKDAY,
    1: _WEEKDAY,
    2: _WEEKDAY,
    3: _WEEKDAY,
    4: _FRIDAY,
    5: [*_WEEKEND_DAY, (Kind.EVENING, datetime.time(19, 0), datetime.time(20, 0), 1, 1)],
    6: [*_WEEKEND_DAY, (Kind.EVENING, datetime.time(19, 0), datetime.time(20, 0), 1, 2)],
}


class ShiftError(Exception):
    """A rule refused the action. The message is Danish and shown to the resident as-is."""


# --- time helpers ---------------------------------------------------------------------------------


def _aware(date: datetime.date, time: datetime.time) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(date, time))


def add_months(date: datetime.date, months: int) -> datetime.date:
    month_index = date.month - 1 + months
    year, month = date.year + month_index // 12, month_index % 12 + 1
    for day in (date.day, 30, 29, 28):
        try:
            return datetime.date(year, month, day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def calendar_horizon() -> datetime.date:
    return add_months(current_datetime().date(), CALENDAR_MONTHS_AHEAD)


# --- calendar -------------------------------------------------------------------------------------


def ensure_calendar() -> int:
    """Create the standard shifts from today up to the 3-month horizon. Idempotent; returns how many
    were created. Cheap when already done: one indexed lookup."""
    today = current_datetime().date()
    horizon = calendar_horizon()
    if KitchenShift.objects.filter(date=horizon).exclude(kind=Kind.SPECIAL).exists():
        return 0
    existing = set(
        KitchenShift.objects.filter(date__gte=today, date__lte=horizon)
        .exclude(kind=Kind.SPECIAL)
        .values_list("date", "kind")
    )
    new = []
    day = today
    while day <= horizon:
        for kind, start, end, spots, points in STANDARD_WEEK[day.weekday()]:
            if (day, kind) in existing:
                continue
            new.append(
                KitchenShift(
                    kind=kind,
                    date=day,
                    starts_at=_aware(day, start),
                    ends_at=_aware(day, end),
                    spots=spots,
                    points_per_spot=points,
                )
            )
        day += datetime.timedelta(days=1)
    KitchenShift.objects.bulk_create(new, ignore_conflicts=True)
    return len(new)


def open_shifts() -> QuerySet[KitchenShift]:
    """Future, enabled shifts that still have a free spot."""
    return (
        KitchenShift.objects.filter(is_disabled=False, starts_at__gt=current_datetime())
        .annotate(taken=Coalesce(Sum("assignments__spots"), 0))
        .filter(taken__lt=F("spots"))
    )


def unmanned_shifts(within: datetime.timedelta) -> QuerySet[KitchenShift]:
    """Future, enabled shifts within `within` that nobody has taken at all. A half-taken multi-spot
    shift is not unmanned: its single holder is given the remaining spots when it ends."""
    now = current_datetime()
    return (
        KitchenShift.objects.filter(is_disabled=False, starts_at__gt=now, starts_at__lte=now + within)
        .annotate(n=Count("assignments"))
        .filter(n=0)
    )


# --- ledger ---------------------------------------------------------------------------------------


@transaction.atomic
def post_entry(
    resident_id: int,
    amount: int,
    message: str,
    kind: str,
    *,
    shift: KitchenShift | None = None,
    created_by: Resident | None = None,
    year: int | None = None,
    month: int | None = None,
) -> KitchenPointEntry:
    # Lock the resident so two postings cannot both read the same balance_before.
    Resident.objects.select_for_update().only("pk").get(pk=resident_id)
    before = KitchenPointEntry.balance_for(resident_id)
    return KitchenPointEntry.objects.create(
        resident_id=resident_id,
        kind=kind,
        balance_before=before,
        amount=amount,
        balance_after=before + amount,
        message=message[:255],
        shift=shift,
        created_by=created_by,
        year=year,
        month=month,
    )


def manual_entry(resident: Resident, amount: int, message: str, admin: Resident) -> KitchenPointEntry:
    if not amount:
        raise ShiftError("Beløbet må ikke være 0.")
    if not message.strip():
        raise ShiftError("Skriv en begrundelse.")
    return post_entry(resident.pk, amount, message.strip(), KitchenPointEntry.Kind.MANUAL, created_by=admin)


def apply_monthly_charge(year: int, month: int) -> int:
    """Subtract MONTHLY_CHARGE from everyone on (year, month)'s alumneliste. Idempotent."""
    charged = set(
        KitchenPointEntry.objects.filter(
            kind=KitchenPointEntry.Kind.MONTHLY, year=year, month=month
        ).values_list("resident_id", flat=True)
    )
    members = Residency.objects.filter(year=year, month=month).values_list("resident_id", flat=True)
    written = 0
    for resident_id in set(members) - charged:
        post_entry(
            resident_id,
            -MONTHLY_CHARGE,
            f"Månedlig afregning {year}-{month:02d}",
            KitchenPointEntry.Kind.MONTHLY,
            year=year,
            month=month,
        )
        written += 1
    return written


def ensure_monthly_charge() -> None:
    """Backstop for the scheduled job, mirroring ak.services.ensure_active_month_applied."""
    year, month = active_period()
    state = KitchenMonthlyState.get()
    if (state.year, state.month) == (year, month):
        return
    try:
        apply_monthly_charge(year, month)
    except Exception:
        logger.exception("Kitchen monthly charge failed for %s-%02d", year, month)
        return
    state.year, state.month = year, month
    state.save(update_fields=["year", "month"])


# --- forecast -------------------------------------------------------------------------------------

# The forecast's zones. Balances are whole points: 3 and up is green, 0 to 2 yellow, -1 to -6 red,
# and -7 or lower the alarm zone.
YELLOW_MAX = 2
ALARM_BELOW = -6


def zone(balance: int) -> str:
    if balance > YELLOW_MAX:
        return "good"
    if balance >= 0:
        return "warn"
    if balance >= ALARM_BELOW:
        return "neg"
    return "alarm"


@dataclass(frozen=True)
class ForecastStep:
    at: datetime.datetime
    amount: int
    label: str
    balance: int  # after this step

    @property
    def zone(self) -> str:
        return zone(self.balance)


# The forecast's firm part. A shift starting within FREE_UNENROLL_DAYS can no longer be dropped, only
# sold, so the forecast balance that far ahead is what the overview judges a resident by.
OUTLOOK_DAYS = FREE_UNENROLL_DAYS


def _charged_months(residents: Sequence[Resident], horizon: datetime.date) -> dict[int, list[datetime.date]]:
    """Per resident, the first day of each month after the active one, up to `horizon`, that they
    will be charged for. A month whose alumneliste is already out says so; a later one is assumed to
    look like this month's, less a resident whose move-out date falls before it."""
    year, month = active_period()
    first = datetime.date(year, month, 1)
    months = []
    while (first := add_months(first, 1)) <= horizon:
        months.append(first)
    if not months:
        return {r.pk: [] for r in residents}
    window = Residency.objects.filter(year__gte=year, year__lte=months[-1].year)
    listed = set(window.values_list("year", "month").distinct())
    mine: dict[int, set[tuple[int, int]]] = {r.pk: set() for r in residents}
    for resident_id, y, m in window.filter(resident__in=residents).values_list(
        "resident_id", "year", "month"
    ):
        mine[resident_id].add((y, m))

    def charged(resident: Resident, first: datetime.date) -> bool:
        if (first.year, first.month) in listed:
            return (first.year, first.month) in mine[resident.pk]
        moves_out = resident.move_out_date
        return (year, month) in mine[resident.pk] and (moves_out is None or moves_out >= first)

    return {r.pk: [first for first in months if charged(r, first)] for r in residents}


def forecasts(residents: Sequence[Resident]) -> dict[int, list[ForecastStep]]:
    """Where each resident's balance is heading if nothing changes: the points for the spots they
    hold, booked when the shift ends, and the monthly charge on the 1st of each month up to the
    calendar horizon.

    Conservative on purpose. A lone holder of a multi-spot shift is counted for their own spots only,
    although complete_shift gives them the rest if nobody else signs up: somebody else may. A shift
    put for sale still counts, as it is theirs until it is sold."""
    now = current_datetime()
    events: dict[int, list[tuple[datetime.datetime, int, str]]] = {r.pk: [] for r in residents}
    held = KitchenAssignment.objects.filter(
        resident__in=residents, shift__completed_at__isnull=True, shift__is_disabled=False
    ).select_related("shift")
    for a in held:
        shift = a.shift
        start = timezone.localtime(shift.starts_at)
        label = f"{shift.title} {WEEKDAYS_SHORT[start.weekday()]} {start:%d.%m}"
        if a.spots > 1:
            label += f" ({a.spots} pladser)"
        if a.for_sale:
            label += " – til salg"
        events[a.resident_id].append((max(shift.ends_at, now), a.spots * shift.points_per_spot_total, label))
    for resident_id, months in _charged_months(residents, calendar_horizon()).items():
        for first in months:
            at = max(timezone.make_aware(datetime.datetime.combine(first, datetime.time())), now)
            events[resident_id].append((at, -MONTHLY_CHARGE, f"Månedlig afregning {MONTHS[first.month]}"))
    # The ledger's SUM is the newest balance_after by construction (KitchenPointEntry.balances).
    balances = dict(
        KitchenPointEntry.objects.filter(resident__in=residents)
        .values("resident")
        .annotate(b=Sum("amount"))
        .values_list("resident", "b")
    )
    result = {}
    for resident in residents:
        balance = balances.get(resident.pk, 0)
        steps = []
        # A charge and a shift at the same moment: the charge first, as the scheduled job runs at night.
        for at, amount, label in sorted(events[resident.pk], key=lambda e: (e[0], e[1] > 0)):
            balance += amount
            steps.append(ForecastStep(at, amount, label, balance))
        result[resident.pk] = steps
    return result


def forecast(resident: Resident) -> list[ForecastStep]:
    return forecasts([resident])[resident.pk]


def balance_at(balance: int, steps: list[ForecastStep], when: datetime.datetime) -> int:
    """The forecast balance at `when`, given today's `balance` and the steps from forecast()."""
    for step in reversed(steps):
        if step.at <= when:
            return step.balance
    return balance


# --- enrolment ------------------------------------------------------------------------------------


def _locked_shift(shift_id: int) -> KitchenShift:
    return KitchenShift.objects.select_for_update().get(pk=shift_id)


@transaction.atomic
def enroll(shift: KitchenShift, resident: Resident, spots: int = 1) -> None:
    shift = _locked_shift(shift.pk)
    if shift.is_disabled:
        raise ShiftError("Vagten er aflyst.")
    if shift.has_started:
        raise ShiftError("Vagten er allerede startet.")
    if spots < 1:
        raise ShiftError("Vælg mindst én plads.")
    assignments = list(shift.assignments.select_for_update())
    free = shift.spots - sum(a.spots for a in assignments)
    missing = spots - free
    if missing > 0:
        # Override: take spots from others holding more than one spot on this shift.
        for other in assignments:
            if other.resident_id == resident.pk:
                continue
            take = min(missing, other.spots - 1)
            if take <= 0:
                continue
            other.spots -= take
            other.save(update_fields=["spots", "updated_at"])
            missing -= take
            if not missing:
                break
        if missing > 0:
            raise ShiftError("Der er ikke nok ledige pladser på vagten.")
    mine = next((a for a in assignments if a.resident_id == resident.pk), None)
    if mine:
        mine.spots += spots
        mine.save(update_fields=["spots", "updated_at"])
    else:
        KitchenAssignment.objects.create(
            shift=shift, resident=resident, spots=spots, self_enrolled_at=current_datetime()
        )


def can_unenroll(assignment: KitchenAssignment) -> bool:
    now = current_datetime()
    until_start = assignment.shift.starts_at - now
    if until_start > datetime.timedelta(days=FREE_UNENROLL_DAYS):
        return True
    enrolled = assignment.self_enrolled_at
    return (
        enrolled is not None
        and now - enrolled <= REGRET_WINDOW
        and until_start > REGRET_MIN_BEFORE_START
        and not assignment.for_sale
    )


@transaction.atomic
def unenroll(assignment: KitchenAssignment) -> None:
    if not can_unenroll(assignment):
        raise ShiftError(
            f"Du kan kun melde fra mere end {FREE_UNENROLL_DAYS} dage før, eller inden for "
            f"{REGRET_WINDOW.seconds // 60} minutter efter du skrev dig på. Sæt vagten til salg i stedet."
        )
    assignment.delete()


# --- trading --------------------------------------------------------------------------------------


@transaction.atomic
def put_for_sale(assignment: KitchenAssignment, bonus: int) -> None:
    assignment = KitchenAssignment.objects.select_for_update().select_related("shift").get(pk=assignment.pk)
    if assignment.shift.has_started:
        raise ShiftError("Vagten er allerede startet.")
    if not 0 <= bonus <= MAX_SALE_BONUS:
        raise ShiftError(f"Du kan tilbyde mellem 0 og {MAX_SALE_BONUS} ekstra point.")
    assignment.for_sale = True
    assignment.sale_bonus = bonus
    assignment.put_for_sale_at = current_datetime()
    assignment.save(update_fields=["for_sale", "sale_bonus", "put_for_sale_at", "updated_at"])
    notify_for_sale(assignment)


@transaction.atomic
def cancel_sale(assignment: KitchenAssignment) -> None:
    assignment.for_sale = False
    assignment.sale_bonus = 0
    assignment.put_for_sale_at = None
    assignment.save(update_fields=["for_sale", "sale_bonus", "put_for_sale_at", "updated_at"])


@transaction.atomic
def buy(assignment: KitchenAssignment, buyer: Resident) -> None:
    _locked_shift(assignment.shift_id)
    assignment = KitchenAssignment.objects.select_related("shift").get(pk=assignment.pk)
    shift = assignment.shift
    if not assignment.for_sale:
        raise ShiftError("Vagten er ikke længere til salg.")
    if assignment.resident_id == buyer.pk:
        raise ShiftError("Du kan ikke købe din egen vagt.")
    if shift.has_started:
        raise ShiftError("Vagten er allerede startet.")
    seller_id, bonus = assignment.resident_id, assignment.sale_bonus
    mine = KitchenAssignment.objects.filter(shift=shift, resident=buyer).first()
    if mine:
        mine.spots += assignment.spots
        mine.self_enrolled_at = None
        mine.save(update_fields=["spots", "self_enrolled_at", "updated_at"])
        assignment.delete()
    else:
        assignment.resident = buyer
        assignment.for_sale = False
        assignment.sale_bonus = 0
        assignment.put_for_sale_at = None
        # A bought shift is never in the regret window, or the buyer could keep the bonus and drop it.
        assignment.self_enrolled_at = None
        assignment.save()
    if bonus:
        label = f"{shift.title} {timezone.localtime(shift.starts_at):%d.%m.%Y}"
        post_entry(
            seller_id, -bonus, f"Ekstra point for salg af {label}", KitchenPointEntry.Kind.TRADE, shift=shift
        )
        post_entry(
            buyer.pk, bonus, f"Ekstra point for køb af {label}", KitchenPointEntry.Kind.TRADE, shift=shift
        )


# --- completion and absence -----------------------------------------------------------------------


@transaction.atomic
def complete_shift(shift: KitchenShift) -> bool:
    """Book the points for an ended shift. Returns False when there was nothing to do."""
    shift = _locked_shift(shift.pk)
    if shift.completed_at or shift.is_disabled or not shift.has_ended:
        return False
    assignments = list(shift.assignments.all())
    if len(assignments) == 1 and assignments[0].spots < shift.spots:
        assignments[0].spots = shift.spots
    label = f"{shift.title} {timezone.localtime(shift.starts_at):%d.%m.%Y}"
    for a in assignments:
        a.awarded_points = a.spots * shift.points_per_spot_total
        a.save(update_fields=["spots", "awarded_points", "updated_at"])
        spots = f" ({a.spots} pladser)" if a.spots > 1 else ""
        post_entry(
            a.resident_id, a.awarded_points, f"{label}{spots}", KitchenPointEntry.Kind.SHIFT, shift=shift
        )
    shift.completed_at = current_datetime()
    shift.save(update_fields=["completed_at", "updated_at"])
    return True


def complete_finished_shifts() -> int:
    due = KitchenShift.objects.filter(
        completed_at__isnull=True, is_disabled=False, ends_at__lte=current_datetime()
    )
    return sum(complete_shift(shift) for shift in due)


def can_mark_absent(assignment: KitchenAssignment) -> bool:
    shift = assignment.shift
    now = current_datetime()
    return (
        not assignment.is_absent
        and not shift.is_disabled
        and shift.ends_at <= now
        and shift.starts_at >= now - datetime.timedelta(days=ABSENCE_WINDOW_DAYS)
    )


@transaction.atomic
def mark_absent(assignment: KitchenAssignment, fine: int | None, admin: Resident) -> None:
    complete_shift(assignment.shift)
    assignment = KitchenAssignment.objects.select_for_update().select_related("shift").get(pk=assignment.pk)
    if not can_mark_absent(assignment):
        raise ShiftError(f"Udeblivelse kan kun registreres for vagter de seneste {ABSENCE_WINDOW_DAYS} dage.")
    if fine is None:
        fine = assignment.awarded_points
    if fine < 0:
        raise ShiftError("Bøden kan ikke være negativ.")
    shift = assignment.shift
    label = f"{shift.title} {timezone.localtime(shift.starts_at):%d.%m.%Y}"
    post_entry(
        assignment.resident_id,
        -(assignment.awarded_points + fine),
        f"Udeblevet fra {label}: {assignment.awarded_points} point tilbageført + {fine} i bøde",
        KitchenPointEntry.Kind.ABSENCE,
        shift=shift,
        created_by=admin,
    )
    assignment.is_absent = True
    assignment.save(update_fields=["is_absent", "updated_at"])


# --- administration -------------------------------------------------------------------------------


def disable_shift(shift: KitchenShift, reason: str) -> None:
    if shift.has_started:
        raise ShiftError("Kun fremtidige vagter kan aflyses.")
    if not reason.strip():
        raise ShiftError("Skriv en begrundelse.")
    shift.is_disabled = True
    shift.disabled_reason = reason.strip()[:255]
    shift.save(update_fields=["is_disabled", "disabled_reason", "updated_at"])


def enable_shift(shift: KitchenShift) -> None:
    if shift.has_started:
        raise ShiftError("Kun fremtidige vagter kan genåbnes.")
    shift.is_disabled = False
    shift.disabled_reason = ""
    shift.save(update_fields=["is_disabled", "disabled_reason", "updated_at"])


def set_bonus(shift: KitchenShift, bonus: int) -> None:
    if shift.completed_at:
        raise ShiftError("Pointene for vagten er allerede uddelt.")
    if bonus < 0:
        raise ShiftError("Bonus kan ikke være negativ.")
    shift.bonus_points = bonus
    shift.save(update_fields=["bonus_points", "updated_at"])


# --- notifications --------------------------------------------------------------------------------


def _current_resident_ids() -> list[int]:
    """This month's alumneliste, as ids: notifications.notify would read a QuerySet's own pk, which
    for a Residency queryset is the wrong id."""
    year, month = active_period()
    return list(Residency.objects.filter(year=year, month=month).values_list("resident_id", flat=True))


def _negative_resident_ids(below: int) -> list[int]:
    current = set(_current_resident_ids())
    return [
        rid for rid, balance in KitchenPointEntry.balances().items() if balance < below and rid in current
    ]


def _shift_line(shift: KitchenShift) -> str:
    start = timezone.localtime(shift.starts_at)
    return f"{shift.title} {WEEKDAYS_SHORT[start.weekday()]} {start:%d.%m kl. %H:%M}"


def notify_for_sale(assignment: KitchenAssignment, *, background: bool = True) -> None:
    shift = assignment.shift
    if shift.starts_at - current_datetime() > datetime.timedelta(days=7):
        return
    bonus = f" (+{assignment.sale_bonus} ekstra point)" if assignment.sale_bonus else ""
    notifications.notify(
        _current_resident_ids(),
        category=categories.KITCHEN,
        title="Køkkenvagt til salg",
        body=f"{_shift_line(shift)}{bonus}",
        url=MARKET_URL,
        # Nobody can buy it once it has started.
        expires_at=shift.starts_at,
        exclude=assignment.resident_id,
        background=background,
    )


def _notify_unmanned(shifts: list[KitchenShift], recipients: Iterable[int], title: str) -> set[int]:
    """Tell `recipients` about `shifts`; returns who was reached. Expires when the last one starts."""
    if not shifts:
        return set()
    recipients = set(recipients)
    body = "; ".join(_shift_line(s) for s in shifts[:3])
    if len(shifts) > 3:
        body += f" og {len(shifts) - 3} mere"
    notifications.notify(
        recipients,
        category=categories.KITCHEN,
        title=title,
        body=body,
        url=MARKET_URL,
        expires_at=max(s.starts_at for s in shifts),
        background=False,
    )
    return recipients


def notify_unmanned_daily() -> None:
    """Everyone: unmanned within 24h. Negative balance: unmanned within 72h (once per run)."""
    told = _notify_unmanned(
        list(unmanned_shifts(datetime.timedelta(hours=24))),
        _current_resident_ids(),
        "Ledige køkkenvagter inden for et døgn",
    )
    _notify_unmanned(
        list(unmanned_shifts(datetime.timedelta(hours=72))),
        set(_negative_resident_ids(0)) - told,
        "Ledige køkkenvagter de næste 3 dage",
    )


def notify_unmanned_weekly() -> None:
    """Balance under -10: unmanned within the next 7 days."""
    _notify_unmanned(
        list(unmanned_shifts(datetime.timedelta(days=7))),
        _negative_resident_ids(-10),
        "Ledige køkkenvagter i den kommende uge",
    )
