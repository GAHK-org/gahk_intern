"""Køkkenvagter views. Every resident sees the whole calendar and market; Køkkengruppen (and
administrators) get the administrative actions, and Inspektionen may look at the overview and every
resident's forecast without them. The rules themselves live in services."""

import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.clock import current_datetime
from core.danish import MONTHS, WEEKDAYS_SHORT
from residents.models import Residency, Resident, Role, active_period
from residents.permissions import current_resident, request_has_role, role_required

from . import services
from .forms import SpecialShiftForm
from .models import KitchenAssignment, KitchenPointEntry, KitchenShift

ADMIN_ROLES = (Role.KOKKENGRUPPE, Role.ADMINISTRATOR)
# The overview and every resident's forecast and statement: Inspektionen too, read-only. Everything
# that changes something (special shifts, cancelling, bonus, absence, manual entries) is ADMIN_ROLES.
VIEW_ROLES = (*ADMIN_ROLES, Role.INSPEKTION)


def _is_admin(request: HttpRequest) -> bool:
    return request_has_role(request, *ADMIN_ROLES)


def _can_view_overview(request: HttpRequest) -> bool:
    return request_has_role(request, *VIEW_ROLES)


def _backstops() -> None:
    """Keep the calendar, the monthly charge and finished shifts current even if a scheduled job
    was missed. Each is a single cheap lookup when there is nothing to do."""
    services.ensure_calendar()
    services.ensure_monthly_charge()
    services.complete_finished_shifts()


def _with_assignees() -> Prefetch:
    return Prefetch(
        "assignments", queryset=KitchenAssignment.objects.select_related("resident").order_by("created_at")
    )


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def _attempt(request: HttpRequest, action: Callable[[], object], success: str) -> None:
    try:
        action()
    except services.ShiftError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, success)


SHEET_DAYS = 30


def _sheet_url(shift: KitchenShift) -> str:
    """Where the shift's row is: the plan for today onwards, the history before that."""
    after = shift.date + datetime.timedelta(days=1)
    if shift.date < current_datetime().date():
        return f"{reverse('kitchen:history')}?til={after:%Y-%m-%d}#dag-{shift.date:%Y-%m-%d}"
    return f"{reverse('kitchen:market')}?til={after:%Y-%m-%d}#dag-{shift.date:%Y-%m-%d}"


def _date_param(request: HttpRequest, name: str, default: datetime.date) -> datetime.date:
    try:
        return datetime.date.fromisoformat(request.GET.get(name, ""))
    except ValueError:
        return default


def _grid_columns() -> list[dict]:
    """One column per standard time slot (as on the paper sheet), widest spot count wins."""
    columns: dict[tuple[str, datetime.time], dict] = {}
    for day in services.STANDARD_WEEK.values():
        for kind, start, end, spots, _points in day:
            col = columns.setdefault((kind, start), {"kind": kind, "start": start, "end": end, "spots": 0})
            col["spots"] = max(col["spots"], spots)
    return sorted(columns.values(), key=lambda c: c["start"])


def _slots(shift: KitchenShift, resident_id: int) -> list[dict | None]:
    """One entry per spot: the holder's assignment, or None while the spot is free. A holder's spots
    beyond their first are `overridable`: anyone else may take them over before the shift starts."""
    slots: list[dict | None] = [
        {
            "a": a,
            "mine": a.resident_id == resident_id,
            "overridable": i > 0 and a.resident_id != resident_id and not shift.has_started,
        }
        for a in shift.assignments.all()
        for i in range(a.spots)
    ]
    return slots + [None] * max(0, shift.spots - len(slots))


def _urgency(shift: KitchenShift, now: datetime.datetime) -> str:
    """How badly a free spot on this shift needs taking: 'critical' within 24h, 'soon' within 72h."""
    until_start = shift.starts_at - now
    if until_start <= datetime.timedelta(0):
        return ""
    if until_start <= datetime.timedelta(hours=24):
        return "critical"
    if until_start <= datetime.timedelta(hours=72):
        return "soon"
    return ""


