"""Booking: facilities, private bookings, approval, privacy, and bookings that follow an event."""

import datetime
from collections.abc import Callable

import pytest
from django.http import HttpResponse
from django.test import Client
from django.utils import timezone

from audit.models import AuditEvent
from events import services as event_services
from events.models import Event, Visibility
from facilities import api, services
from facilities.models import PRIVATE_EVENT_PURPOSE, Booking, Facility, Status
from notifications import categories
from notifications.models import Notification
from residents.models import Resident, Role

pytestmark = pytest.mark.django_db

FACILITIES = "/facilities/"
EVENTS = "/begivenheder/"


def local(moment: datetime.datetime) -> str:
    return timezone.localtime(moment).strftime("%Y-%m-%dT%H:%M")


def at(days: int, hour: int) -> datetime.datetime:
    """`days` from now at a round local hour, minute precision like the form."""
    day = timezone.localdate() + datetime.timedelta(days=days)
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour)))


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")


@pytest.fixture
def other(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="b@gahk.dk", first_name="Mette", last_name="Hansen")


@pytest.fixture
def inspektion(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="i@gahk.dk", first_name="Ida", last_name="Inspektør", roles=(Role.INSPEKTION,))


@pytest.fixture
def festsal() -> Facility:
    return Facility.objects.create(name="Festsalen", capacity=120, requires_approval=True)


@pytest.fixture
def guest_room() -> Facility:
    return Facility.objects.create(name="Gæsteværelset")


def book_form(
    client: Client,
    facility: Facility,
    start: datetime.datetime,
    end: datetime.datetime,
    purpose: str = "Forældre på besøg",
) -> HttpResponse:
    return client.post(
        f"{FACILITIES}{facility.pk}/book/",
        {"starts_at": local(start), "ends_at": local(end), "purpose": purpose},
    )


def told() -> list[tuple[int, str]]:
    return sorted(
        Notification.objects.filter(category=categories.GENERAL).values_list("recipient_id", "title")
    )


# --- private bookings ------------------------------------------------------------------------------


def test_a_resident_books_a_free_facility(client: Client, beboer: Resident, guest_room: Facility) -> None:
    client.force_login(beboer)
    response = book_form(client, guest_room, at(2, 14), at(3, 11))

    booking = Booking.objects.get()
    assert response["Location"] == f"{FACILITIES}bookings/{booking.pk}/"
    assert (booking.booked_by, booking.status, booking.purpose) == (
        beboer,
        Status.CONFIRMED,
        "Forældre på besøg",
    )
    assert AuditEvent.objects.filter(action="facilities.booking.created", actor=beboer).exists()


def test_an_overlapping_booking_is_refused(
    client: Client, beboer: Resident, other: Resident, guest_room: Facility
) -> None:
    services.book(guest_room, other, at(2, 14), at(2, 18), "Kæreste")
    client.force_login(beboer)

    response = book_form(client, guest_room, at(2, 17), at(2, 20))

    assert Booking.objects.count() == 1
    assert "er allerede booket" in response.content.decode()
    assert "Mette Hansen" in response.content.decode()


def test_touching_bookings_are_allowed(beboer: Resident, other: Resident, guest_room: Facility) -> None:
    services.book(guest_room, other, at(2, 14), at(2, 18), "Før")
    services.book(guest_room, beboer, at(2, 18), at(2, 22), "Efter")

    assert Booking.objects.holding().count() == 2


def test_a_cancelled_or_rejected_booking_frees_the_time(
    beboer: Resident, other: Resident, inspektion: Resident, festsal: Facility, guest_room: Facility
) -> None:
    cancelled = services.book(guest_room, other, at(2, 14), at(2, 18), "Aflyst")
    services.cancel(cancelled, other)
    rejected = services.book(festsal, other, at(2, 14), at(2, 18), "Afvist")
    services.reject(rejected, inspektion, "")

    services.book(guest_room, beboer, at(2, 15), at(2, 17), "Nu min")
    services.book(festsal, beboer, at(2, 15), at(2, 17), "Også min")


