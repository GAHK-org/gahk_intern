"""What begivenheder leaves in the audit trail (events.auditing)."""

import datetime
from collections.abc import Callable

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from audit.models import AuditChange
from core.models import Room
from events import access, services
from events.models import RETENTION_AFTER_END, Answer, CalendarFeedToken, Event, EventComment, Visibility
from residents.models import Residency, Resident, active_period

from .audit_support import actions, changes, events

pytestmark = pytest.mark.django_db

EVENTS = "/begivenheder/"


@pytest.fixture(autouse=True)
def rollout_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


@pytest.fixture
def anton(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")


@pytest.fixture
def mette(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="b@gahk.dk", first_name="Mette", last_name="Hansen")


def local(moment: datetime.datetime) -> str:
    return timezone.localtime(moment).strftime("%Y-%m-%dT%H:%M")


def make_event(organiser: Resident, **extra: object) -> Event:
    starts = (timezone.now() + datetime.timedelta(days=3)).replace(second=0, microsecond=0)
    return Event.objects.create(
        organiser=organiser, **{"title": "Fællesspisning", "starts_at": starts, **extra}
    )


def test_creating_editing_and_cancelling_an_event_are_recorded(
    client: Client, anton: Resident, mette: Resident
) -> None:
    year, month = active_period()
    room = Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=mette, room=room, year=year, month=month)  # so she can be invited
    client.force_login(anton)
    starts = timezone.now() + datetime.timedelta(days=2)
    form = {
        "title": "Filmaften",
        "location": "TV-stuen",
        "starts_at": local(starts),
        "visibility": Visibility.AABENT,
        "invitees": [mette.pk],
    }
    client.post(EVENTS + "opret", form)
    event = Event.objects.get()
    client.post(f"{EVENTS}{event.pk}/rediger", {**form, "title": "Filmaften med popcorn", "invitees": []})
    client.post(f"{EVENTS}{event.pk}/rediger", {**form, "title": "Filmaften med popcorn", "invitees": []})
    client.post(f"{EVENTS}{event.pk}/aflys")

    assert actions("events.") == ["events.event.created", "events.event.edited", "events.event.cancelled"]
    created = events("events.event.created").get()
    assert created.actor_id == anton.pk
    assert changes(created)["title"] == [None, "Filmaften"]
    invite = created.entities.get(kind="events.invite")
    assert (invite.operation, invite.name) == ("created", "Mette Hansen: Filmaften")

    edited = events("events.event.edited").get()
    # The SEQUENCE bump and the Redigeret stamp are kept by code; the title change is what was done.
    assert changes(edited) == {"title": ["Filmaften", "Filmaften med popcorn"]}
    assert edited.entities.get(kind="events.invite").operation == "deleted"

    cancelled = events("events.event.cancelled").get()
    assert set(changes(cancelled)) == {"cancelled_at"}


def test_an_answer_is_recorded_once_and_names_whom_it_let_in(
    anton: Resident, mette: Resident, make_resident: Callable[..., Resident]
) -> None:
    third = make_resident(email="c@gahk.dk", first_name="Anders", last_name="Bo")
    event = make_event(anton, capacity=1)
    services.set_answer(event.pk, mette, Answer.JA)
    services.set_answer(event.pk, mette, Answer.JA)  # the same answer again: nothing new
    services.set_answer(event.pk, third, Answer.JA)  # on the venteliste
    services.set_answer(event.pk, mette, Answer.NEJ)  # frees the seat for Anders

    answered = list(events("events.rsvp.answered"))
    assert len(answered) == 3
    assert answered[0].details == {"answer": "ja"}
    assert answered[0].entities.get(role="resident").entity_id == mette.pk
    dropped = answered[2]
    own = dropped.entities.get(kind="events.rsvp", name="Mette Hansen: Fællesspisning")
    assert [(c.field, c.before, c.after) for c in own.changes.all()] == [("answer", "ja", "nej")]
    promoted = dropped.entities.get(kind="events.rsvp", name="Anders Bo: Fællesspisning")
    assert promoted.changes.get().field == "promoted_at"


def test_answering_through_the_page_is_by_the_resident(
    client: Client, anton: Resident, mette: Resident
) -> None:
    event = make_event(anton)
    client.force_login(mette)
    client.post(f"{EVENTS}{event.pk}/svar", {"svar": Answer.JA})

    assert events("events.rsvp.answered", actor=mette).count() == 1


def test_comments_and_a_deletion_are_recorded(client: Client, anton: Resident, mette: Resident) -> None:
    event = make_event(anton)
    client.force_login(mette)
    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Jeg tager dessert med"})
    comment = EventComment.objects.get()
    client.post(f"{EVENTS}kommentar/{comment.pk}/slet")
    client.force_login(anton)
    client.post(f"{EVENTS}{event.pk}/slet")

    assert actions("events.") == ["events.comment.posted", "events.comment.deleted", "events.event.deleted"]
    posted = events("events.comment.posted").get()
    assert posted.actor_id == mette.pk
    assert changes(posted)["body"] == [None, "Jeg tager dessert med"]
    assert posted.entities.get(role="event").entity_id == event.pk
    deleted = events("events.event.deleted").get().entities.get(role="subject")
    assert (deleted.name, deleted.operation) == ("Fællesspisning", "deleted")


def test_the_purge_is_one_event_by_nobody_and_an_empty_one_records_nothing(
    client: Client, anton: Resident, mette: Resident
) -> None:
    client.force_login(anton)
    client.get(EVENTS)  # the list purges on the way past; nothing is due
    event = make_event(anton)
    services.set_answer(event.pk, mette, Answer.JA)
    stale = timezone.now() - RETENTION_AFTER_END - datetime.timedelta(hours=1)
    Event.objects.filter(pk=event.pk).update(starts_at=stale)

    call_command("purge_events")

    purged = events("events.event.purged").get()
    assert purged.actor_id is None
    assert purged.details == {"count": 1}
    assert {(e.kind, e.operation) for e in purged.entities.all()} == {
        ("events.event", "deleted"),
        ("events.rsvp", "deleted"),
    }


def test_the_calendar_address_is_recorded_without_its_secret(client: Client, anton: Resident) -> None:
    client.force_login(anton)
    client.get(EVENTS + "kalender/abonnement")
    client.get(EVENTS + "kalender/abonnement")  # already has one: nothing new
    client.post(EVENTS + "kalender/nyt-link")
    token = CalendarFeedToken.objects.get()
    client.get(f"/kalender/{token.token}.ics")  # a calendar fetching it is not a change

    assert actions("events.") == ["events.feed_token.created", "events.feed_token.rotated"]
    rotated = events("events.feed_token.rotated").get()
    assert rotated.actor_id == anton.pk
    change = AuditChange.objects.get(entity__event=rotated)
    assert (change.field, change.redacted, change.before, change.after) == ("token", True, None, None)
    assert not AuditChange.objects.filter(after=token.token).exists()