def _rows(start: datetime.date, end: datetime.date, columns: list[dict], resident_id: int) -> list[dict]:
    """Sheet rows for the days start..end (exclusive); a month heading opens each new month."""
    shifts = KitchenShift.objects.filter(date__gte=start, date__lt=end).prefetch_related(_with_assignees())
    now = current_datetime()
    by_day: dict[datetime.date, list[KitchenShift]] = {}
    for shift in shifts:
        shift.slots = _slots(shift, resident_id)  # type: ignore[attr-defined]
        shift.urgency = _urgency(shift, now)  # type: ignore[attr-defined]
        by_day.setdefault(shift.date, []).append(shift)
    today = current_datetime().date()
    cutoff = timezone.localtime(current_datetime() + datetime.timedelta(days=services.FREE_UNENROLL_DAYS))
    rows = []
    day = start
    while day < end:
        cells: list[KitchenShift | None] = [None] * len(columns)
        extra = []
        for shift in by_day.get(day, []):
            begins = timezone.localtime(shift.starts_at).time()
            index = next(
                (i for i, c in enumerate(columns) if c["kind"] == shift.kind and c["start"] == begins), None
            )
            if index is None or cells[index] is not None:
                extra.append(shift)
            else:
                cells[index] = shift
        rows.append(
            {
                "date": day,
                "month_label": f"{MONTHS[day.month].capitalize()} {day.year}"
                if day == today or day.day == 1
                else "",
                "weekday": WEEKDAYS_SHORT[day.weekday()],
                "is_weekend": day.weekday() >= 5,
                "is_today": day == today,
                "unenroll_cutoff": cutoff if day == cutoff.date() else None,
                "cells": cells,
                "extra": extra,
            }
        )
        day += datetime.timedelta(days=1)
    return rows


def _sheet_context(start: datetime.date, end: datetime.date, resident_id: int) -> dict:
    """Rows for start..end plus where the lazy-loading sentinel should continue from, if anywhere."""
    columns = _grid_columns()
    horizon_end = services.calendar_horizon() + datetime.timedelta(days=1)
    end = min(end, horizon_end)
    return {
        "columns": columns,
        "colspan": len(columns) + 2,
        "rows": _rows(start, end, columns, resident_id),
        "more_from": end if end < horizon_end else None,
    }


@login_required
def sheet_rows(request: HttpRequest) -> HttpResponse:
    """The next chunk of future rows, fetched by htmx as the sheet is scrolled."""
    today = current_datetime().date()
    start = max(_date_param(request, "fra", today), today)
    context = _sheet_context(start, start + datetime.timedelta(days=SHEET_DAYS), current_resident(request).pk)
    return render(request, "kitchen/_duty_rows.html", context)


@login_required
def history(request: HttpRequest) -> HttpResponse:
    """Past shifts, 30 days per page, newest page first."""
    today = current_datetime().date()
    end = min(_date_param(request, "til", today), today)
    start = end - datetime.timedelta(days=SHEET_DAYS)
    first = KitchenShift.objects.order_by("date").values_list("date", flat=True).first()
    columns = _grid_columns()
    rows = _rows(start, end, columns, current_resident(request).pk)
    rows.reverse()
    for row in rows:
        row["month_label"] = ""
    return render(
        request,
        "kitchen/history.html",
        {
            "columns": columns,
            "colspan": len(columns) + 2,
            "rows": rows,
            "start": start,
            "last": end - datetime.timedelta(days=1),
            "older": start if first and first < start else None,
            "newer": end + datetime.timedelta(days=SHEET_DAYS) if end < today else None,
        },
    )


@login_required
def market(request: HttpRequest) -> HttpResponse:
    _backstops()
    resident = current_resident(request)
    now = current_datetime()
    today = now.date()
    # ?til= extends the first render so a #dag- anchor beyond the first 30 days exists.
    # One day past SHEET_DAYS so the free-unenrolment ruler is on the first screen.
    end = max(today + datetime.timedelta(days=SHEET_DAYS + 1), _date_param(request, "til", today))
    for_sale = (
        KitchenAssignment.objects.filter(for_sale=True, shift__starts_at__gt=now, shift__is_disabled=False)
        .select_related("shift", "resident")
        .order_by("shift__starts_at")
    )
    soon = (
        services.open_shifts().filter(starts_at__lte=now + datetime.timedelta(hours=72)).order_by("starts_at")
    )
    return render(
        request,
        "kitchen/market.html",
        {
            "for_sale": for_sale,
            "soon": soon,
            **_sheet_context(today, end, resident.pk),
            "free_unenroll_days": services.FREE_UNENROLL_DAYS,
            "regret_minutes": services.REGRET_WINDOW.seconds // 60,
            "now": now,
            "balance": KitchenPointEntry.balance_for(resident.pk),
            "is_admin": _is_admin(request),
            "can_view_overview": _can_view_overview(request),
        },
    )


