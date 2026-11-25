"""Booking's pages. Open to every resident; the managers' pages and actions check access.request_is_manager."""

import datetime
from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.clock import current_date, current_datetime
from core.danish import MONTHS
from residents.models import Resident
from residents.permissions import current_resident

from . import access, services
from .forms import BookingForm, DecisionNoteForm, FacilityForm
from .models import Booking, Facility, Status

WEEKDAYS = ("Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag")
PAGE_SIZE = 25


def _decorate(bookings: list[Booking], resident: Resident, manager: bool) -> list[Booking]:
    """Attach what the templates may show of each booking: the purpose only to those who may read it."""
    for b in bookings:
        b.shown_purpose = b.purpose if access.can_see_purpose(b, resident, manager=manager) else ""  # type: ignore[attr-defined]
        b.is_mine = b.booked_by_id == resident.pk  # type: ignore[attr-defined]
    return bookings


@login_required
def index(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    manager = access.request_is_manager(request)
    now = current_datetime()
    facilities = list(Facility.objects.filter(is_active=True))
    upcoming = _decorate(
        list(
            Booking.objects.holding()
            .filter(facility__in=facilities, ends_at__gt=now)
            .select_related("booked_by", "facility")
        ),
        resident,
        manager,
    )
    for facility in facilities:
        mine = [b for b in upcoming if b.facility_id == facility.pk]
        facility.current = next((b for b in mine if b.starts_at <= now), None)  # type: ignore[attr-defined]
        facility.next = next((b for b in mine if b.starts_at > now), None)  # type: ignore[attr-defined]
    return render(
        request,
        "facilities/index.html",
        {
            "facilities": facilities,
            "my_bookings": [b for b in upcoming if b.booked_by_id == resident.pk][:5],
            "is_manager": manager,
            "pending_count": Booking.objects.filter(status=Status.PENDING, ends_at__gt=now).count()
            if manager
            else 0,
            "retired": Facility.objects.filter(is_active=False) if manager else [],
        },
    )


def _week_start(request: HttpRequest) -> datetime.date:
    today = current_date()
    try:
        chosen = datetime.date.fromisoformat(request.GET.get("week", ""))
    except ValueError:
        chosen = today
    return chosen - datetime.timedelta(days=chosen.weekday())


def _midnight(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time()))


@dataclass
class Segment:
    """One booking as one day shows it: where it sits on that day's 24-hour bar (SVG x and width in
    hours: attributes, not inline styles, so a strict style-src cannot break them) and the times that
    day shows. A booking past midnight is a segment on each day it touches."""

    booking: Booking
    x: float
    width: float
    starts: str
    ends: str


def _segment(booking: Booking, day: datetime.date) -> Segment:
    day_start, day_end = _midnight(day), _midnight(day + datetime.timedelta(days=1))
    start, end = max(booking.starts_at, day_start), min(booking.ends_at, day_end)
    local_start, local_end = timezone.localtime(booking.starts_at), timezone.localtime(booking.ends_at)
    return Segment(
        booking=booking,
        x=round((start - day_start).total_seconds() / 3600, 3),
        width=max(round((end - start).total_seconds() / 3600, 3), 0.25),
        starts=f"{local_start:%H.%M}" if local_start.date() == day else f"{local_start:%d.%m}",
        ends=f"{local_end:%H.%M}" if local_end.date() == day else f"{local_end:%d.%m}",
    )


@dataclass
class Day:
    date: datetime.date
    label: str
    segments: list[Segment]
    is_today: bool
    is_past: bool


