"""Køkkenvagter views. Every resident sees the whole calendar and market; Køkkengruppen (and
administrators) get the administrative actions. The rules themselves live in services."""

import datetime
from collections.abc import Callable

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import push
from core.clock import current_datetime
from core.danish import MONTHS, WEEKDAYS_SHORT
from residents.models import Residency, Resident, Role, active_period
from residents.permissions import current_resident, request_has_role, role_required

from . import services
from .forms import SpecialShiftForm
from .models import KitchenAssignment, KitchenPointEntry, KitchenShift

ADMIN_ROLES = (Role.KOKKENGRUPPE, Role.ADMINISTRATOR)


def _is_admin(request: HttpRequest) -> bool:
    return request_has_role(request, *ADMIN_ROLES)


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


def _rows(start: datetime.date, end: datetime.date, columns: list[dict], resident_id: int) -> list[dict]:
    """Sheet rows for the days start..end (exclusive); a month heading opens each new month."""
    shifts = KitchenShift.objects.filter(date__gte=start, date__lt=end).prefetch_related(_with_assignees())
    by_day: dict[datetime.date, list[KitchenShift]] = {}
    for shift in shifts:
        shift.slots = _slots(shift, resident_id)  # type: ignore[attr-defined]
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
            "push_configured": push.is_configured(),
            "vapid_public_key": push.vapid_public_key(),
            "push_subscribed": services.is_subscribed(resident),
        },
    )


@login_required
def account(request: HttpRequest) -> HttpResponse:
    _backstops()
    resident = current_resident(request)
    return render(
        request,
        "kitchen/account.html",
        {
            "resident": resident,
            "balance": KitchenPointEntry.balance_for(resident.pk),
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


@require_POST
@login_required
def save_subscription(request: HttpRequest) -> HttpResponse:
    return push.handle_subscription_request(request, services.TOPIC)


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
    year, month = active_period()
    residents = Resident.objects.filter(
        pk__in=Residency.objects.filter(year=year, month=month).values("resident_id")
    )
    balances = KitchenPointEntry.balances()
    rows = sorted(((r, balances.get(r.pk, 0)) for r in residents), key=lambda t: t[1])
    return render(request, "kitchen/admin.html", {"form": form, "rows": rows})


@role_required(*ADMIN_ROLES)
def admin_resident(request: HttpRequest, pk: int) -> HttpResponse:
    resident = get_object_or_404(Resident, pk=pk)
    if request.method == "POST":
        amount = _int(request.POST.get("amount"))
        message = request.POST.get("message", "")
        _attempt(
            request,
            lambda: services.manual_entry(resident, amount, message, current_resident(request)),
            "Posteringen er bogført.",
        )
        return redirect("kitchen:admin_resident", pk=pk)
    return render(
        request,
        "kitchen/admin_resident.html",
        {
            "resident": resident,
            "balance": KitchenPointEntry.balance_for(resident.pk),
            "entries": resident.kitchen_entries.select_related("created_by")[:200],
        },
    )