def _ms(at: datetime.datetime) -> int:
    return int(at.timestamp() * 1000)


def _horizon_end() -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(services.calendar_horizon(), datetime.time()))


def _forecast_context(resident: Resident, balance: int) -> dict:
    """The balance forecast as a step chart (json_script for frontend/src/kitchen-forecast.ts) and
    the same steps as a table, with the numbers the summary above the chart quotes."""
    now = current_datetime()
    steps = services.forecast(resident)
    end = _horizon_end()
    points: list[dict] = [{"x": _ms(now), "y": balance, "items": []}]
    for step in steps:
        item = [step.label, step.amount]
        if len(points) > 1 and points[-1]["x"] == _ms(step.at):
            points[-1]["y"] = step.balance
            points[-1]["items"].append(item)
        else:
            points.append({"x": _ms(step.at), "y": step.balance, "items": [item]})
    end_balance = steps[-1].balance if steps else balance
    if points[-1]["x"] < _ms(end):
        points.append({"x": _ms(end), "y": end_balance, "items": []})
    # Only worth its own figure when it is below today and not simply where the forecast ends.
    lowest = min(steps, key=lambda s: (s.balance, s.at), default=None)
    if lowest and (lowest.balance >= balance or lowest is steps[-1]):
        lowest = None
    soon = services.balance_at(balance, steps, now + datetime.timedelta(days=services.OUTLOOK_DAYS))
    return {
        "steps": steps,
        "end": end,
        "end_balance": end_balance,
        "end_zone": services.zone(end_balance),
        "lowest": lowest,
        "soon": soon,
        "soon_zone": services.zone(soon),
        "outlook_days": services.OUTLOOK_DAYS,
        "alarm": next((s for s in steps if s.zone == "alarm"), None),
        "chart": {"points": points, "yellowMax": services.YELLOW_MAX, "alarmBelow": services.ALARM_BELOW},
    }


@login_required
def account(request: HttpRequest) -> HttpResponse:
    _backstops()
    resident = current_resident(request)
    balance = KitchenPointEntry.balance_for(resident.pk)
    return render(
        request,
        "kitchen/account.html",
        {
            "resident": resident,
            "balance": balance,
            "balance_zone": services.zone(balance),
            "forecast": _forecast_context(resident, balance),
            "monthly_charge": services.MONTHLY_CHARGE,
            "entries": resident.kitchen_entries.select_related("created_by")[:200],
            "upcoming": resident.kitchen_assignments.filter(shift__ends_at__gt=current_datetime())
            .select_related("shift")
            .order_by("shift__starts_at"),
        },
    )


@login_required
def shift_detail(request: HttpRequest, pk: int) -> HttpResponse:
    shift = get_object_or_404(KitchenShift.objects.prefetch_related(_with_assignees()), pk=pk)
    resident = current_resident(request)
    assignments = list(shift.assignments.all())
    is_admin = _is_admin(request)
    for a in assignments:
        a.can_unenroll = a.resident_id == resident.pk and services.can_unenroll(a)  # type: ignore[attr-defined]
        a.regret_until = (  # type: ignore[attr-defined]
            a.self_enrolled_at + services.REGRET_WINDOW
            if a.can_unenroll  # type: ignore[attr-defined]
            and a.self_enrolled_at
            and shift.starts_at - current_datetime() <= datetime.timedelta(days=services.FREE_UNENROLL_DAYS)
            else None
        )
        a.can_mark_absent = is_admin and services.can_mark_absent(a)  # type: ignore[attr-defined]
    free = shift.free_spots()
    overridable = sum(a.spots - 1 for a in assignments if a.resident_id != resident.pk)
    open_for_enrolment = not shift.is_disabled and not shift.has_started
    return render(
        request,
        "kitchen/shift.html",
        {
            "shift": shift,
            "back_url": _sheet_url(shift),
            "assignments": assignments,
            "mine": next((a for a in assignments if a.resident_id == resident.pk), None),
            "free": free,
            "can_enroll": open_for_enrolment and free + overridable > 0,
            "max_spots": free + overridable,
            "is_admin": is_admin,
            "max_sale_bonus": services.MAX_SALE_BONUS,
            "free_unenroll_days": services.FREE_UNENROLL_DAYS,
            "regret_minutes": services.REGRET_WINDOW.seconds // 60,
        },
    )