def test_no_booking_in_the_past_or_back_to_front_or_too_long(
    client: Client, beboer: Resident, guest_room: Facility
) -> None:
    client.force_login(beboer)
    book_form(client, guest_room, at(-1, 14), at(-1, 16))
    book_form(client, guest_room, at(2, 16), at(2, 14))
    book_form(client, guest_room, at(2, 16), at(20, 14))

    assert not Booking.objects.exists()


def test_a_retired_facility_cannot_be_booked(client: Client, beboer: Resident, guest_room: Facility) -> None:
    guest_room.is_active = False
    guest_room.save()
    client.force_login(beboer)

    assert book_form(client, guest_room, at(2, 14), at(2, 16)).status_code == 404


# --- approval --------------------------------------------------------------------------------------


def test_a_facility_needing_approval_waits_and_tells_the_managers(
    client: Client, beboer: Resident, inspektion: Resident, festsal: Facility
) -> None:
    client.force_login(beboer)
    book_form(client, festsal, at(5, 18), at(5, 23), "Fødselsdag")

    booking = Booking.objects.get()
    assert booking.status == Status.PENDING
    assert told() == [(inspektion.pk, "Ny booking til godkendelse: Festsalen")]


def test_a_pending_request_holds_the_time(beboer: Resident, other: Resident, festsal: Facility) -> None:
    services.book(festsal, other, at(5, 18), at(5, 23), "Først")

    with pytest.raises(services.BookingConflict):
        services.book(festsal, beboer, at(5, 20), at(5, 22), "Bagefter")


def test_a_manager_approves_and_the_booker_is_told(
    client: Client, beboer: Resident, inspektion: Resident, festsal: Facility
) -> None:
    booking = services.book(festsal, beboer, at(5, 18), at(5, 23), "Fødselsdag")
    Notification.objects.all().delete()
    client.force_login(inspektion)

    client.post(f"{FACILITIES}bookings/{booking.pk}/approve/")

    booking.refresh_from_db()
    assert (booking.status, booking.decided_by) == (Status.CONFIRMED, inspektion)
    assert told() == [(beboer.pk, "Godkendt: Festsalen")]


def test_a_manager_rejects_with_a_reason(
    client: Client, beboer: Resident, inspektion: Resident, festsal: Facility
) -> None:
    booking = services.book(festsal, beboer, at(5, 18), at(5, 23), "Fødselsdag")
    Notification.objects.all().delete()
    client.force_login(inspektion)

    client.post(
        f"{FACILITIES}bookings/{booking.pk}/reject/", {"note": "Gulvet bliver lakeret", "next": "requests"}
    )

    booking.refresh_from_db()
    assert (booking.status, booking.decision_note) == (Status.REJECTED, "Gulvet bliver lakeret")
    assert Notification.objects.get(recipient=beboer).body.endswith("Gulvet bliver lakeret")


def test_a_resident_cannot_approve(
    client: Client, beboer: Resident, other: Resident, festsal: Facility
) -> None:
    booking = services.book(festsal, other, at(5, 18), at(5, 23), "Fest")
    client.force_login(beboer)

    assert client.post(f"{FACILITIES}bookings/{booking.pk}/approve/").status_code == 403
    assert client.get(f"{FACILITIES}requests/").status_code == 403
    booking.refresh_from_db()
    assert booking.status == Status.PENDING


def test_a_managers_own_booking_needs_no_approval(inspektion: Resident, festsal: Facility) -> None:
    booking = services.book(festsal, inspektion, at(5, 18), at(5, 23), "Inspektionsmøde")

    assert booking.status == Status.CONFIRMED


def test_the_requests_page_lists_what_waits(
    client: Client, beboer: Resident, inspektion: Resident, festsal: Facility
) -> None:
    services.book(festsal, beboer, at(5, 18), at(5, 23), "Fødselsdag")
    client.force_login(inspektion)

    page = client.get(f"{FACILITIES}requests/").content.decode()

    assert "Fødselsdag" in page and "Anton Storgaard" in page


# --- what others see -------------------------------------------------------------------------------


