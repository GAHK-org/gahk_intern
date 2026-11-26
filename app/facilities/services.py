"""Booking, approving and cancelling — every write to a booking goes through here.

NO DOUBLE BOOKING, and the guarantee is a row lock rather than a check in a form. Two residents
pressing "Book" for the same Saturday at the same moment both pass a form's "is it free?", so every
write that makes a booking hold its facility first locks that facility's row (`_lock`) and asks the
question again under the lock. The second one then waits for the first to commit and sees its row.
"""

import datetime
from collections.abc import Iterable

from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone

from audit import trail
from core.clock import current_datetime
from notifications import categories
from notifications import services as notifications
from notifications.categories import Importance
from residents.models import Resident, Role, RoleAssignment, active_period

from . import access
from .models import Booking, Facility, Status


class BookingConflict(Exception):
    """The facility is already held for part of the requested time."""

    def __init__(self, clashes: list[Booking]) -> None:
        super().__init__("Facility already booked")
        self.clashes = clashes


def clashes(
    facility: Facility,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    *,
    exclude_pk: int | None = None,
    exclude_event_pk: int | None = None,
) -> list[Booking]:
    """The bookings holding `facility` for any part of [starts_at, ends_at)."""
    qs = Booking.objects.filter(facility=facility).holding().overlapping(starts_at, ends_at)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if exclude_event_pk is not None:
        qs = qs.exclude(event_id=exclude_event_pk)
    return list(qs.select_related("booked_by"))


def lock(facilities: Iterable[Facility]) -> None:
    """Lock the facilities' rows until the transaction ends. In pk order, so two writers locking the
    same pair can never wait for each other."""
    list(Facility.objects.select_for_update().filter(pk__in=[f.pk for f in facilities]).order_by("pk"))


def initial_status(facility: Facility, booker: Resident) -> Status:
    """A manager's own booking needs no approval: they would only be approving it themselves."""
    if facility.requires_approval and not access.is_manager(booker):
        return Status.PENDING
    return Status.CONFIRMED


def book(
    facility: Facility,
    booker: Resident,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    purpose: str,
) -> Booking:
    """A private booking. Raises BookingConflict when the time is taken."""
    with trail.event("facilities.booking.created", actor=booker, involve=[(facility, "facility")]) as ev:
        lock([facility])
        taken = clashes(facility, starts_at, ends_at)
        if taken:
            raise BookingConflict(taken)
        booking = Booking.objects.create(
            facility=facility,
            booked_by=booker,
            starts_at=starts_at,
            ends_at=ends_at,
            purpose=purpose,
            status=initial_status(facility, booker),
        )
        ev.involve(booking, "subject")
    if booking.status == Status.PENDING:
        notify_request(booking)
    return booking


def approve(booking: Booking, manager: Resident) -> None:
    with trail.event("facilities.booking.approved", subject=booking, actor=manager):
        booking.status = Status.CONFIRMED
        booking.decided_by = manager
        booking.decided_at = current_datetime()
        booking.save(update_fields=["status", "decided_by", "decided_at"])
    notify_booker(booking, f"Godkendt: {booking.facility}", importance=None)


def reject(booking: Booking, manager: Resident, note: str) -> None:
    with trail.event("facilities.booking.rejected", subject=booking, actor=manager):
        booking.status = Status.REJECTED
        booking.decided_by = manager
        booking.decided_at = current_datetime()
        booking.decision_note = note
        booking.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
    notify_booker(booking, f"Afvist: {booking.facility}", importance=Importance.HOEJ)


def cancel(booking: Booking, by: Resident | None, note: str = "") -> None:
    """Free the facility. The booker is told when somebody else (a manager) did it."""
    with trail.event("facilities.booking.cancelled", subject=booking, actor=by):
        booking.status = Status.CANCELLED
        booking.cancelled_by = by
        booking.cancelled_at = current_datetime()
        booking.decision_note = note
        booking.save(update_fields=["status", "cancelled_by", "cancelled_at", "decision_note"])
    if by is not None and by.pk != booking.booked_by_id:
        notify_booker(booking, f"Aflyst: {booking.facility}", importance=Importance.HOEJ)


def save_facility(facility: Facility, by: Resident) -> Facility:
    action = "facilities.facility.changed" if facility.pk else "facilities.facility.created"
    with trail.event(action, actor=by) as ev:
        facility.save()
        ev.involve(facility, "subject")
    return facility


# --- notifications --------------------------------------------------------------------------------


def booking_url(booking: Booking) -> str:
    return reverse("facilities:booking", args=[booking.pk])


def when(booking: Booking) -> str:
    """ "28.11 kl. 18.00–23.00", or both dates when it runs past midnight."""
    start = timezone.localtime(booking.starts_at)
    end = timezone.localtime(booking.ends_at)
    if start.date() == end.date():
        return f"{start:%d.%m} kl. {start:%H.%M}–{end:%H.%M}"
    return f"{start:%d.%m kl. %H.%M} – {end:%d.%m kl. %H.%M}"


def managers() -> QuerySet[Resident]:
    """Inspektionen and administrators this month, and superusers (who hold every role with no row)."""
    year, month = active_period()
    ids = RoleAssignment.objects.filter(
        year=year, month=month, role__in=[Role.INSPEKTION, Role.ADMINISTRATOR]
    ).values("resident_id")
    return Resident.objects.filter(is_active=True).filter(Q(pk__in=ids) | Q(is_superuser=True))


def notify_request(booking: Booking) -> None:
    """A request waiting for approval goes to the managers."""
    notifications.notify(
        managers(),
        category=categories.GENERAL,
        title=f"Ny booking til godkendelse: {booking.facility}",
        body=f"{booking.booked_by.full_name}, {when(booking)}",
        url=booking_url(booking),
        exclude=booking.booked_by_id,
    )


def notify_booker(booking: Booking, title: str, *, importance: Importance | None) -> None:
    body = when(booking)
    if booking.decision_note:
        body = f"{body} · {booking.decision_note}"
    notifications.notify(
        [booking.booked_by_id],
        category=categories.GENERAL,
        title=title,
        body=body,
        url=booking_url(booking),
        importance=importance,
    )