@require_POST
@login_required
def enroll(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    shift = get_object_or_404(KitchenShift, pk=pk)
    spots = max(1, _int(request.POST.get("spots"), 1))
    _attempt(request, lambda: services.enroll(shift, current_resident(request), spots), "Du er skrevet op.")
    if request.POST.get("next") == "market":
        return redirect(_sheet_url(shift))
    return redirect("kitchen:shift", pk=pk)


def _own_assignment(request: HttpRequest, pk: int) -> KitchenAssignment:
    return get_object_or_404(
        KitchenAssignment.objects.select_related("shift"), pk=pk, resident=current_resident(request)
    )


@require_POST
@login_required
def unenroll(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    assignment = _own_assignment(request, pk)
    _attempt(request, lambda: services.unenroll(assignment), "Du er meldt fra vagten.")
    return redirect("kitchen:shift", pk=assignment.shift_id)


@require_POST
@login_required
def sell(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    assignment = _own_assignment(request, pk)
    bonus = _int(request.POST.get("bonus"))
    _attempt(request, lambda: services.put_for_sale(assignment, bonus), "Vagten er sat til salg.")
    return redirect("kitchen:shift", pk=assignment.shift_id)


@require_POST
@login_required
def cancel_sale(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    assignment = _own_assignment(request, pk)
    _attempt(request, lambda: services.cancel_sale(assignment), "Vagten er taget af markedet.")
    return redirect("kitchen:shift", pk=assignment.shift_id)


@require_POST
@login_required
def buy(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    assignment = get_object_or_404(KitchenAssignment, pk=pk)
    shift_id = assignment.shift_id
    _attempt(request, lambda: services.buy(assignment, current_resident(request)), "Vagten er din.")
    return redirect("kitchen:shift", pk=shift_id)


# --- administration -------------------------------------------------------------------------------


@require_POST
@role_required(*ADMIN_ROLES)
def mark_absent(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    assignment = get_object_or_404(KitchenAssignment.objects.select_related("shift"), pk=pk)
    raw = (request.POST.get("fine") or "").strip()
    fine = _int(raw, -1) if raw else None
    _attempt(
        request,
        lambda: services.mark_absent(assignment, fine, current_resident(request)),
        "Udeblivelsen er registreret.",
    )
    return redirect("kitchen:shift", pk=assignment.shift_id)


@require_POST
@role_required(*ADMIN_ROLES)
def disable(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    shift = get_object_or_404(KitchenShift, pk=pk)
    reason = request.POST.get("reason", "")
    _attempt(request, lambda: services.disable_shift(shift, reason), "Vagten er aflyst.")
    return redirect("kitchen:shift", pk=pk)


@require_POST
@role_required(*ADMIN_ROLES)
def enable(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    shift = get_object_or_404(KitchenShift, pk=pk)
    _attempt(request, lambda: services.enable_shift(shift), "Vagten er genåbnet.")
    return redirect("kitchen:shift", pk=pk)


@require_POST
@role_required(*ADMIN_ROLES)
def set_bonus(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    shift = get_object_or_404(KitchenShift, pk=pk)
    bonus = _int(request.POST.get("bonus"), -1)
    _attempt(request, lambda: services.set_bonus(shift, bonus), "Bonus er opdateret.")
    return redirect("kitchen:shift", pk=pk)


@role_required(*ADMIN_ROLES)
def admin_overview(request: HttpRequest) -> HttpResponse:
    _backstops()
    form = SpecialShiftForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        shift = form.save(created_by=current_resident(request))
        messages.success(request, "Den særlige vagt er oprettet.")
        return redirect("kitchen:shift", pk=shift.pk)
    return render(request, "kitchen/admin.html", {"form": form})


# --- overview: Køkkengruppen, administrators and Inspektionen ---------------------------------------

SPARK_WIDTH, SPARK_HEIGHT = 160, 40


def _sparkline(
    balance: int,
    steps: list[services.ForecastStep],
    span: tuple[datetime.datetime, datetime.datetime],
    y_range: tuple[int, int],
    check_at: datetime.datetime,
) -> dict:
    """One resident's forecast as an inline SVG step line for the overview's tables. Every row
    shares `y_range`, so the rows compare at a glance; a value outside it is drawn at its edge (the
    figures beside it are exact). The zone edges sit half-way between whole points, as on the big
    chart.

    Coordinates leave here as strings: LANGUAGE_CODE is "da", and a float rendered by the template
    would get a decimal comma, which is no number to SVG."""
    start, end = span
    lo, hi = y_range
    seconds = (end - start).total_seconds() or 1

    def x(at: datetime.datetime) -> float:
        return round(SPARK_WIDTH * min(max((at - start).total_seconds() / seconds, 0), 1), 1)

    def y(value: float) -> float:
        return round(SPARK_HEIGHT * (hi - min(max(value, lo), hi)) / (hi - lo), 1)

    path = f"M0 {y(balance)}" + "".join(f" H{x(s.at)} V{y(s.balance)}" for s in steps) + f" H{SPARK_WIDTH}"
    edges = [hi, services.YELLOW_MAX + 0.5, -0.5, services.ALARM_BELOW - 0.5, lo]
    bands = [
        {"zone": zone, "y": f"{y(top):g}", "height": f"{y(bottom) - y(top):.1f}"}
        for zone, top, bottom in zip(("good", "warn", "neg", "alarm"), edges, edges[1:], strict=False)
        if y(bottom) > y(top)
    ]
    last = steps[-1].balance if steps else balance
    return {
        "path": path,
        "bands": bands,
        "end_y": f"{y(last):g}",
        "end_zone": services.zone(last),
        "check_x": f"{x(check_at):g}",
        "width": SPARK_WIDTH,
        "height": SPARK_HEIGHT,
    }


ZONES = ("good", "warn", "neg", "alarm")


def _distribution(label: str, balances: list[int]) -> dict:
    counts = dict.fromkeys(ZONES, 0)
    for balance in balances:
        counts[services.zone(balance)] += 1
    total = len(balances) or 1
    return {
        "label": label,
        "segments": [
            # A string for the same reason as _sparkline's coordinates: it goes into a style attribute.
            {"zone": zone, "count": counts[zone], "share": f"{100 * counts[zone] / total:.2f}"}
            for zone in ZONES
            if counts[zone]
        ],
    }


@dataclass
class OverviewRow:
    """One resident on the overview. `soon` is the forecast balance OUTLOOK_DAYS ahead."""

    resident: Resident
    steps: list[services.ForecastStep]
    balance: int
    soon: int
    spark: dict = field(default_factory=dict)

    @property
    def zone(self) -> str:
        return services.zone(self.balance)

    @property
    def soon_zone(self) -> str:
        return services.zone(self.soon)

    @property
    def fine(self) -> bool:
        """What the overview's "OK ifølge prognosen" means: back at 0 or more OUTLOOK_DAYS ahead."""
        return self.soon >= 0

    @property
    def end_balance(self) -> int:
        return self.steps[-1].balance if self.steps else self.balance

    @property
    def end_zone(self) -> str:
        return services.zone(self.end_balance)

    @cached_property
    def shifts(self) -> list[services.ForecastStep]:
        return [s for s in self.steps if s.amount > 0]

    @property
    def shift_count(self) -> int:
        return len(self.shifts)

    @property
    def shift_points(self) -> int:
        return sum(s.amount for s in self.shifts)

    @property
    def next_shift(self) -> datetime.datetime | None:
        return self.shifts[0].at if self.shifts else None


@role_required(*VIEW_ROLES)
def dashboard(request: HttpRequest) -> HttpResponse:
    """Everyone on this month's alumneliste: where their balance is and where the forecast takes it,
    with those whose balance is low now at the top."""
    _backstops()
    now = current_datetime()
    soon_at = now + datetime.timedelta(days=services.OUTLOOK_DAYS)
    end = _horizon_end()
    year, month = active_period()
    residents = list(
        Resident.objects.filter(
            pk__in=Residency.objects.filter(year=year, month=month).values("resident_id")
        ).order_by("first_name", "last_name")
    )
    forecasts = services.forecasts(residents)
    balances = KitchenPointEntry.balances()
    rows = []
    for resident in residents:
        steps = forecasts[resident.pk]
        balance = balances.get(resident.pk, 0)
        rows.append(OverviewRow(resident, steps, balance, services.balance_at(balance, steps, soon_at)))
    values = [v for row in rows for v in (row.balance, *(s.balance for s in row.steps))]
    # Tight enough that a 3-point shift is a visible step in a 40px row; -10 and +8 are far enough
    # out that the zone a row ends in is never in doubt.
    y_range = (
        min(max(min(values, default=0) - 1, -10), -3),
        max(min(max(values, default=0) + 1, 8), services.YELLOW_MAX + 3),
    )
    for row in rows:
        row.spark = _sparkline(row.balance, row.steps, (now, end), y_range, soon_at)
    rows.sort(key=lambda row: (row.balance, row.soon, row.resident.full_name))
    return render(
        request,
        "kitchen/dashboard.html",
        {
            "rows": rows,
            "low": [row for row in rows if row.balance <= services.YELLOW_MAX],
            "negative_now": sum(row.balance < 0 for row in rows),
            "alarm_now": sum(row.zone == "alarm" for row in rows),
            "negative_soon": sum(row.soon < 0 for row in rows),
            "alarm_soon": sum(row.soon_zone == "alarm" for row in rows),
            "unmanned": services.unmanned_shifts(datetime.timedelta(days=7)).count(),
            "for_sale": KitchenAssignment.objects.filter(
                for_sale=True, shift__starts_at__gt=now, shift__is_disabled=False
            ).count(),
            "distribution": [
                _distribution("Nu", [row.balance for row in rows]),
                _distribution(f"Om {services.OUTLOOK_DAYS} dage", [row.soon for row in rows]),
                _distribution(
                    f"{end.day}. {MONTHS[end.month]}",
                    [row.end_balance for row in rows],
                ),
            ],
            "outlook_days": services.OUTLOOK_DAYS,
            "end": end,
            "yellow_max": services.YELLOW_MAX,
            "alarm_below": services.ALARM_BELOW,
            "is_admin": _is_admin(request),
        },
    )


@role_required(*VIEW_ROLES)
def resident_overview(request: HttpRequest, pk: int) -> HttpResponse:
    """One resident's forecast, shifts and statement. Køkkengruppen may also post a manual entry
    here; Inspektionen only looks."""
    resident = get_object_or_404(Resident, pk=pk)
    is_admin = _is_admin(request)
    if request.method == "POST":
        if not is_admin:
            raise PermissionDenied
        amount = _int(request.POST.get("amount"))
        message = request.POST.get("message", "")
        _attempt(
            request,
            lambda: services.manual_entry(resident, amount, message, current_resident(request)),
            "Posteringen er bogført.",
        )
        return redirect("kitchen:resident", pk=pk)
    _backstops()
    balance = KitchenPointEntry.balance_for(resident.pk)
    return render(
        request,
        "kitchen/resident.html",
        {
            "resident": resident,
            "balance": balance,
            "balance_zone": services.zone(balance),
            "forecast": _forecast_context(resident, balance),
            "monthly_charge": services.MONTHLY_CHARGE,
            "is_admin": is_admin,
            "entries": resident.kitchen_entries.select_related("created_by")[:200],
            "upcoming": resident.kitchen_assignments.filter(shift__ends_at__gt=current_datetime())
            .select_related("shift")
            .order_by("shift__starts_at"),
        },
    )