def test_others_see_who_and_when_but_not_why(
    client: Client, beboer: Resident, other: Resident, inspektion: Resident, guest_room: Facility
) -> None:
    booking = services.book(guest_room, beboer, at(1, 14), at(1, 18), "Hemmelig date")

    client.force_login(other)
    week = client.get(f"{FACILITIES}{guest_room.pk}/").content.decode()
    detail = client.get(f"{FACILITIES}bookings/{booking.pk}/").content.decode()
    assert "Anton Storgaard" in week and "Anton Storgaard" in detail
    assert "Hemmelig date" not in week and "Hemmelig date" not in detail

    client.force_login(inspektion)
    assert "Hemmelig date" in client.get(f"{FACILITIES}bookings/{booking.pk}/").content.decode()
    client.force_login(beboer)
    assert "Hemmelig date" in client.get(f"{FACILITIES}{guest_room.pk}/").content.decode()


def test_a_booking_past_midnight_shows_on_both_days(
    client: Client, beboer: Resident, guest_room: Facility
) -> None:
    today = timezone.localdate()
    monday = today + datetime.timedelta(days=7 - today.weekday())
    start = timezone.make_aware(datetime.datetime.combine(monday, datetime.time(20)))
    services.book(guest_room, beboer, start, start + datetime.timedelta(hours=14), "Overnatning")
    client.force_login(beboer)

    week = client.get(f"{FACILITIES}{guest_room.pk}/?week={monday.isoformat()}").content.decode()

    assert week.count("Overnatning") == 2
    assert f"20.00&ndash;{monday + datetime.timedelta(days=1):%d.%m}" in week
    assert f"{monday:%d.%m}&ndash;10.00" in week


def test_the_overview_says_what_is_free(
    client: Client, beboer: Resident, other: Resident, guest_room: Facility
) -> None:
    services.book(
        guest_room,
        other,
        timezone.now() - datetime.timedelta(hours=1),
        timezone.now() + datetime.timedelta(hours=2),
        "Nu",
    )
    client.force_login(beboer)

    page = client.get(FACILITIES).content.decode()

    assert "Optaget til" in page and "Mette Hansen" in page


# --- cancelling ------------------------------------------------------------------------------------


def test_the_booker_cancels_their_own(client: Client, beboer: Resident, guest_room: Facility) -> None:
    booking = services.book(guest_room, beboer, at(2, 14), at(2, 18), "Besøg")
    client.force_login(beboer)

    client.post(f"{FACILITIES}bookings/{booking.pk}/cancel/")

    booking.refresh_from_db()
    assert (booking.status, booking.cancelled_by) == (Status.CANCELLED, beboer)
    assert not told()


def test_nobody_else_cancels_it_but_a_manager_who_tells_the_booker(
    client: Client, beboer: Resident, other: Resident, inspektion: Resident, guest_room: Facility
) -> None:
    booking = services.book(guest_room, beboer, at(2, 14), at(2, 18), "Besøg")

    client.force_login(other)
    assert client.post(f"{FACILITIES}bookings/{booking.pk}/cancel/").status_code == 403

    client.force_login(inspektion)
    client.post(f"{FACILITIES}bookings/{booking.pk}/cancel/", {"note": "Vandskade"})
    booking.refresh_from_db()
    assert (booking.status, booking.cancelled_by, booking.decision_note) == (
        Status.CANCELLED,
        inspektion,
        "Vandskade",
    )
    assert told() == [(beboer.pk, "Aflyst: Gæsteværelset")]


# --- managing facilities ---------------------------------------------------------------------------


def test_a_manager_creates_and_retires_a_facility(client: Client, inspektion: Resident) -> None:
    client.force_login(inspektion)
    client.post(
        f"{FACILITIES}new/",
        {"name": "Musiklokalet", "capacity": "8", "requires_approval": "on", "is_active": "on"},
    )

    facility = Facility.objects.get()
    assert (facility.name, facility.capacity, facility.requires_approval, facility.is_active) == (
        "Musiklokalet",
        8,
        True,
        True,
    )
    assert AuditEvent.objects.filter(action="facilities.facility.created").exists()

    client.post(f"{FACILITIES}{facility.pk}/edit/", {"name": "Musiklokalet", "capacity": "8"})
    facility.refresh_from_db()
    assert not facility.is_active