@login_required
def facility(request: HttpRequest, pk: int) -> HttpResponse:
    """One facility's week, Monday to Sunday, one bar per day."""
    resident = current_resident(request)
    manager = access.request_is_manager(request)
    fac = get_object_or_404(Facility, pk=pk)
    monday = _week_start(request)
    week_end = monday + datetime.timedelta(days=7)
    bookings = _decorate(
        list(
            Booking.objects.filter(facility=fac)
            .holding()
            .overlapping(_midnight(monday), _midnight(week_end))
            .select_related("booked_by")
        ),
        resident,
        manager,
    )
    today = current_date()
    days = []
    for offset in range(7):
        day = monday + datetime.timedelta(days=offset)
        todays = [
            _segment(b, day)
            for b in bookings
            if b.starts_at < _midnight(day + datetime.timedelta(days=1)) and b.ends_at > _midnight(day)
        ]
        days.append(
            Day(
                date=day,
                label=f"{WEEKDAYS[day.weekday()]} {day.day}. {MONTHS[day.month]}",
                segments=todays,
                is_today=day == today,
                is_past=day < today,
            )
        )
    iso_year, iso_week, _ = monday.isocalendar()
    return render(
        request,
        "facilities/facility.html",
        {
            "facility": fac,
            "days": days,
            "week_number": iso_week,
            "week_year": iso_year,
            "prev_week": (monday - datetime.timedelta(days=7)).isoformat(),
            "next_week": week_end.isoformat(),
            "this_week": monday <= today < week_end,
            "is_manager": manager,
            "hours": (0, 6, 12, 18, 24),
        },
    )


@login_required
def book(request: HttpRequest, pk: int) -> HttpResponse:
    resident = current_resident(request)
    fac = get_object_or_404(Facility, pk=pk, is_active=True)
    if request.method != "POST":
        initial = {}
        try:
            day = datetime.date.fromisoformat(request.GET.get("date", ""))
            initial = {
                "starts_at": datetime.datetime.combine(day, datetime.time(18)),
                "ends_at": datetime.datetime.combine(day, datetime.time(22)),
            }
        except ValueError:
            pass
        form = BookingForm(initial=initial, facility=fac)
        return render(request, "facilities/book.html", {"form": form, "facility": fac})

    form = BookingForm(request.POST, facility=fac)
    if form.is_valid():
        try:
            booking = services.book(
                fac,
                resident,
                form.cleaned_data["starts_at"],
                form.cleaned_data["ends_at"],
                form.cleaned_data["purpose"],
            )
        except services.BookingConflict as conflict:
            for clash in conflict.clashes:
                form.add_error(
                    None, f"{fac} er allerede booket {services.when(clash)} af {clash.booked_by.full_name}."
                )
        else:
            if booking.status == Status.PENDING:
                messages.success(request, "Din booking venter nu på, at Inspektionen godkender den.")
            else:
                messages.success(request, f"{fac} er booket.")
            return redirect("facilities:booking", pk=booking.pk)
    return render(request, "facilities/book.html", {"form": form, "facility": fac})


def _booking(pk: int) -> Booking:
    return get_object_or_404(
        Booking.objects.select_related("facility", "booked_by", "decided_by", "cancelled_by"), pk=pk
    )


@login_required
def booking(request: HttpRequest, pk: int) -> HttpResponse:
    resident = current_resident(request)
    manager = access.request_is_manager(request)
    b = _booking(pk)
    _decorate([b], resident, manager)
    return render(
        request,
        "facilities/booking.html",
        {
            "booking": b,
            "is_manager": manager,
            "can_cancel": access.can_cancel(b, resident, manager=manager),
            "can_decide": access.can_decide(b, manager=manager),
            # The event's link is for whoever may already know about it: an open event is everybody's.
            "show_event": b.event_id is not None and (b.purpose_public or b.booked_by_id == resident.pk),
            "note_form": DecisionNoteForm(),
        },
    )


