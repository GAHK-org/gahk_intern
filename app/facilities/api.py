"""What other apps may use of facilities: today, begivenheder booking rooms for its events.

An event's booking FOLLOWS the event. The event form picks facilities; on every save events calls
`sync_event_bookings`, which books, moves, retitles and releases to match. Cancelling or deleting the
event calls `release_event_bookings`. events never writes a Booking itself, and this module never
imports events: it reads what it needs off the event object it is handed (pk, title, times).
"""

import datetime
from typing import Any, Protocol

from django.db.models import Q, QuerySet

from audit import trail
from core.clock import current_datetime
from residents.models import Resident

from . import services
from .models import MAX_BOOKING_LENGTH, PRIVATE_EVENT_PURPOSE, Booking, Facility, Status


class BookableEvent(Protocol):
    pk: Any
    title: str
    starts_at: datetime.datetime
    ends_at: datetime.datetime | None


def bookable_facilities(event: BookableEvent | None = None) -> QuerySet[Facility]:
    """What the event form offers: the active facilities, plus any retired one this event still holds
    (so editing the event does not silently drop it)."""
    held = Q()
    if event is not None and event.pk:
        held = Q(pk__in=Booking.objects.holding().filter(event_id=event.pk).values("facility_id"))
    return Facility.objects.filter(Q(is_active=True) | held).order_by("name")


def booked_facility_ids(event: BookableEvent) -> list[int]:
    if not event.pk:
        return []
    return list(Booking.objects.holding().filter(event_id=event.pk).values_list("facility_id", flat=True))


def event_bookings(event: BookableEvent) -> list[Booking]:
    """What the event page shows: what holds a facility for it, and what a manager turned down (so its
    hosts see why the room they picked is not theirs)."""
    held = Booking.objects.filter(event_id=event.pk).holding()
    rejected = Booking.objects.filter(event_id=event.pk, status=Status.REJECTED).exclude(
        facility_id__in=held.values("facility_id")
    )
    return list((held | rejected).select_related("facility").order_by("facility__name", "-pk"))


def booking_problems(
    facilities: list[Facility],
    starts_at: datetime.datetime | None,
    ends_at: datetime.datetime | None,
    event: BookableEvent | None = None,
) -> list[str]:
    """Why the event form cannot book these facilities for that time, in Danish. Empty when it can."""
    if not facilities:
        return []
    if starts_at is None or ends_at is None:
        return ["Angiv hvornår begivenheden slutter, når den skal bruge et lokale."]
    if ends_at - starts_at > MAX_BOOKING_LENGTH:
        return [f"Et lokale kan højst bookes {MAX_BOOKING_LENGTH.days} dage ad gangen."]
    problems = []
    event_pk = event.pk if event is not None else None
    for facility in facilities:
        for clash in services.clashes(facility, starts_at, ends_at, exclude_event_pk=event_pk):
            problems.append(f"{facility} er optaget {services.when(clash)} af {clash.booked_by.full_name}.")
    return problems


def sync_event_bookings(
    event: BookableEvent, facilities: list[Facility], *, public: bool, booked_by: Resident
) -> list[Facility]:
    """Make the event's bookings match `facilities` and its current time and title.

    Returns the facilities that could not be booked because somebody took them between the form's
    check and this save; the caller tells the organiser. A facility needing approval goes back to
    waiting when its time moves, since the yes was for the old time.
    """
    existing = list(Booking.objects.holding().filter(event_id=event.pk).select_related("facility"))
    if not facilities and not existing:
        return []
    starts_at = event.starts_at
    ends_at = event.ends_at
    if ends_at is None:
        if facilities:
            raise ValueError("An event needs an end to book a facility")
        ends_at = starts_at  # only releasing below; never written
    purpose = event.title if public else PRIVATE_EVENT_PURPOSE
    wanted = {f.pk: f for f in facilities}
    failed: list[Facility] = []
    to_request: list[Booking] = []
    now = current_datetime()

    with trail.event("facilities.booking.event_synced", actor=booked_by, only_if_changed=True) as ev:
        ev.details["event"] = event.pk
        services.lock([*facilities, *(b.facility for b in existing)])
        for booking in existing:
            if booking.facility_id not in wanted:
                if booking.ends_at > now:
                    _release(booking, booked_by, now)
                    ev.involve(booking, "released")
                continue
            facility = wanted.pop(booking.facility_id)
            moved = (booking.starts_at, booking.ends_at) != (starts_at, ends_at)
            if moved and services.clashes(facility, starts_at, ends_at, exclude_event_pk=event.pk):
                failed.append(facility)
                continue
            booking.starts_at = starts_at
            booking.ends_at = ends_at
            booking.purpose = purpose
            booking.purpose_public = public
            if moved and booking.status == Status.CONFIRMED:
                booking.status = services.initial_status(facility, booked_by)
                if booking.status == Status.PENDING:
                    to_request.append(booking)
            booking.save()
        for facility in wanted.values():
            if services.clashes(facility, starts_at, ends_at):
                failed.append(facility)
                continue
            booking = Booking.objects.create(
                facility=facility,
                booked_by=booked_by,
                starts_at=starts_at,
                ends_at=ends_at,
                purpose=purpose,
                purpose_public=public,
                event_id=event.pk,
                status=services.initial_status(facility, booked_by),
            )
            ev.involve(booking, "booked")
            if booking.status == Status.PENDING:
                to_request.append(booking)

    for booking in to_request:
        services.notify_request(booking)
    return failed


def release_event_bookings(event: BookableEvent, *, by: Resident | None) -> int:
    """Free whatever the event still holds and has not used yet. Returns how many were released.

    Bookings already over are left as they were: that is the record of the event having happened.
    """
    now = current_datetime()
    upcoming = list(Booking.objects.holding().filter(event_id=event.pk, ends_at__gt=now))
    if not upcoming:
        return 0
    with trail.event("facilities.booking.event_released", actor=by, details={"event": event.pk}) as ev:
        for booking in upcoming:
            _release(booking, by, now)
            ev.involve(booking, "released")
    return len(upcoming)


def _release(booking: Booking, by: Resident | None, now: datetime.datetime) -> None:
    booking.status = Status.CANCELLED
    booking.cancelled_by = by
    booking.cancelled_at = now
    booking.save(update_fields=["status", "cancelled_by", "cancelled_at"])