def test_a_resident_cannot_manage_facilities(client: Client, beboer: Resident, guest_room: Facility) -> None:
    client.force_login(beboer)

    assert client.post(f"{FACILITIES}new/", {"name": "Mit eget"}).status_code == 403
    assert client.get(f"{FACILITIES}{guest_room.pk}/edit/").status_code == 403
    assert client.get(f"{FACILITIES}{guest_room.pk}/history/").status_code == 403


@pytest.mark.parametrize("path", ["", "mine/", "1/", "1/book/", "bookings/1/"])
def test_every_page_needs_a_login(client: Client, path: str) -> None:
    assert client.get(FACILITIES + path).status_code == 302


# --- events ----------------------------------------------------------------------------------------


def event_form(
    start: datetime.datetime, end: datetime.datetime | None, *facilities: Facility, **extra: str
) -> dict:
    data = {
        "title": "Julefrokost",
        "description": "",
        "location": "",
        "starts_at": local(start),
        "ends_at": local(end) if end else "",
        "visibility": Visibility.AABENT,
        "facilities": [str(f.pk) for f in facilities],
    }
    data.update(extra)
    return data


def test_an_event_books_its_rooms(
    client: Client, beboer: Resident, other: Resident, guest_room: Facility
) -> None:
    client.force_login(beboer)

    client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), guest_room))

    event = Event.objects.get()
    booking = Booking.objects.get()
    assert (booking.event_id, booking.booked_by, booking.status) == (event.pk, beboer, Status.CONFIRMED)
    assert (booking.starts_at, booking.ends_at) == (event.starts_at, event.ends_at)
    assert (booking.purpose, booking.purpose_public) == ("Julefrokost", True)
    assert event.location == "Gæsteværelset"
    # An open event's title is everybody's, so it shows on the facility's week.
    client.force_login(other)
    assert (
        "Julefrokost"
        in client.get(f"{FACILITIES}{guest_room.pk}/?week={at(4, 0).date().isoformat()}").content.decode()
    )
    assert "Gæsteværelset" in client.get(f"{EVENTS}{event.pk}").content.decode()


def test_an_event_needs_an_end_to_book_a_room(client: Client, beboer: Resident, guest_room: Facility) -> None:
    client.force_login(beboer)

    response = client.post(EVENTS + "opret", event_form(at(4, 18), None, guest_room))

    assert not Event.objects.exists()
    assert "Angiv hvornår begivenheden slutter" in response.content.decode()


def test_an_event_cannot_take_a_booked_room(
    client: Client, beboer: Resident, other: Resident, guest_room: Facility
) -> None:
    services.book(guest_room, other, at(4, 20), at(5, 10), "Gæst")
    client.force_login(beboer)

    response = client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), guest_room))

    assert not Event.objects.exists()
    assert "Gæsteværelset er optaget" in response.content.decode()


def test_moving_the_event_moves_the_booking_and_asks_again(
    client: Client, beboer: Resident, inspektion: Resident, festsal: Facility
) -> None:
    client.force_login(beboer)
    client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), festsal))
    event = Event.objects.get()
    booking = Booking.objects.get()
    assert booking.status == Status.PENDING
    services.approve(booking, inspektion)
    Notification.objects.all().delete()

    client.post(f"{EVENTS}{event.pk}/rediger", event_form(at(5, 18), at(5, 23), festsal))

    booking.refresh_from_db()
    assert (booking.starts_at, booking.ends_at) == (at(5, 18), at(5, 23))
    assert booking.status == Status.PENDING
    assert told() == [(inspektion.pk, "Ny booking til godkendelse: Festsalen")]


def test_editing_without_moving_keeps_the_approval(
    client: Client, beboer: Resident, inspektion: Resident, festsal: Facility
) -> None:
    client.force_login(beboer)
    client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), festsal))
    event = Event.objects.get()
    services.approve(Booking.objects.get(), inspektion)

    client.post(
        f"{EVENTS}{event.pk}/rediger", event_form(at(4, 18), at(4, 23), festsal, title="Julefrokost 2.0")
    )

    booking = Booking.objects.get()
    assert (booking.status, booking.purpose) == (Status.CONFIRMED, "Julefrokost 2.0")