@require_POST
@login_required
def cancel(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    resident = current_resident(request)
    manager = access.request_is_manager(request)
    b = _booking(pk)
    if not access.can_cancel(b, resident, manager=manager):
        raise PermissionDenied
    form = DecisionNoteForm(request.POST)
    note = form.cleaned_data["note"] if form.is_valid() else ""
    services.cancel(b, resident, note if b.booked_by_id != resident.pk else "")
    messages.success(request, "Bookingen er aflyst.")
    return redirect("facilities:booking", pk=b.pk)


@require_POST
@login_required
def approve(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    b = _booking(pk)
    if not access.can_decide(b, manager=access.request_is_manager(request)):
        raise PermissionDenied
    services.approve(b, current_resident(request))
    messages.success(request, "Bookingen er godkendt.")
    return _after_decision(request, b)


@require_POST
@login_required
def reject(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    b = _booking(pk)
    if not access.can_decide(b, manager=access.request_is_manager(request)):
        raise PermissionDenied
    form = DecisionNoteForm(request.POST)
    services.reject(b, current_resident(request), form.cleaned_data["note"] if form.is_valid() else "")
    messages.success(request, "Bookingen er afvist.")
    return _after_decision(request, b)


def _after_decision(request: HttpRequest, b: Booking) -> HttpResponseRedirect:
    """Back to the list of requests when decided from there, otherwise to the booking."""
    if request.POST.get("next") == "requests":
        return redirect("facilities:requests")
    return redirect("facilities:booking", pk=b.pk)


@login_required
def mine(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    now = current_datetime()
    mine_qs = Booking.objects.filter(booked_by=resident).select_related("facility", "booked_by")
    upcoming = list(mine_qs.filter(ends_at__gt=now).exclude(status=Status.CANCELLED))
    past = Paginator(
        mine_qs.filter(Q(ends_at__lte=now) | Q(status=Status.CANCELLED)).order_by("-starts_at"), PAGE_SIZE
    )
    page = past.get_page(request.GET.get("page"))
    _decorate(upcoming, resident, manager=False)
    _decorate(list(page), resident, manager=False)
    return render(request, "facilities/mine.html", {"upcoming": upcoming, "page_obj": page})


def _manager_only(request: HttpRequest) -> None:
    if not access.request_is_manager(request):
        raise PermissionDenied


@login_required
def requests(request: HttpRequest) -> HttpResponse:
    _manager_only(request)
    resident = current_resident(request)
    pending = _decorate(
        list(
            Booking.objects.filter(status=Status.PENDING, ends_at__gt=current_datetime()).select_related(
                "facility", "booked_by"
            )
        ),
        resident,
        manager=True,
    )
    return render(request, "facilities/requests.html", {"pending": pending, "note_form": DecisionNoteForm()})


@login_required
def history(request: HttpRequest, pk: int) -> HttpResponse:
    """Every booking a facility has had, newest first. For managers: "who had it that night?"."""
    _manager_only(request)
    fac = get_object_or_404(Facility, pk=pk)
    qs = Booking.objects.filter(facility=fac).select_related("booked_by").order_by("-starts_at", "-pk")
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    _decorate(list(page), current_resident(request), manager=True)
    return render(request, "facilities/history.html", {"facility": fac, "page_obj": page})


@login_required
def create_facility(request: HttpRequest) -> HttpResponse:
    _manager_only(request)
    form = FacilityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        fac = services.save_facility(form.save(commit=False), current_resident(request))
        messages.success(request, f"{fac} er oprettet.")
        return redirect("facilities:facility", pk=fac.pk)
    return render(request, "facilities/facility_form.html", {"form": form})


@login_required
def edit_facility(request: HttpRequest, pk: int) -> HttpResponse:
    _manager_only(request)
    fac = get_object_or_404(Facility, pk=pk)
    form = FacilityForm(request.POST or None, instance=fac)
    if request.method == "POST" and form.is_valid():
        services.save_facility(form.save(commit=False), current_resident(request))
        messages.success(request, "Gemt.")
        return redirect("facilities:facility", pk=fac.pk)
    return render(
        request,
        "facilities/facility_form.html",
        {
            "form": form,
            "facility": fac,
            "booking_count": Booking.objects.filter(facility=fac).aggregate(n=Count("pk"))["n"],
        },
    )