def test_unticking_a_room_releases_it(
    client: Client, beboer: Resident, guest_room: Facility, festsal: Facility
) -> None:
    client.force_login(beboer)
    client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), guest_room))
    event = Event.objects.get()

    client.post(f"{EVENTS}{event.pk}/rediger", event_form(at(4, 18), at(4, 23)))

    assert Booking.objects.get().status == Status.CANCELLED
    assert api.booked_facility_ids(event) == []


def test_cancelling_the_event_frees_the_room(client: Client, beboer: Resident, guest_room: Facility) -> None:
    client.force_login(beboer)
    client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), guest_room))
    event = Event.objects.get()

    client.post(f"{EVENTS}{event.pk}/aflys")

    booking = Booking.objects.get()
    assert (booking.status, booking.cancelled_by) == (Status.CANCELLED, beboer)


def test_deleting_the_event_frees_the_room_and_keeps_the_record(
    client: Client, beboer: Resident, guest_room: Facility
) -> None:
    client.force_login(beboer)
    client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), guest_room))
    event = Event.objects.get()

    client.post(f"{EVENTS}{event.pk}/slet")

    booking = Booking.objects.get()
    assert (booking.status, booking.event_id, booking.purpose) == (Status.CANCELLED, None, "Julefrokost")


def test_the_purge_keeps_a_held_events_booking(beboer: Resident, guest_room: Facility) -> None:
    event = Event.objects.create(
        organiser=beboer, title="Gammel fest", starts_at=at(-20, 18), ends_at=at(-20, 23)
    )
    Booking.objects.create(
        facility=guest_room,
        booked_by=beboer,
        starts_at=event.starts_at,
        ends_at=event.ends_at,  # type: ignore[misc]
        purpose=event.title,
        purpose_public=True,
        event=event,
    )

    Event.objects.purge_expired()

    booking = Booking.objects.get()
    assert (booking.status, booking.event_id, booking.purpose) == (Status.CONFIRMED, None, "Gammel fest")


def test_a_private_events_booking_hides_its_title(
    beboer: Resident, inspektion: Resident, guest_room: Facility
) -> None:
    """Managers see every booking's purpose, and a private event promises they cannot see it."""
    event = Event.objects.create(
        organiser=beboer,
        title="Hemmelig fest",
        starts_at=at(4, 18),
        ends_at=at(4, 23),
        visibility=Visibility.KUN_INVITEREDE,
    )

    event_services.sync_facilities(event, [guest_room], booked_by=beboer)

    booking = Booking.objects.get()
    assert (booking.purpose, booking.purpose_public) == (PRIVATE_EVENT_PURPOSE, False)


def test_a_room_taken_between_check_and_save_is_reported(
    beboer: Resident, other: Resident, guest_room: Facility
) -> None:
    event = Event.objects.create(organiser=beboer, title="Fest", starts_at=at(4, 18), ends_at=at(4, 23))
    services.book(guest_room, other, at(4, 22), at(5, 10), "Snuppet")

    missed = event_services.sync_facilities(event, [guest_room], booked_by=beboer)

    assert missed == [guest_room]
    assert api.booked_facility_ids(event) == []


def test_an_event_booking_is_changed_through_the_event(
    client: Client, beboer: Resident, guest_room: Facility
) -> None:
    client.force_login(beboer)
    client.post(EVENTS + "opret", event_form(at(4, 18), at(4, 23), guest_room))
    booking = Booking.objects.get()

    assert client.post(f"{FACILITIES}bookings/{booking.pk}/cancel/").status_code == 403
    assert (
        "Ret eller aflys begivenheden" in client.get(f"{FACILITIES}bookings/{booking.pk}/").content.decode()
    )


def test_the_event_form_has_no_room_field_without_facilities(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)

    assert "Book lokaler" not in client.get(EVENTS + "opret").content.decode()
