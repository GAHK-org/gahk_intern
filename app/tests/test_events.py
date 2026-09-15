"""Begivenheder: events, answers, seats, the deadline, and what gets deleted when.

What is tested elsewhere: the Markdown renderer in test_markdown.py, push transport and per-topic
consent in test_push.py, image validation in test_uploads.py. What is here is this feature's own
behaviour — and, as much as anything, the things that are deliberately ABSENT, because those are
the ones a later change removes without noticing.

The two that matter most:

  * **`views.py` never touches `Event.objects`.** A private event leaks the moment somebody adds a
    view and forgets to filter it, so that is asserted against the source rather than hoped for.
  * **A locked event renders no answer form at all.** Not a disabled button — the POST target has
    to be gone, because a disabled control with a live endpoint behind it is a lie.
"""

import ast
import datetime
from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core import push
from core.models import PushSubscription
from events import access, services
from events import icalendar as icalendar_mod
from events.models import (
    RETENTION_AFTER_END,
    Answer,
    CalendarFeedToken,
    Event,
    EventComment,
    EventInvite,
    Rsvp,
    Visibility,
)
from residents.models import Resident, Role

EVENTS = "/intern/begivenheder/"


def settings_media_root() -> object:
    """MEDIA_ROOT as the `media_tmp` fixture left it — read late, never captured at import."""
    from django.conf import settings as django_settings

    return django_settings.MEDIA_ROOT


pytestmark = pytest.mark.django_db


# Whoever the trial is for, read off the real value so the gate tests cannot drift from it. The
# fallback is only for the day someone sets ACCESS_ROLES = None and opens the feature: the gate
# tests still have to be able to switch it back on.
GATED_ROLES = access.ACCESS_ROLES or (Role.ADMINISTRATOR, Role.INSPEKTION)


@pytest.fixture(autouse=True)
def rollout_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the rollout gate for the whole module.

    Begivenheder is limited to the trial group while its mechanics are being tested
    (events.access.ACCESS_ROLES), but that restriction is temporary and every test outside the
    "staged rollout" section is about behaviour that outlives it. Without this they would all have
    to hand their residents a role, which would quietly stop them testing what a normal resident
    experiences — a beboer answering ja, joining a venteliste and subscribing to a feed is most of
    this file.

    Same fixture, same argument, as test_opslagstavle.py.
    """
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


@pytest.fixture
def rollout_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the gate back on — runs after the autouse fixture, so it wins."""
    monkeypatch.setattr(access, "ACCESS_ROLES", GATED_ROLES)


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")


@pytest.fixture
def other(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="b@gahk.dk", first_name="Mette", last_name="Hansen")


@pytest.fixture
def third(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="c@gahk.dk", first_name="Anders", last_name="Bo")


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch, settings: object) -> list:
    """Record push fan-outs instead of sending them. Lifted from test_opslagstavle."""
    settings.VAPID_PUBLIC_KEY = "test-public-key"  # type: ignore[attr-defined]
    settings.VAPID_PRIVATE_KEY = "test-private-key"  # type: ignore[attr-defined]
    settings.VAPID_ADMIN_EMAIL = "drift@gahk.dk"  # type: ignore[attr-defined]
    sent: list = []

    def record(subscriptions: object, payload: dict) -> int:
        sent.append((sorted(s.user_id for s in subscriptions), payload))  # type: ignore[union-attr]
        return len(sent)

    monkeypatch.setattr(push, "_dispatch", record)
    monkeypatch.setattr(push, "_run_in_background", lambda fn: fn())
    return sent


@pytest.fixture
def media_tmp(settings: object, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = str(tmp_path)  # type: ignore[attr-defined]


def subscribe(resident: Resident, endpoint: str) -> PushSubscription:
    return PushSubscription.objects.create(
        user=resident, endpoint=endpoint, auth="a", p256dh="p", wants_begivenheder=True
    )


def make_event(organiser: Resident, **extra: object) -> Event:
    """An event as the form would have produced it — minute precision, no stray microseconds.

    That matters for the edit tests: `datetime-local` only ever submits whole minutes, so an event
    carrying microseconds would look changed the first time anyone saved the form without touching
    the time, and `significant_change` would fire a notification nobody asked for.
    """
    defaults: dict = {
        "title": "Fællesspisning",
        "starts_at": (timezone.now() + datetime.timedelta(days=3)).replace(second=0, microsecond=0),
    }
    defaults.update(extra)
    return Event.objects.create(organiser=organiser, **defaults)


# --- access and the staged rollout ----------------------------------------------------------------


# The gate is the same for every route; only the verb differs. Anything not listed here is POSTed,
# so a new write route that is forgotten below still gets checked rather than silently skipped.
GATED_GET_ROUTES = ("", "opret", "kalender", "kalender/abonnement", "1", "1/rediger", "1/ics")


@pytest.mark.parametrize(
    "path",
    [
        "",
        "opret",
        "abonner",
        "kalender",
        "kalender/abonnement",
        "kalender/nyt-link",
        "1",
        "1/rediger",
        "1/slet",
        "1/aflys",
        "1/svar",
        "1/kommentar",
        "1/ics",
        "kommentar/1/slet",
    ],
)
def test_every_route_is_closed_to_non_administrators(
    client: Client, beboer: Resident, rollout_limited: None, path: str
) -> None:
    """While ACCESS_ROLES is set, a plain resident must not reach any of it — not just the page.

    Parametrised over every route so a new view cannot quietly be added outside the gate.

    The site-root feed (`/kalender/<token>.ics`) is deliberately absent: it carries no auth decorator
    at all, because a calendar client cannot log in. Its gate is the token, and
    `test_the_feed_belongs_to_one_resident` is what pins it.
    """
    client.force_login(beboer)
    url = EVENTS + path
    response = client.get(url) if path in GATED_GET_ROUTES else client.post(url)
    assert response.status_code == 403


def test_the_route_table_covers_every_url_pattern() -> None:
    """A route added without a gate test fails here rather than in production."""
    from events import urls as events_urls

    covered = {
        "index",
        "create",
        "save_subscription",
        "calendar",
        "feed_settings",
        "rotate_token",
        "detail",
        "edit",
        "delete",
        "cancel",
        "answer",
        "create_comment",
        "delete_comment",
        "event_ics",
    }
    named = {p.name for p in events_urls.urlpatterns if p.name}
    assert named == covered, f"routes missing from the gate test: {named - covered}"


def test_an_anonymous_visitor_is_sent_to_the_login_page(client: Client) -> None:
    response = client.get(EVENTS)
    assert response.status_code == 302
    assert "/login" in response["Location"]


# --- creating and editing -------------------------------------------------------------------------


def test_a_resident_creates_an_event(client: Client, beboer: Resident, pushes: list) -> None:
    client.force_login(beboer)
    when = timezone.localtime(timezone.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    response = client.post(
        EVENTS + "opret",
        {
            "title": "Filmaften",
            "description": "",
            "location": "TV-stuen",
            "starts_at": when,
            "visibility": Visibility.AABENT,
        },
    )

    event = Event.objects.get()
    assert response["Location"] == f"{EVENTS}{event.pk}"
    assert event.organiser == beboer
    assert event.visibility == Visibility.AABENT


def test_an_event_cannot_start_in_the_past(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)
    when = timezone.localtime(timezone.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

    client.post(
        EVENTS + "opret",
        {"title": "I går", "starts_at": when, "visibility": Visibility.AABENT},
    )

    assert not Event.objects.exists()


def test_an_organiser_can_set_a_svarfrist_from_the_form(client: Client, beboer: Resident) -> None:
    """The deadline is a form field, not something only the admin can reach.

    It was missing from Meta.fields at first, which made the whole deadline half of the feature
    unreachable from the UI while every service-level test still passed.
    """
    client.force_login(beboer)
    start = timezone.now() + datetime.timedelta(days=4)
    deadline = start - datetime.timedelta(days=1)

    client.post(
        EVENTS + "opret",
        {
            "title": "Med frist",
            "visibility": Visibility.AABENT,
            "starts_at": timezone.localtime(start).strftime("%Y-%m-%dT%H:%M"),
            "rsvp_deadline_at": timezone.localtime(deadline).strftime("%Y-%m-%dT%H:%M"),
        },
    )

    event = Event.objects.get()
    assert event.rsvp_deadline_at is not None
    assert timezone.localtime(event.rsvp_deadline_at).strftime("%Y-%m-%dT%H:%M") == (
        timezone.localtime(deadline).strftime("%Y-%m-%dT%H:%M")
    )


def test_a_svarfrist_after_the_start_is_refused(client: Client, beboer: Resident) -> None:
    """Refused rather than ignored: answers close at the start regardless, so such a deadline would
    show "svar inden lørdag" on a page whose answers actually shut on Friday."""
    client.force_login(beboer)
    start = timezone.now() + datetime.timedelta(days=4)

    client.post(
        EVENTS + "opret",
        {
            "title": "Frist efter start",
            "visibility": Visibility.AABENT,
            "starts_at": timezone.localtime(start).strftime("%Y-%m-%dT%H:%M"),
            "rsvp_deadline_at": timezone.localtime(start + datetime.timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
        },
    )

    assert not Event.objects.exists()


def test_the_end_must_come_after_the_start(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)
    start = timezone.now() + datetime.timedelta(days=2)
    client.post(
        EVENTS + "opret",
        {
            "title": "Bagvendt",
            "visibility": Visibility.AABENT,
            "starts_at": timezone.localtime(start).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": timezone.localtime(start - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert not Event.objects.exists()


def test_only_a_host_can_edit(client: Client, beboer: Resident, other: Resident) -> None:
    event = make_event(beboer)
    client.force_login(other)
    assert client.get(f"{EVENTS}{event.pk}/rediger").status_code == 403


def test_a_co_organiser_can_edit(client: Client, beboer: Resident, other: Resident) -> None:
    event = make_event(beboer)
    event.co_organisers.add(other)
    client.force_login(other)
    assert client.get(f"{EVENTS}{event.pk}/rediger").status_code == 200


def test_moving_the_start_bumps_sequence_and_notifies(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """One predicate drives both, so a calendar client and a phone cannot disagree about whether
    something changed. See services.significant_change."""
    event = make_event(beboer)
    services.set_answer(event.pk, other, Answer.JA)
    subscribe(other, "https://push.example/other")
    client.force_login(beboer)
    pushes.clear()

    moved = timezone.localtime(event.starts_at + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    client.post(
        f"{EVENTS}{event.pk}/rediger",
        {"title": event.title, "starts_at": moved, "visibility": Visibility.AABENT},
    )

    event.refresh_from_db()
    assert event.sequence == 1
    assert [p[0] for p in pushes] == [[other.pk]]


def test_editing_only_the_description_notifies_nobody(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """A typo fix must not buzz sixty phones or make every calendar re-notify."""
    event = make_event(beboer)
    services.set_answer(event.pk, other, Answer.JA)
    subscribe(other, "https://push.example/other")
    client.force_login(beboer)
    pushes.clear()

    client.post(
        f"{EVENTS}{event.pk}/rediger",
        {
            "title": event.title,
            "visibility": Visibility.AABENT,
            "description": "Rettet stavefejl",
            "starts_at": timezone.localtime(event.starts_at).strftime("%Y-%m-%dT%H:%M"),
        },
    )

    event.refresh_from_db()
    assert event.sequence == 0
    assert pushes == []


# --- rsvp: ja og nej ------------------------------------------------------------------------------


def test_answering_ja_takes_a_seat(client: Client, beboer: Resident) -> None:
    event = make_event(beboer)
    client.force_login(beboer)

    client.post(f"{EVENTS}{event.pk}/svar", {"svar": "ja"})

    rsvp = Rsvp.objects.get()
    assert rsvp.answer == Answer.JA
    assert [r.pk for r in services.seated(Event.objects.get())] == [rsvp.pk]


def test_answering_returns_only_the_panel(client: Client, beboer: Resident) -> None:
    """The htmx swap target, so pressing Ja never re-renders the page under a reader's thumb."""
    event = make_event(beboer)
    client.force_login(beboer)

    body = client.post(f"{EVENTS}{event.pk}/svar", {"svar": "ja"}).content.decode()

    assert f'id="rsvp-{event.pk}"' in body
    assert "<html" not in body


def test_answering_twice_leaves_one_row(client: Client, beboer: Resident) -> None:
    """uniq_rsvp_per_resident, and the double-tapped phone it exists for."""
    event = make_event(beboer)
    client.force_login(beboer)

    client.post(f"{EVENTS}{event.pk}/svar", {"svar": "ja"})
    client.post(f"{EVENTS}{event.pk}/svar", {"svar": "ja"})

    assert Rsvp.objects.count() == 1


def test_answering_notifies_nobody(client: Client, beboer: Resident, other: Resident, pushes: list) -> None:
    """Sixty phones buzzing about somebody else's dinner plans is the noise this replaces — and it
    is what stops people answering at all. Same call NoticeReaction made."""
    event = make_event(beboer)
    subscribe(beboer, "https://push.example/a")
    subscribe(other, "https://push.example/b")
    client.force_login(other)
    pushes.clear()

    client.post(f"{EVENTS}{event.pk}/svar", {"svar": "ja"})

    assert pushes == []


# --- capacity and the waitlist ---------------------------------------------------------------------


def test_the_first_person_over_the_cap_goes_on_the_waitlist(
    beboer: Resident, other: Resident, third: Resident
) -> None:
    event = make_event(beboer, capacity=2)

    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    outcome = services.set_answer(event.pk, third, Answer.JA)

    assert outcome.seated is False
    assert len(services.seated(event)) == 2
    assert [r.resident_id for r in services.waitlist(event)] == [third.pk]


def test_no_capacity_means_no_waitlist_ever(beboer: Resident, other: Resident, third: Resident) -> None:
    event = make_event(beboer)  # capacity is None
    for who in (beboer, other, third):
        services.set_answer(event.pk, who, Answer.JA)

    assert len(services.seated(event)) == 3
    assert services.waitlist(event) == []


def test_capacity_zero_is_not_the_same_as_no_capacity(beboer: Resident) -> None:
    """NULL means no cap; 0 is a real, if silly, state. They must stay distinguishable."""
    event = make_event(beboer, capacity=0)
    outcome = services.set_answer(event.pk, beboer, Answer.JA)
    assert outcome.seated is False


def test_withdrawing_promotes_the_next_in_line(
    beboer: Resident, other: Resident, third: Resident, pushes: list
) -> None:
    event = make_event(beboer, capacity=1)
    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    services.set_answer(event.pk, third, Answer.JA)
    pushes.clear()
    subscribe(other, "https://push.example/other")

    services.set_answer(event.pk, beboer, Answer.NEJ)

    assert [r.resident_id for r in services.seated(event)] == [other.pk]
    assert Rsvp.objects.get(event=event, resident=other).promoted_at is not None
    assert [r.resident_id for r in services.waitlist(event)] == [third.pk]


def test_a_promotion_notifies_exactly_the_promoted_person(
    beboer: Resident, other: Resident, third: Resident, pushes: list
) -> None:
    event = make_event(beboer, capacity=1)
    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    for who in (beboer, other, third):
        subscribe(who, f"https://push.example/{who.pk}")
    pushes.clear()

    services.set_answer(event.pk, beboer, Answer.NEJ)

    assert [p[0] for p in pushes] == [[other.pk]]


def test_a_promoted_attendee_is_distinguishable_from_a_direct_one(beboer: Resident, other: Resident) -> None:
    """`promoted_at` no longer decides anything — seating is derived — but it is still recorded, so
    the page can say "du er rykket op" and this can assert the notification had a reason."""
    event = make_event(beboer, capacity=1)
    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    services.set_answer(event.pk, beboer, Answer.NEJ)

    assert Rsvp.objects.get(event=event, resident=other).promoted_at is not None


def test_the_queue_is_ordered_by_the_current_commitment_not_first_contact(
    beboer: Resident, other: Resident, third: Resident
) -> None:
    """ja → nej → ja puts you at the back: you gave the seat up. `created_at` remembers first
    contact, `answered_at` remembers the commitment, and the queue reads the latter."""
    event = make_event(beboer, capacity=1)
    services.set_answer(event.pk, beboer, Answer.JA)  # seated
    services.set_answer(event.pk, other, Answer.JA)  # queue: other
    services.set_answer(event.pk, other, Answer.NEJ)  # leaves
    services.set_answer(event.pk, third, Answer.JA)  # queue: third
    services.set_answer(event.pk, other, Answer.JA)  # rejoins, behind third

    assert [r.resident_id for r in services.waitlist(event)] == [third.pk, other.pk]


def test_saying_nej_from_the_waitlist_promotes_nobody(
    beboer: Resident, other: Resident, third: Resident
) -> None:
    event = make_event(beboer, capacity=1)
    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    services.set_answer(event.pk, third, Answer.JA)

    services.set_answer(event.pk, other, Answer.NEJ)  # was queued, not seated

    assert len(services.seated(event)) == 1
    assert [r.resident_id for r in services.waitlist(event)] == [third.pk]


def test_the_capacity_cannot_be_lowered_below_the_seated_count(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """Refusing rather than silently demoting somebody who was told they had a seat."""
    event = make_event(beboer, capacity=3)
    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    client.force_login(beboer)

    client.post(
        f"{EVENTS}{event.pk}/rediger",
        {
            "title": event.title,
            "visibility": Visibility.AABENT,
            "starts_at": timezone.localtime(event.starts_at).strftime("%Y-%m-%dT%H:%M"),
            "capacity": "1",
        },
    )

    event.refresh_from_db()
    assert event.capacity == 3


# --- the rsvp deadline ----------------------------------------------------------------------------


def test_answers_are_open_just_before_the_deadline(beboer: Resident) -> None:
    deadline = timezone.now() + datetime.timedelta(days=1)
    event = make_event(beboer, rsvp_deadline_at=deadline)
    just_before = deadline - datetime.timedelta(microseconds=1)

    assert services.answers_locked(event, now=just_before) is False


def test_answers_are_closed_at_the_deadline_instant(beboer: Resident) -> None:
    """`<=`: Danish "svarfrist kl. 18.00" reads as *senest* 18.00."""
    deadline = timezone.now() + datetime.timedelta(days=1)
    event = make_event(beboer, rsvp_deadline_at=deadline)

    assert services.answers_locked(event, now=deadline) is True


def test_an_event_without_a_deadline_closes_when_it_starts(beboer: Resident) -> None:
    """The only defensible implicit rule, and what makes answers_locked total — there is no state
    in which this feature is open forever."""
    event = make_event(beboer, starts_at=timezone.now() + datetime.timedelta(hours=1))

    assert services.answers_locked(event) is False
    assert services.answers_locked(event, now=event.starts_at) is True


def test_the_service_refuses_a_late_answer_even_when_the_view_is_bypassed(beboer: Resident) -> None:
    """set_answer is the authority — not the view, and not the form. The same transition is
    reachable from five places."""
    event = make_event(beboer, rsvp_deadline_at=timezone.now() - datetime.timedelta(minutes=1))

    with pytest.raises(services.RsvpClosed):
        services.set_answer(event.pk, beboer, Answer.JA)


def test_a_locked_event_renders_no_answer_form(client: Client, beboer: Resident) -> None:
    """The POST target must be ABSENT, not disabled. A greyed-out button with a live endpoint
    behind it is a lie, so this asserts the URL is gone rather than that an attribute is present."""
    event = make_event(beboer, rsvp_deadline_at=timezone.now() - datetime.timedelta(minutes=1))
    client.force_login(beboer)

    body = client.get(f"{EVENTS}{event.pk}").content.decode()

    assert "Svarfristen er udløbet" in body
    assert f"{EVENTS}{event.pk}/svar" not in body


def test_nobody_can_change_their_answer_after_the_deadline(beboer: Resident, other: Resident) -> None:
    """The guarantee the whole derived-seating design rests on.

    Seating is not stored anywhere — `seated` recomputes it from the ja rows every time — so the
    attendee list is final only because the rows it reads are. That holds precisely because
    `set_answer` refuses EVERY answer once the deadline has passed, drop-outs included and not just
    new arrivals. If withdrawing were ever allowed after the deadline, the person behind you would
    silently move up and "listen er endelig" would stop being true.
    """
    event = make_event(beboer, capacity=1)
    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    Event.objects.filter(pk=event.pk).update(rsvp_deadline_at=timezone.now() - datetime.timedelta(minutes=1))

    with pytest.raises(services.RsvpClosed):
        services.set_answer(event.pk, beboer, Answer.NEJ)  # the seated person tries to drop out
    with pytest.raises(services.RsvpClosed):
        services.set_answer(event.pk, other, Answer.NEJ)  # and so does the queued one

    event.refresh_from_db()
    assert [r.resident_id for r in services.seated(event)] == [beboer.pk]
    assert [r.resident_id for r in services.waitlist(event)] == [other.pk]


def test_the_seating_is_stable_across_reads_once_locked(beboer: Resident, other: Resident) -> None:
    """Derived does not mean unstable: the same rows in the same order give the same answer."""
    event = make_event(beboer, capacity=1)
    services.set_answer(event.pk, beboer, Answer.JA)
    services.set_answer(event.pk, other, Answer.JA)
    Event.objects.filter(pk=event.pk).update(rsvp_deadline_at=timezone.now() - datetime.timedelta(minutes=1))
    event.refresh_from_db()

    first = [r.resident_id for r in services.seated(event)]
    second = [r.resident_id for r in services.seated(event)]

    assert first == second == [beboer.pk]


# --- aflys, and when delete is allowed --------------------------------------------------------------


def test_an_event_nobody_answered_can_be_deleted(client: Client, beboer: Resident) -> None:
    event = make_event(beboer)
    client.force_login(beboer)

    client.post(f"{EVENTS}{event.pk}/slet")

    assert not Event.objects.filter(pk=event.pk).exists()


def test_an_event_someone_is_coming_to_cannot_be_deleted(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """A hard delete vanishes from every subscribed calendar with no explanation, and the .ics
    already imported into other people's calendars stays there forever."""
    event = make_event(beboer)
    services.set_answer(event.pk, other, Answer.JA)
    client.force_login(beboer)

    assert client.post(f"{EVENTS}{event.pk}/slet").status_code == 403
    assert Event.objects.filter(pk=event.pk).exists()


def test_cancelling_keeps_the_row_and_tells_the_people_coming(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    event = make_event(beboer)
    services.set_answer(event.pk, other, Answer.JA)
    subscribe(other, "https://push.example/other")
    client.force_login(beboer)
    pushes.clear()

    client.post(f"{EVENTS}{event.pk}/aflys")

    event.refresh_from_db()
    assert event.cancelled_at is not None
    assert event.sequence == 1
    assert [p[0] for p in pushes] == [[other.pk]]


def test_a_cancelled_event_takes_no_more_answers(beboer: Resident, other: Resident) -> None:
    event = make_event(beboer)
    services.cancel(event)

    with pytest.raises(services.RsvpClosed):
        services.set_answer(event.pk, other, Answer.JA)


# --- retention ---------------------------------------------------------------------------------------


def test_an_event_is_kept_for_a_week_after_it_is_held() -> None:
    """The policy, pinned once and by value.

    Every other test in this section works off the constant, so they follow a change here instead of
    failing one by one. This is the one that has to be edited deliberately — which is the point:
    widening retention is a decision about whether this feature has an archive, not a tuning knob.
    """
    assert RETENTION_AFTER_END == datetime.timedelta(days=7)


def test_an_event_survives_until_the_window_is_up(beboer: Resident) -> None:
    now = timezone.now()
    ended = now - RETENTION_AFTER_END + datetime.timedelta(hours=1)  # an hour short of the cutoff
    make_event(beboer, starts_at=ended - datetime.timedelta(hours=3), ends_at=ended)

    assert Event.objects.purge_expired() == 0


def test_an_event_is_gone_once_the_window_is_up(beboer: Resident) -> None:
    now = timezone.now()
    ended = now - RETENTION_AFTER_END - datetime.timedelta(hours=1)  # an hour past it
    make_event(beboer, starts_at=ended - datetime.timedelta(hours=3), ends_at=ended)

    assert Event.objects.purge_expired() == 1


def test_retention_falls_back_to_the_start_when_there_is_no_end(beboer: Resident) -> None:
    make_event(beboer, starts_at=timezone.now() - RETENTION_AFTER_END - datetime.timedelta(hours=1))
    assert Event.objects.purge_expired() == 1


def test_a_cancelled_event_is_kept_for_its_own_grace_period(beboer: Resident) -> None:
    """It never happens, so the held-and-done rule would either purge it the moment its start
    passed or hold it to a date that means nothing. Thirty days is what lets subscribed calendars
    poll and pick up STATUS:CANCELLED."""
    event = make_event(beboer, starts_at=timezone.now() + datetime.timedelta(days=9))
    Event.objects.filter(pk=event.pk).update(cancelled_at=timezone.now() - datetime.timedelta(days=29))
    assert Event.objects.purge_expired() == 0

    Event.objects.filter(pk=event.pk).update(cancelled_at=timezone.now() - datetime.timedelta(days=31))
    assert Event.objects.purge_expired() == 1


def test_purging_takes_the_answers_and_the_image_with_it(
    beboer: Resident,
    other: Resident,
    media_tmp: None,
    django_capture_on_commit_callbacks: Callable,
) -> None:
    """A bulk queryset delete never calls Model.delete(), which is why the file cleanup is a
    post_delete signal. See core.files."""

    event = make_event(beboer, starts_at=timezone.now() + datetime.timedelta(hours=1))
    services.set_answer(event.pk, other, Answer.JA)
    event.image.save("plakat.png", SimpleUploadedFile("plakat.png", b"x", "image/png"), save=True)
    stored = event.image.storage
    name = event.image.name
    stale = timezone.now() - RETENTION_AFTER_END - datetime.timedelta(hours=1)
    Event.objects.filter(pk=event.pk).update(starts_at=stale)

    with django_capture_on_commit_callbacks(execute=True):
        Event.objects.purge_expired()

    assert not Rsvp.objects.exists()
    assert not stored.exists(name)


def test_the_list_purges_on_the_way_past(client: Client, beboer: Resident) -> None:
    """Traffic does the cleanup; the cron covers the quiet weeks. The den_hurtige idiom, and the
    backstop for a cron that has silently stopped."""
    stale = timezone.now() - RETENTION_AFTER_END - datetime.timedelta(hours=1)
    make_event(beboer, starts_at=stale)
    client.force_login(beboer)

    client.get(EVENTS)

    assert not Event.objects.exists()


def test_the_list_shows_only_what_is_coming(client: Client, beboer: Resident) -> None:
    make_event(beboer, title="Snart", starts_at=timezone.now() + datetime.timedelta(days=1))
    make_event(beboer, title="Lige overstået", starts_at=timezone.now() - datetime.timedelta(hours=2))
    client.force_login(beboer)

    body = client.get(EVENTS).content.decode()

    assert "Snart" in body
    assert "Lige overstået" not in body


# --- notifications -----------------------------------------------------------------------------------


def test_a_new_event_notifies_everyone_but_the_organiser(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    subscribe(beboer, "https://push.example/a")
    subscribe(other, "https://push.example/b")
    client.force_login(beboer)
    when = timezone.localtime(timezone.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    client.post(
        EVENTS + "opret",
        {"title": "Fest", "starts_at": when, "visibility": Visibility.AABENT},
    )

    assert [p[0] for p in pushes] == [[other.pk]]


def test_the_notification_links_to_the_event_not_the_list(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    subscribe(other, "https://push.example/b")
    client.force_login(beboer)
    when = timezone.localtime(timezone.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    client.post(
        EVENTS + "opret",
        {"title": "Fest", "starts_at": when, "visibility": Visibility.AABENT},
    )

    event = Event.objects.get()
    assert pushes[0][1]["url"] == f"{EVENTS}{event.pk}"


# --- visibility: kun inviterede -------------------------------------------------------------------
#
# The leak surface. A private event has to be ABSENT for a non-invitee, not forbidden — 404 rather
# than 403, because a 403 confirms that an event with that id exists, which is the fact the event is
# hiding. These are parametrised over every route so that a view added later without the visibility
# filter fails here instead of in production.

PRIVATE_ROUTES = [
    ("get", "{pk}"),
    ("get", "{pk}/rediger"),
    ("post", "{pk}/slet"),
    ("post", "{pk}/aflys"),
    ("post", "{pk}/svar"),
    ("post", "{pk}/kommentar"),
    ("get", "{pk}/ics"),
]


def make_private(organiser: Resident, invitees: list[Resident], **extra: object) -> Event:
    event = make_event(organiser, visibility=Visibility.KUN_INVITEREDE, **extra)
    for who in invitees:
        EventInvite.objects.create(event=event, resident=who, invited_by=organiser)
    return event


@pytest.mark.parametrize(("method", "suffix"), PRIVATE_ROUTES)
def test_an_outsider_gets_404_on_every_route_of_a_private_event(
    client: Client, beboer: Resident, other: Resident, method: str, suffix: str
) -> None:
    """404, specifically — never 403. See access.py on why the two refusals mean different things."""
    event = make_private(beboer, [])
    client.force_login(other)

    url = EVENTS + suffix.format(pk=event.pk)
    response = client.get(url) if method == "get" else client.post(url)

    assert response.status_code == 404


def test_the_private_route_table_covers_every_pk_route() -> None:
    """A pk-taking route added without a leak test fails here rather than silently leaking.

    Names are resolved rather than counted: an earlier version of this test compared lengths, which
    would have been satisfied by covering the same route twice.
    """
    from django.urls import resolve

    from events import urls as events_urls

    pk_routes = {p.name for p in events_urls.urlpatterns if p.name and "<int:pk>" in str(p.pattern)}
    covered = {resolve(EVENTS + suffix.format(pk=1)).url_name for _method, suffix in PRIVATE_ROUTES}
    # delete_comment takes a COMMENT's pk, not an event's, so the parametrised table above cannot
    # reach it — the url needs a comment to exist before there is an id to ask about. Its leak test
    # is test_an_outsider_cannot_delete_a_comment_on_a_private_event, named here so this meta-test
    # keeps working as a checklist rather than being loosened to a subset comparison.
    covered |= {"delete_comment"}

    assert pk_routes == covered, f"pk routes with no leak test: {pk_routes - covered}"


# --- comments -------------------------------------------------------------------------------------
#
# The thread on an event. Three things separate it from opslagstavlen's comments and each has a test
# here: it is open to anyone who can SEE the event (not only those who answered), it is removable by
# its author or a HOST rather than by Inspektionen, and it goes when the event goes.


def test_any_resident_who_can_see_the_event_may_comment(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """Not gated on having answered: "kan man tage boern med?" is asked before you commit."""
    event = make_event(beboer)
    client.force_login(other)

    response = client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Skal man tage noget med?"})

    assert response.status_code == 302
    comment = EventComment.objects.get()
    assert comment.body == "Skal man tage noget med?"
    assert comment.author_id == other.pk
    assert not event.rsvps.exists(), "commenting must not imply an answer"


def test_commenting_lands_back_on_the_thread(client: Client, beboer: Resident) -> None:
    """The page is long -- image, facts, description, answer panel, posts -- so a bare redirect to
    the top hides the comment that was just written. See views._comment_anchor."""
    event = make_event(beboer)
    client.force_login(beboer)

    response = client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Jeg tager kage med"})

    assert response["Location"] == f"{EVENTS}{event.pk}#kommentarer"


def test_a_blank_comment_is_refused(client: Client, beboer: Resident) -> None:
    """Whitespace only, and nothing attached. Django accepts a blank TextField and the form now
    strips rather than rejects, so the refusal is the view's — see test_neither_text_nor_photo_is_refused
    for the same rule stated from the photo side."""
    event = make_event(beboer)
    client.force_login(beboer)

    response = client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "   \n  "}, follow=True)

    assert not EventComment.objects.exists()
    assert "Skriv en kommentar, eller vedhæft et billede." in response.content.decode()


def test_the_thread_is_rendered_on_the_event_page(client: Client, beboer: Resident) -> None:
    event = make_event(beboer)
    EventComment.objects.create(event=event, author=beboer, body="Vi moedes i koekkenet")
    client.force_login(beboer)

    body = client.get(f"{EVENTS}{event.pk}").content.decode()

    assert "Vi moedes i koekkenet" in body
    assert "1 kommentar" in body


def test_a_comment_is_plain_text_and_never_markup(client: Client, beboer: Resident) -> None:
    """Deliberately not Markdown (see EventComment), so a pasted tag is shown, not run."""
    event = make_event(beboer)
    EventComment.objects.create(event=event, author=beboer, body="<b>hej</b> **ikke fed**")
    client.force_login(beboer)

    body = client.get(f"{EVENTS}{event.pk}").content.decode()

    assert "&lt;b&gt;hej&lt;/b&gt;" in body
    assert "<b>hej</b>" not in body
    assert "**ikke fed**" in body


def test_a_comment_on_a_cancelled_event_is_still_allowed(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """Aflyst freezes the ANSWER, not the conversation. "Hvorfor?" and "vi finder en ny dato" are
    the most predictable thread on the feature, and a comment re-notifies no calendar the way an
    edit would. See views.create_comment."""
    event = make_event(beboer)
    client.force_login(beboer)
    client.post(f"{EVENTS}{event.pk}/aflys")
    client.force_login(other)

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Hvorfor?"})

    assert EventComment.objects.count() == 1


def test_its_author_deletes_their_own_comment(client: Client, beboer: Resident, other: Resident) -> None:
    event = make_event(beboer)
    comment = EventComment.objects.create(event=event, author=other, body="Fortryder")
    client.force_login(other)

    response = client.post(f"{EVENTS}kommentar/{comment.pk}/slet")

    assert response.status_code == 302
    assert not EventComment.objects.exists()


def test_a_host_deletes_somebody_elses_comment(client: Client, beboer: Resident, other: Resident) -> None:
    """The host runs the event and the thread is aimed at them, so they moderate it."""
    event = make_event(beboer)
    comment = EventComment.objects.create(event=event, author=other, body="Spam spam spam")
    client.force_login(beboer)

    client.post(f"{EVENTS}kommentar/{comment.pk}/slet")

    assert not EventComment.objects.exists()


def test_a_co_organiser_may_also_delete_a_comment(
    client: Client,
    beboer: Resident,
    other: Resident,
    make_resident: Callable[..., Resident],
) -> None:
    """access.is_host, not just the organiser -- co-organisers run the event too."""
    helper = make_resident(email="medarrangoer@gahk.dk")
    event = make_event(beboer)
    event.co_organisers.add(helper)
    comment = EventComment.objects.create(event=event, author=other, body="Vaek med den")
    client.force_login(helper)

    client.post(f"{EVENTS}kommentar/{comment.pk}/slet")

    assert not EventComment.objects.exists()


def test_a_bystander_cannot_delete_somebody_elses_comment(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """403, not 404: the comment is on an event `other` can see, so its existence is no secret.
    See access.py on the split between the two refusals."""
    event = make_event(beboer)
    comment = EventComment.objects.create(event=event, author=beboer, body="Min kommentar")
    client.force_login(other)

    response = client.post(f"{EVENTS}kommentar/{comment.pk}/slet")

    assert response.status_code == 403
    assert EventComment.objects.exists()


def test_inspektionen_may_not_delete_a_comment(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """THE DELIBERATE DIVERGENCE from both sibling features, where a moderator may remove any
    comment. It cannot be that here: access.visible_to gives Inspektionen no read access to a
    private event, so "Inspektionen moderates comments" is a power that either does nothing or
    quietly restores the visibility the 404 exists to deny. A reported thread is a superuser job in
    the admin, exactly as a reported private event already is."""
    inspektion = make_resident(email="inspektion@gahk.dk", roles=(Role.INSPEKTION,))
    event = make_event(beboer)
    comment = EventComment.objects.create(event=event, author=beboer, body="Min kommentar")
    client.force_login(inspektion)

    response = client.post(f"{EVENTS}kommentar/{comment.pk}/slet")

    assert response.status_code == 403
    assert EventComment.objects.exists()


def test_the_delete_button_is_only_rendered_for_someone_who_may_use_it(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """The permission and the control have to agree, or the page offers a button that 403s."""
    event = make_event(beboer)
    EventComment.objects.create(event=event, author=beboer, body="Min kommentar")
    client.force_login(other)

    assert "Slet kommentar" not in client.get(f"{EVENTS}{event.pk}").content.decode()

    client.force_login(beboer)
    assert "Slet kommentar" in client.get(f"{EVENTS}{event.pk}").content.decode()


def test_an_outsider_cannot_delete_a_comment_on_a_private_event(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """404, and it is the reason views._visible_comment exists. A comment id is a side channel onto
    the event it hangs off: without the visible_to filter this would answer 403 ("exists, but not
    yours"), which tells a non-invitee that a private event with a thread on it is there.

    Named in test_the_private_route_table_covers_every_pk_route, which cannot reach this route by
    parametrising over an event pk.
    """
    event = make_private(beboer, [])
    comment = EventComment.objects.create(event=event, author=beboer, body="Hemmeligt")
    client.force_login(other)

    response = client.post(f"{EVENTS}kommentar/{comment.pk}/slet")

    assert response.status_code == 404
    assert EventComment.objects.exists()


def test_an_invitee_may_comment_on_a_private_event(client: Client, beboer: Resident, other: Resident) -> None:
    """The positive control for the 404 above: the filter must not shut out the invite list."""
    event = make_private(beboer, [other])
    client.force_login(other)

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Jeg kommer"})

    assert EventComment.objects.count() == 1


def test_comments_go_when_the_event_goes(beboer: Resident) -> None:
    """CASCADE is the retention. The module docstring commits to there being no record of what
    happened, so a thread outliving its event would be the archive it says belongs to opslagstavlen."""
    event = make_event(beboer)
    EventComment.objects.create(event=event, author=beboer, body="Ses i morgen")

    event.delete()

    assert not EventComment.objects.exists()


def test_a_comment_notifies_the_hosts_and_nobody_else(
    client: Client,
    beboer: Resident,
    other: Resident,
    make_resident: Callable[..., Resident],
    pushes: list,
) -> None:
    """The rule in services.py is that a notification is about a commitment with a clock on it,
    which is why answering notifies nobody. A comment is narrow for the same reason: it is a
    question aimed at whoever runs the event, and going wide would be sixty phones buzzing about
    somebody else's dinner plans."""
    bystander = make_resident(email="tilskuer@gahk.dk")
    helper = make_resident(email="medarrangoer@gahk.dk")
    event = make_event(beboer)
    event.co_organisers.add(helper)
    for who in (beboer, other, bystander, helper):
        subscribe(who, f"https://push.example/{who.pk}")
    client.force_login(other)

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Skal man tage noget med?"})

    assert len(pushes) == 1
    assert pushes[0][0] == sorted([beboer.pk, helper.pk])
    assert pushes[0][1]["head"] == f"{other.full_name} kommenterede"
    assert pushes[0][1]["url"] == f"{EVENTS}{event.pk}"


def test_a_host_commenting_on_their_own_event_notifies_nobody(
    client: Client, beboer: Resident, pushes: list
) -> None:
    """The author is excluded even when they are the only host -- otherwise an organiser answering
    a question would buzz their own phone."""
    event = make_event(beboer)
    subscribe(beboer, "https://push.example/a")
    client.force_login(beboer)

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Tag selv drikkevarer med"})

    assert pushes == []


def test_a_comment_on_a_private_event_cannot_notify_past_the_invite_list(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """_audience narrows to the invite list first, so a private event's thread has the same
    guarantee notify_new_event has."""
    event = make_private(beboer, [other])
    subscribe(beboer, "https://push.example/a")
    subscribe(other, "https://push.example/b")
    client.force_login(other)

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Jeg kommer"})

    assert len(pushes) == 1
    assert pushes[0][0] == [beboer.pk]


def test_rendering_a_thread_costs_no_query_per_comment(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """`can_delete` is the same answer for every row, so it is resolved once for the event. Asked
    per comment it calls access.is_host each time, which filters co_organisers -- twelve comments,
    twelve queries, to draw a button that is on all of them or none."""
    event = make_event(beboer)
    client.force_login(beboer)
    client.get(f"{EVENTS}{event.pk}")  # warm

    for n in range(3):
        EventComment.objects.create(event=event, author=other, body=f"Kommentar {n}")
    with CaptureQueriesContext(connection) as few:
        client.get(f"{EVENTS}{event.pk}")

    for n in range(3, 12):
        EventComment.objects.create(event=event, author=other, body=f"Kommentar {n}")
    with CaptureQueriesContext(connection) as many:
        client.get(f"{EVENTS}{event.pk}")

    assert len(many.captured_queries) == len(few.captured_queries), (
        f"{len(few.captured_queries)} queries for 3 comments, {len(many.captured_queries)} for 12"
    )


# --- photos on comments -------------------------------------------------------------------------
#
# One optional picture per comment, and a picture on its own is a whole comment. The awkward case is
# a photo-only comment whose photo is refused: it has to fail, and it has to say both why the
# picture did not count and why the comment did not land. See views.create_comment.


def test_a_comment_can_carry_a_photo(client: Client, beboer: Resident, media_tmp: None) -> None:
    event = make_event(beboer)
    client.force_login(beboer)
    photo = SimpleUploadedFile("lokale.jpg", bytes.fromhex("ffd8ff") + b"x" * 512, content_type="image/jpeg")

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Sådan ser lokalet ud", "image": photo})

    comment = EventComment.objects.get()
    assert comment.body == "Sådan ser lokalet ud"
    assert comment.image, "the file must be attached, not merely accepted"
    assert comment.image.name.startswith("begivenheder/kommentarer/")


def test_a_photo_on_its_own_is_a_whole_comment(client: Client, beboer: Resident, media_tmp: None) -> None:
    """ "Her, se" is an answer. The body is blank-able for exactly this."""
    event = make_event(beboer)
    client.force_login(beboer)
    photo = SimpleUploadedFile(
        "kvittering.jpg", bytes.fromhex("ffd8ff") + b"x" * 512, content_type="image/jpeg"
    )

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "", "image": photo})

    comment = EventComment.objects.get()
    assert comment.body == ""
    assert comment.image


def test_neither_text_nor_photo_is_refused(client: Client, beboer: Resident) -> None:
    event = make_event(beboer)
    client.force_login(beboer)

    response = client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "  "}, follow=True)

    assert not EventComment.objects.exists()
    assert "Skriv en kommentar, eller vedhæft et billede." in response.content.decode()


def test_a_refused_photo_does_not_throw_away_the_text_beside_it(
    client: Client, beboer: Resident, media_tmp: None
) -> None:
    """An SVG is refused by core.uploads because it executes script when opened from our own
    /media/. Losing a typed comment to that would be the worse outcome, so the comment saves and the
    warning explains the missing picture."""
    event = make_event(beboer)
    client.force_login(beboer)
    bad = SimpleUploadedFile(
        "evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", content_type="image/svg+xml"
    )

    response = client.post(
        f"{EVENTS}{event.pk}/kommentar", {"body": "Kan man tage børn med?", "image": bad}, follow=True
    )

    comment = EventComment.objects.get()
    assert comment.body == "Kan man tage børn med?"
    assert not comment.image, "the SVG must not be stored"
    assert "Billedet blev ikke gemt" in response.content.decode()


def test_a_refused_photo_with_no_text_reports_both_halves(
    client: Client, beboer: Resident, media_tmp: None
) -> None:
    """core.uploads.attached_image returns None for "nothing attached" and "attached but refused"
    alike, so this lands in the empty branch — which is right, there is nothing to save. On its own
    that would explain only half of it, so both messages have to arrive together."""
    event = make_event(beboer)
    client.force_login(beboer)
    bad = SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/svg+xml")

    response = client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "", "image": bad}, follow=True)
    body = response.content.decode()

    assert not EventComment.objects.exists()
    assert "Billedet blev ikke gemt" in body
    assert "Skriv en kommentar, eller vedhæft et billede." in body


def test_an_oversized_photo_is_refused(
    client: Client, beboer: Resident, media_tmp: None, settings: object
) -> None:
    settings.EVENT_IMAGE_MAX_MB = 1  # type: ignore[attr-defined]
    event = make_event(beboer)
    client.force_login(beboer)
    huge = SimpleUploadedFile(
        "stor.jpg", bytes.fromhex("ffd8ff") + b"x" * (2 * 1024 * 1024), content_type="image/jpeg"
    )

    response = client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "Se her", "image": huge}, follow=True)

    assert not EventComment.objects.get().image
    assert "for stort" in response.content.decode()


def test_the_comment_photo_is_rendered_on_the_event_page(
    client: Client, beboer: Resident, media_tmp: None
) -> None:
    event = make_event(beboer)
    client.force_login(beboer)
    photo = SimpleUploadedFile("lokale.jpg", bytes.fromhex("ffd8ff") + b"x" * 512, content_type="image/jpeg")
    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "", "image": photo})

    body = client.get(f"{EVENTS}{event.pk}").content.decode()

    assert EventComment.objects.get().image.url in body
    assert 'enctype="multipart/form-data"' in body, "without it request.FILES is silently empty"


def test_deleting_a_comment_deletes_its_photo(
    beboer: Resident, media_tmp: None, django_capture_on_commit_callbacks: Callable
) -> None:
    """The post_delete receiver, which keeps "the comment is gone" from leaving a paid-for file that
    nothing references.

    WRAPPED IN django_capture_on_commit_callbacks BECAUSE THE DELETE IS DEFERRED, and that deferral
    is the feature: core.files.delete_attached_files purges on `transaction.on_commit`, so a delete
    that is rolled back cannot destroy the file. A test runs inside a transaction that never
    commits, so without this the callback simply never fires and the assertion would be testing the
    test harness. Same pattern as the Den Hurtige and CMS image-deletion tests.
    """
    from django.core.files.base import ContentFile

    event = make_event(beboer)
    comment = EventComment.objects.create(event=event, author=beboer, body="Se her")
    comment.image.save("lokale.jpg", ContentFile(b"jpegbytes"), save=True)
    path = Path(str(settings_media_root())) / comment.image.name
    assert path.is_file()

    with django_capture_on_commit_callbacks(execute=True):
        comment.delete()

    assert not path.is_file()


def test_a_photo_only_comment_notifies_with_a_body_rather_than_a_blank(
    client: Client, beboer: Resident, other: Resident, pushes: list, media_tmp: None
) -> None:
    """push.preview("") is "", and a notification with an empty body reads on a lock screen as
    though it failed to load."""
    event = make_event(beboer)
    subscribe(beboer, "https://push.example/a")
    client.force_login(other)
    photo = SimpleUploadedFile(
        "kvittering.jpg", bytes.fromhex("ffd8ff") + b"x" * 512, content_type="image/jpeg"
    )

    client.post(f"{EVENTS}{event.pk}/kommentar", {"body": "", "image": photo})

    assert len(pushes) == 1
    assert pushes[0][1]["body"].strip() not in ("", event.title + ": ")
    assert "Billede" in pushes[0][1]["body"]


def test_the_comment_form_carries_the_attachment_note_hooks(client: Client, beboer: Resident) -> None:
    """The hooks frontend/src/feed.ts finds the note by. The BEHAVIOUR (thumbnail, filename, the
    remove button) is JavaScript and this project has no JS test runner, so what is asserted here is
    the contract between the template and that module: the note exists, it starts hidden, and it
    carries the three data attributes the handler queries. Rename one of those without the other and
    the paperclip silently stops giving any feedback at all - which is the state this replaced.
    """
    event = make_event(beboer)
    client.force_login(beboer)

    body = client.get(f"{EVENTS}{event.pk}").content.decode()

    assert "data-file-note" in body
    assert "data-file-note-thumb" in body
    assert "data-file-note-name" in body
    assert "data-file-note-clear" in body
    assert '<div class="file-note" data-file-note hidden>' in body


def test_a_private_event_is_absent_from_the_list_for_an_outsider(
    client: Client, beboer: Resident, other: Resident
) -> None:
    make_private(beboer, [], title="Hemmelig fest")
    client.force_login(other)

    assert "Hemmelig fest" not in client.get(EVENTS).content.decode()


def test_an_invitee_sees_the_private_event_everywhere(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """The positive control. Without it the whole section passes on a feature that shows nothing to
    anybody."""
    event = make_private(beboer, [other], title="Hemmelig fest")
    client.force_login(other)

    assert "Hemmelig fest" in client.get(EVENTS).content.decode()
    assert client.get(f"{EVENTS}{event.pk}").status_code == 200


def test_a_co_organiser_sees_a_private_event_they_were_not_invited_to(
    client: Client, beboer: Resident, other: Resident
) -> None:
    event = make_private(beboer, [])
    event.co_organisers.add(other)
    client.force_login(other)

    assert client.get(f"{EVENTS}{event.pk}").status_code == 200


def test_moderators_do_not_see_private_events(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """Deliberate: "except Inspektionen" would make the privacy promise false in exactly the case
    anyone would care about. A reported private event is a superuser job in the Django admin."""
    inspektion = make_resident(email="i@gahk.dk", roles=(Role.INSPEKTION,))
    event = make_private(beboer, [])
    client.force_login(inspektion)

    assert client.get(f"{EVENTS}{event.pk}").status_code == 404


def test_the_visibility_filter_never_duplicates_an_event(beboer: Resident, other: Resident) -> None:
    """Subqueries rather than joins. `Q(invites__resident=r)` is multi-valued, so somebody who is
    BOTH a co-organiser and an invitee would come back twice — a duplicate card, and two VEVENTs
    sharing one UID in a calendar file, which some clients resolve by dropping both."""
    event = make_private(beboer, [other])
    event.co_organisers.add(other)

    assert access.visible_to(other).filter(pk=event.pk).count() == 1


def test_the_visibility_filter_survives_an_annotation(beboer: Resident, other: Resident) -> None:
    """`.distinct()` would paper over the duplicate above and then silently stop being enough here,
    because a join multiplies rows before an aggregate reaches them."""
    from django.db.models import Count

    event = make_private(beboer, [other])
    event.co_organisers.add(other)
    services.set_answer(event.pk, other, Answer.JA)

    annotated = access.visible_to(other).annotate(n=Count("rsvps")).get(pk=event.pk)
    assert annotated.n == 1


def test_a_private_event_notifies_only_its_invitees(
    client: Client, beboer: Resident, other: Resident, third: Resident, pushes: list
) -> None:
    """Announcing a private event to the house IS the leak. The invite list is the audience."""
    for who in (beboer, other, third):
        subscribe(who, f"https://push.example/{who.pk}")
    client.force_login(beboer)
    pushes.clear()

    event = make_private(beboer, [other], title="Hemmelig fest")
    services.notify_new_event(event)

    assert [p[0] for p in pushes] == [[other.pk]]


# --- the four-invitee minimum ----------------------------------------------------------------------


def _local(naive: datetime.datetime) -> datetime.datetime:
    """A wall-clock time in Europe/Copenhagen, as an aware datetime."""
    return timezone.make_aware(naive)


def _future_month_first() -> datetime.date:
    """The 1st of a month two ahead of today.

    Far enough that nothing placed in it is near `RETENTION_AFTER_END`, which is what makes a date
    in a calendar test safe: `calendar()` purges on every request, so an event fixed to a literal
    date stops existing a week after that date passes, and the test then fails for a reason
    unrelated to what it is testing.
    """
    return (timezone.localdate().replace(day=1) + datetime.timedelta(days=62)).replace(day=1)


def _month_with_trailing_padding() -> tuple[datetime.date, datetime.date]:
    """A month safely in the future whose grid runs past its own end, and the first padding day.

    DERIVED, NOT HARDCODED, and the reason is a trap worth naming: `calendar()` calls
    `purge_expired()` on every request, and an event is hard-deleted a week after it ends
    (RETENTION_AFTER_END). These two tests originally pinned an event to 2026-09-01 and asked for
    the August grid, which worked perfectly until the real date passed 2026-09-08 — at which point
    the view deleted the event before rendering it, and the padding tests began failing for a reason
    that has nothing to do with padding. A fixed date in a test that exercises a retention-swept view
    is a time bomb with a known fuse length.

    Two months out, so the event is never near the purge window. Months whose last day is a Sunday
    have no trailing padding at all, so it walks forward until it finds one that does.
    """
    first = _future_month_first()
    for _ in range(12):
        next_first = (first + datetime.timedelta(days=32)).replace(day=1)
        last_day = next_first - datetime.timedelta(days=1)
        if last_day.weekday() != 6:  # Sunday-ending months fill the last row exactly
            return first, next_first
        first = next_first
    raise AssertionError("no month with trailing padding within a year — calendar maths is wrong")


_room_seq = iter(range(1, 10_000))


def _residency(resident: Resident) -> None:
    """Put a resident on this month's list — only current residents are invitable.

    conftest.make_resident deliberately does NOT create a Residency row, so anything touching the
    picker has to. Room shape copied from test_ak._room.
    """
    from core.models import Room
    from residents.models import Residency, active_period

    n = next(_room_seq)
    room = Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")
    year, month = active_period()
    Residency.objects.get_or_create(resident=resident, room=room, year=year, month=month)


def test_an_invite_only_event_needs_four_invitees(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    guests = [make_resident(email=f"g{i}@gahk.dk") for i in range(4)]
    for who in guests:
        _residency(who)
    client.force_login(beboer)
    when = timezone.localtime(timezone.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

    too_few = client.post(
        EVENTS + "opret",
        {
            "title": "For lille",
            "starts_at": when,
            "visibility": Visibility.KUN_INVITEREDE,
            "invitees": [g.pk for g in guests[:3]],
        },
    )
    assert too_few.status_code == 200  # re-rendered with the error, not saved
    assert not Event.objects.exists()

    client.post(
        EVENTS + "opret",
        {
            "title": "Stor nok",
            "starts_at": when,
            "visibility": Visibility.KUN_INVITEREDE,
            "invitees": [g.pk for g in guests],
        },
    )
    assert Event.objects.get().invites.count() == 4


def test_the_minimum_is_enforced_on_edit_too(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The create-only version of this check is the obvious bug: switching an existing open event to
    invite-only is exactly the transition it would miss."""
    event = make_event(beboer)
    client.force_login(beboer)

    client.post(
        f"{EVENTS}{event.pk}/rediger",
        {
            "title": event.title,
            "starts_at": timezone.localtime(event.starts_at).strftime("%Y-%m-%dT%H:%M"),
            "visibility": Visibility.KUN_INVITEREDE,
        },
    )

    event.refresh_from_db()
    assert event.visibility == Visibility.AABENT


def test_the_organiser_cannot_be_their_own_invitee(beboer: Resident, other: Resident) -> None:
    """Otherwise the four-person minimum is satisfiable by inviting yourself."""
    from events.forms import invitable_residents

    for who in (beboer, other):
        _residency(who)

    assert beboer.pk not in {r.pk for r in invitable_residents(exclude=beboer)}
    assert other.pk in {r.pk for r in invitable_residents(exclude=beboer)}


def test_alumni_are_not_offered_as_invitees(beboer: Resident, other: Resident) -> None:
    """The resident table holds everyone who ever lived here. A picker offering four hundred names,
    most of whom moved out years ago, is not a picker."""
    from events.forms import invitable_residents

    _residency(other)  # only `other` lives here this month

    assert {r.pk for r in invitable_residents()} == {other.pk}


def test_syncing_invites_adds_and_removes_without_churning_the_rest(
    beboer: Resident, other: Resident, third: Resident
) -> None:
    """Reconciled, not cleared-and-rewritten: a rewrite would make invited_at lie on every edit and
    would re-notify people who were on the list the whole time."""
    event = make_private(beboer, [other])
    original = event.invites.get(resident=other)

    added = services.sync_invites(event, [other, third], invited_by=beboer)

    assert [r.pk for r in added] == [third.pk]
    assert event.invites.get(resident=other).invited_at == original.invited_at

    services.sync_invites(event, [third], invited_by=beboer)
    assert [i.resident_id for i in event.invites.all()] == [third.pk]


# --- the calendar -----------------------------------------------------------------------------------


def test_the_month_grid_shows_whole_weeks(client: Client, beboer: Resident) -> None:
    """Padded into the neighbouring months rather than left blank: a grid starting mid-row reads as
    broken, and an event on the 1st is worth seeing from the 30th."""
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09")

    weeks = response.context["weeks"]
    assert all(len(week) == 7 for week in weeks)
    assert weeks[0][0]["date"].weekday() == 0  # Monday first, as a Danish calendar is printed


def test_an_event_in_a_padding_day_actually_appears_there(client: Client, beboer: Resident) -> None:
    """The padding days are queried too, not just drawn.

    A month that does not end on a Sunday has its last row run into the next one — so an event on
    the 1st of the following month has a cell on this month's page. The first version of this view
    filtered by the MONTH, so the cell rendered, empty, while the event sat one click away. A
    calendar that draws a day and hides what is on it is worse than one that does not draw the day.
    """
    month, padding_day = _month_with_trailing_padding()
    make_event(
        beboer,
        title="I paddingen",
        starts_at=_local(datetime.datetime.combine(padding_day, datetime.time(19, 0))),
    )
    client.force_login(beboer)

    body = client.get(f"{EVENTS}kalender?maaned={month:%Y-%m}").content.decode()

    assert "I paddingen" in body


def test_a_junk_month_falls_back_to_this_one(client: Client, beboer: Resident) -> None:
    """A mistyped URL should show a calendar, not a stack trace."""
    client.force_login(beboer)
    assert client.get(f"{EVENTS}kalender?maaned=noget-vrøvl").status_code == 200


# The day panel is the phone rendering: seven columns wide enough to read leave room for a dot and
# not for a title, so the cells carry dots and a tap spells the day out underneath. It is a URL
# parameter rather than JavaScript, which is also what makes it testable at all.


def test_tapping_a_day_lists_that_days_events(client: Client, beboer: Resident) -> None:
    """The panel shows the tapped day and only the tapped day.

    Dates derived from today rather than written out: see `_future_month_first`. This test
    originally pinned both events to September 2026 and began failing a week later, when
    `calendar()`'s purge deleted them before the view could render them.
    """
    day = _future_month_first() + datetime.timedelta(days=11)  # the 12th; the 13th stays in-month
    neighbour = day + datetime.timedelta(days=1)
    make_event(
        beboer, title="Den dag", starts_at=_local(datetime.datetime.combine(day, datetime.time(19, 0)))
    )
    make_event(
        beboer,
        title="En anden dag",
        starts_at=_local(datetime.datetime.combine(neighbour, datetime.time(19, 0))),
    )
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned={day:%Y-%m}&dag={day:%Y-%m-%d}")

    assert response.context["chosen_day"] == day
    assert [e.title for e in response.context["chosen_events"]] == ["Den dag"]


def test_a_quiet_day_says_so_rather_than_rendering_nothing(client: Client, beboer: Resident) -> None:
    """An empty panel would read as a broken tap. "Der sker ikke noget den dag" is an answer."""
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09&dag=2026-09-12")

    assert response.context["chosen_day"] == datetime.date(2026, 9, 12)
    assert "Der sker ikke noget den dag" in response.content.decode()


def test_no_day_panel_unless_a_day_was_asked_for(client: Client, beboer: Resident) -> None:
    """Not defaulted to today: it would be redundant beside the desktop chips, and "ingen
    begivenheder i dag" is not what someone opening a month wants to read first."""
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09")

    assert response.context["chosen_day"] is None
    assert 'id="dag"' not in response.content.decode()


@pytest.mark.parametrize("raw", ["noget-vrøvl", "2026-13-40", "", "2019-04-01"])
def test_an_unusable_day_is_treated_as_absent(client: Client, beboer: Resident, raw: str) -> None:
    """Junk, and also a real date that is nowhere on the visible grid — `?dag=2019-04-01` would
    otherwise print a heading and a list for a day the page does not show, which reads as a bug."""
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09&dag={raw}")

    assert response.status_code == 200
    assert response.context["chosen_day"] is None


def test_a_day_in_the_padding_can_still_be_opened(client: Client, beboer: Resident) -> None:
    """The grid draws the next month's 1st on this month's page, so tapping it there has to work —
    the span the day is validated against is the GRID's, not the month's."""
    month, padding_day = _month_with_trailing_padding()
    make_event(
        beboer,
        title="I paddingen",
        starts_at=_local(datetime.datetime.combine(padding_day, datetime.time(19, 0))),
    )
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned={month:%Y-%m}&dag={padding_day:%Y-%m-%d}")

    assert [e.title for e in response.context["chosen_events"]] == ["I paddingen"]


def test_a_private_event_is_absent_from_the_day_panel_for_an_outsider(
    client: Client, beboer: Resident, other: Resident
) -> None:
    """The panel reads the same `by_day` the grid does, so it inherits visible_to — but a leak here
    would be a leak with the title spelled out in full, which the grid's dots are not."""
    day = _future_month_first() + datetime.timedelta(days=11)
    make_private(
        beboer,
        [],
        title="Hemmelig fest",
        starts_at=_local(datetime.datetime.combine(day, datetime.time(19, 0))),
    )
    client.force_login(other)

    response = client.get(f"{EVENTS}kalender?maaned={day:%Y-%m}&dag={day:%Y-%m-%d}")

    # Derived, not written out — and here the fixed date was the more dangerous kind of rot. This
    # test asserts an ABSENCE, so once the purge started deleting the event before the view ran, it
    # went on passing while no longer able to detect the leak it exists to catch.
    assert response.context["chosen_events"] == []
    assert "Hemmelig fest" not in response.content.decode()


def test_a_private_event_is_absent_from_the_calendar_for_an_outsider(
    client: Client, beboer: Resident, other: Resident
) -> None:
    when = timezone.now() + datetime.timedelta(days=3)
    make_private(beboer, [], title="Hemmelig fest", starts_at=when)
    client.force_login(other)

    body = client.get(f"{EVENTS}kalender?maaned={when:%Y-%m}").content.decode()

    assert "Hemmelig fest" not in body


# --- fødselsdage ------------------------------------------------------------------------------------
#
# The module under test is residents/birthdays.py, not this app — a birthday is a fact about a
# PERSON, and the month grid is only its first caller. The tests live here because the calendar is
# where the behaviour is visible, and because the thing most worth pinning is the boundary: a
# birthday is a note printed on a day and must never become an Event, with an organiser, an answer
# form, a push, or a line in anybody's .ics.


def _celebrant(make_resident: Callable[..., Resident], born: datetime.date, **extra: object) -> Resident:
    """A resident on this month's list with a date of birth. Both halves are needed: `birthdays`
    only considers the active residency list, which conftest.make_resident does not create."""
    extra.setdefault("email", f"f{born:%m%d}{next(_room_seq)}@gahk.dk")
    resident = make_resident(birthday=born, **extra)
    _residency(resident)
    return resident


def test_a_birthday_is_a_note_on_the_day_not_an_event(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The whole point of the feature, in one assertion pair: the name is on the calendar, and
    nothing was written to Event. If this ever fails the other way round, birthdays have acquired
    an organiser, an RSVP form, a push audience and a VEVENT in sixty subscribed calendars."""
    _celebrant(make_resident, datetime.date(2001, 9, 12), first_name="Mette", last_name="Hansen")
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09")

    assert "Mette" in response.content.decode()
    assert not Event.objects.exists()


def test_the_birthday_lands_in_its_own_cell(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    _celebrant(make_resident, datetime.date(2001, 9, 12), first_name="Mette", last_name="Hansen")
    client.force_login(beboer)

    weeks = client.get(f"{EVENTS}kalender?maaned=2026-09").context["weeks"]

    marked = {
        cell["date"]: [b.resident.first_name for b in cell["birthdays"]]
        for week in weeks
        for cell in week
        if cell["birthdays"]
    }
    assert marked == {datetime.date(2026, 9, 12): ["Mette"]}


def test_an_alumne_is_not_on_the_calendar(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The directory holds every resident the ETL has ever imported. Without the residency filter a
    calendar belonging to sixty people would carry several hundred names a year."""
    make_resident(email="gammel@gahk.dk", first_name="Fraflyttet", birthday=datetime.date(1994, 9, 12))
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09")

    assert "Fraflyttet" not in response.content.decode()
    assert all(not cell["birthdays"] for week in response.context["weeks"] for cell in week)


def test_the_calendar_flags_exactly_the_people_on_the_active_alumneliste(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The condition, asserted against the alumneliste's OWN query rather than against a
    hand-written idea of it — so the two cannot drift apart without this failing.

    Three people share the date, and only one of them lives here now:

      * on the current month's list           -> flagged
      * never on any list (a plain directory row, as the ETL leaves an alumne) -> not flagged
      * on LAST year's list only              -> not flagged, and still on that period of the
        alumneliste, which is the point: moving out is not a flag anybody has to remember to set,
        it is simply not being on the month the calendar asks about.
    """
    from residents.models import Residency, active_period
    from residents.views import _directory_rows

    born = datetime.date(2001, 9, 12)
    here = _celebrant(make_resident, born, first_name="Nuvaerende", last_name="Beboer")
    make_resident(email="aldrig@gahk.dk", first_name="Aldrig", birthday=born)
    gone = _celebrant(make_resident, born, first_name="Fraflyttet", last_name="Person")
    year, month = active_period()
    Residency.objects.filter(resident=gone).update(year=year - 1)

    client.force_login(beboer)
    response = client.get(f"{EVENTS}kalender?maaned=2026-09&dag=2026-09-12")

    on_the_list = _directory_rows(year, month, "")
    expected = {
        row.resident_id
        for row in on_the_list
        if row.resident.birthday and (row.resident.birthday.month, row.resident.birthday.day) == (9, 12)
    }
    assert {b.resident.pk for b in response.context["chosen_birthdays"]} == expected == {here.pk}

    body = response.content.decode()
    assert "Aldrig" not in body
    assert "Fraflyttet" not in body
    # Not deleted — merely off this month's list, and still on the one they were on.
    assert Residency.objects.filter(resident=gone, year=year - 1).exists()


def test_a_resident_without_a_birthday_is_simply_absent(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """Most of the legacy import has one; some rows do not, and a null must not reach the grid."""
    resident = make_resident(email="ukendt@gahk.dk", first_name="Ukendt")
    _residency(resident)
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09")

    assert response.status_code == 200
    assert "Ukendt" not in response.content.decode()


def test_a_birthday_in_a_padding_day_appears_there_too(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """Same rule the events already follow: August 2026's grid runs to 6 September, so the query
    covers the GRID's span. A name that shows up only after you click through to the next month is
    a name the calendar failed to tell you about."""
    _celebrant(make_resident, datetime.date(2001, 9, 1), first_name="Padding", last_name="Person")
    client.force_login(beboer)

    body = client.get(f"{EVENTS}kalender?maaned=2026-08").content.decode()

    assert "Padding" in body


def test_the_day_panel_names_the_celebrant_and_the_age(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    _celebrant(make_resident, datetime.date(2001, 9, 12), first_name="Mette", last_name="Hansen")
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09&dag=2026-09-12")

    assert [b.resident.first_name for b in response.context["chosen_birthdays"]] == ["Mette"]
    body = response.content.decode()
    assert "Mette Hansen" in body
    assert "Fylder 25" in body


def test_a_day_with_only_a_birthday_does_not_say_nothing_happens(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """A flag on the cell and "der sker ikke noget den dag" underneath it reads as a bug — which is
    why the empty message tests BOTH lists rather than the events' own {% empty %} clause."""
    _celebrant(make_resident, datetime.date(2001, 9, 12), first_name="Mette", last_name="Hansen")
    client.force_login(beboer)

    body = client.get(f"{EVENTS}kalender?maaned=2026-09&dag=2026-09-12").content.decode()

    assert "Der sker ikke noget den dag" not in body


def test_a_day_with_neither_still_says_nothing_happens(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The other half of the pair — the message has to survive the extra condition."""
    _celebrant(make_resident, datetime.date(2001, 9, 12), first_name="Mette", last_name="Hansen")
    client.force_login(beboer)

    body = client.get(f"{EVENTS}kalender?maaned=2026-09&dag=2026-09-13").content.decode()

    assert "Der sker ikke noget den dag" in body


def test_a_birthday_only_day_can_still_be_tapped_open(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """On a phone the flag is the entire marking, so a marked cell that does not open is a cell
    that looks broken. The tap link used to be rendered only for days carrying events."""
    _celebrant(make_resident, datetime.date(2001, 9, 12), first_name="Mette", last_name="Hansen")
    client.force_login(beboer)

    body = client.get(f"{EVENTS}kalender?maaned=2026-09").content.decode()

    assert "dag=2026-09-12" in body


def test_a_birthday_never_reaches_a_subscribed_calendar(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The .ics feed is the one place where "it is only a note" stops being a rendering detail: an
    entry that lands there is copied into Google and Apple and outlives anything done here."""
    _celebrant(make_resident, datetime.date(2001, 9, 12), first_name="Mette", last_name="Hansen")

    body = client.get(_feed_url(beboer)).content.decode()

    assert "Mette" not in body
    assert "BEGIN:VEVENT" not in body


def test_an_unbelievable_birth_year_shows_no_age(
    client: Client, beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    """The legacy import carried a few placeholder years. "Fylder 126" on the kollegium's calendar
    is worse than saying nothing about the age at all — the name still shows."""
    _celebrant(make_resident, datetime.date(1900, 9, 12), first_name="Gammel", last_name="Data")
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-09&dag=2026-09-12")

    assert response.context["chosen_birthdays"][0].turning is None
    body = response.content.decode()
    assert "Gammel Data" in body
    assert "Fylder" not in body


# The date arithmetic on its own. Two cases the calendar cannot exercise by rendering a month,
# because both need a span the grid only produces at particular times of year.


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, datetime.date(2024, 2, 29)), (2026, datetime.date(2026, 3, 1))],
)
def test_the_29th_of_february_is_celebrated_on_the_1st_of_march_in_ordinary_years(
    year: int, expected: datetime.date
) -> None:
    """Three years in four the date names no day at all, and `date(year, 2, 29)` raises rather than
    saying so — so the alternative to deciding is dropping somebody off the calendar for three
    years running and never noticing. Danish practice is 1 March."""
    from residents.birthdays import celebrated_on

    assert celebrated_on(datetime.date(2004, 2, 29), year) == expected


def test_a_span_that_crosses_new_year_finds_birthdays_on_both_sides(
    make_resident: Callable[..., Resident],
) -> None:
    """December's grid pads into January, so a span is not confined to one month or even one year —
    which is why the lookup asks per year IN THE SPAN rather than per date of birth."""
    from residents.birthdays import in_span

    _celebrant(make_resident, datetime.date(2001, 12, 30), first_name="Decem", last_name="Ber")
    _celebrant(make_resident, datetime.date(2002, 1, 2), first_name="Janu", last_name="Ar")

    found = in_span(datetime.date(2026, 12, 28), datetime.date(2027, 1, 3))

    assert {day: [b.resident.first_name for b in bs] for day, bs in found.items()} == {
        datetime.date(2026, 12, 30): ["Decem"],
        datetime.date(2027, 1, 2): ["Janu"],
    }


# --- ics: one event ---------------------------------------------------------------------------------


def test_the_file_uses_crlf_and_wraps_in_a_vcalendar(beboer: Resident) -> None:
    event = make_event(beboer)
    body = icalendar_mod.one_event(event)

    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert body.endswith("END:VCALENDAR\r\n")
    assert "\n" not in body.replace("\r\n", "")


def test_a_long_danish_title_folds_without_splitting_a_character(beboer: Resident) -> None:
    """Folding is by OCTET, not character. `æ ø å` are two bytes each, so counting characters
    overshoots and slicing bytes naively splits a continuation byte into mojibake."""
    title = "Fællesspisning med grøn karry, vårruller og hjemmebagt brød på tværs af hele gården"
    event = make_event(beboer, title=title)

    body = icalendar_mod.one_event(event)

    for line in body.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    # Unfolding puts it back together byte-exactly — the real test of the boundary walk. Mind the
    # comma: folding happens AFTER escaping, so what comes back out is the ESCAPED title.
    unfolded = body.replace("\r\n ", "")
    assert f"SUMMARY:{title.replace(',', r'\,')}" in unfolded


def test_text_escaping_happens_in_the_right_order(beboer: Resident) -> None:
    """Backslash first, or every other escape gets double-escaped."""
    event = make_event(beboer, title=r"Fest; med, komma og \backslash")

    unfolded = icalendar_mod.one_event(event).replace("\r\n ", "")

    assert r"SUMMARY:Fest\; med\, komma og \\backslash" in unfolded


def test_newlines_in_a_description_become_escaped_n(beboer: Resident) -> None:
    """A raw newline inside a value ends the property early and orphans the rest of the text."""
    event = make_event(beboer, description="Første linje\n\nAnden linje")

    body = icalendar_mod.one_event(event)
    unfolded = body.replace("\r\n ", "")

    # One `\n`, not two: the value goes through `plain_text` first, which collapses the blank line
    # along with the rest of the Markdown. The point here is that neither one survives as a real LF.
    assert r"DESCRIPTION:Første linje\nAnden linje" in unfolded
    # And no bare LF survived anywhere: every line break in the file is a CRLF.
    assert "\n" not in body.replace("\r\n", "")


def test_a_summer_and_a_winter_event_both_land_on_the_right_utc_hour(beboer: Resident) -> None:
    """The test that justifies emitting no VTIMEZONE. 17.00 local is 15:00Z in July (CEST) and
    16:00Z in January (CET); a hand-rolled VTIMEZONE is where this normally goes wrong."""
    summer = make_event(beboer, starts_at=_local(datetime.datetime(2026, 7, 15, 17, 0)), title="Sommer")
    winter = make_event(beboer, starts_at=_local(datetime.datetime(2027, 1, 15, 17, 0)), title="Vinter")

    assert "DTSTART:20260715T150000Z" in icalendar_mod.one_event(summer)
    assert "DTSTART:20270115T160000Z" in icalendar_mod.one_event(winter)
    assert "VTIMEZONE" not in icalendar_mod.one_event(summer)


def test_an_event_with_no_end_gets_a_real_duration(beboer: Resident) -> None:
    """A VEVENT with neither DTEND nor DURATION is an instant, which clients render as a
    one-minute sliver you cannot read."""
    event = make_event(beboer, ends_at=None)
    body = icalendar_mod.one_event(event)

    assert "DTEND:" in body
    assert body.count("DTSTART:") == 1


def test_the_uid_is_stable_and_identical_in_download_and_feed(beboer: Resident) -> None:
    """A client keys on the UID, so an edit must update the entry rather than create a second one."""
    event = make_event(beboer)
    services.set_answer(event.pk, beboer, Answer.JA)
    before = icalendar_mod.one_event(event)

    event.title = "Nyt navn"
    event.sequence += 1
    event.save()

    after = icalendar_mod.one_event(event)
    in_feed = icalendar_mod.feed([(event, False)], name="test")
    uid = f"UID:begivenhed-{event.pk}@gahk.dk"
    assert uid in before
    assert uid in after
    assert uid in in_feed


def test_editing_the_time_increments_sequence_in_the_file(beboer: Resident) -> None:
    event = make_event(beboer)
    assert "SEQUENCE:0" in icalendar_mod.one_event(event)

    event.sequence = 1
    assert "SEQUENCE:1" in icalendar_mod.one_event(event)


def test_no_method_property_is_emitted(beboer: Resident) -> None:
    """METHOD:PUBLISH turns the file into an iTIP message, and Outlook then treats a subscribed
    feed as an invitation to accept — which produces ghost and duplicate entries."""
    assert "METHOD" not in icalendar_mod.one_event(make_event(beboer))


def test_no_attendee_or_organizer_leaks_an_email_address(beboer: Resident, other: Resident) -> None:
    """A security decision, not a simplification: these files land on Google's and Apple's servers.
    The guest list stays on the kollegium's own site."""
    event = make_event(beboer)
    services.set_answer(event.pk, other, Answer.JA)

    body = icalendar_mod.one_event(event)

    assert "ATTENDEE" not in body
    assert "ORGANIZER" not in body
    assert "mailto" not in body
    assert other.email not in body


def test_a_cancelled_event_still_emits_as_cancelled(beboer: Resident) -> None:
    """The reason Aflys exists rather than delete: once the row is gone there is nothing left to
    retract the entry with."""
    event = make_event(beboer)
    services.cancel(event)

    assert "STATUS:CANCELLED" in icalendar_mod.one_event(event)


def test_the_output_parses_with_an_independent_implementation(beboer: Resident) -> None:
    """Hand-rolled in production, checked in CI by somebody else's parser — a better answer than
    either alone. `icalendar` is a dev-only dependency for exactly this."""
    import icalendar as oracle

    event = make_event(beboer, title="Fællesspisning; grøn karry", location="Køkkenet, 1. sal", ends_at=None)

    parsed = oracle.Calendar.from_ical(icalendar_mod.one_event(event))
    vevent = next(c for c in parsed.walk() if c.name == "VEVENT")

    assert str(vevent["SUMMARY"]) == "Fællesspisning; grøn karry"
    assert str(vevent["LOCATION"]) == "Køkkenet, 1. sal"
    assert str(vevent["UID"]) == event.ical_uid


# --- the subscribable feed --------------------------------------------------------------------------


def _feed_url(resident: Resident) -> str:
    return f"/kalender/{CalendarFeedToken.for_resident(resident).token}.ics"


def test_the_feed_needs_no_session(client: Client, beboer: Resident) -> None:
    """The whole point. Google's servers fetch this, not the subscriber's browser — no cookies, no
    login form, no redirect to follow. Behind @login_required this would 200 with the login page's
    HTML and Google would show an empty calendar with no error anywhere."""
    make_event(beboer)
    url = _feed_url(beboer)

    response = Client().get(url)  # deliberately not logged in

    assert response.status_code == 200
    assert response["Content-Type"] == "text/calendar; charset=utf-8"


def test_the_feed_contains_only_what_i_said_ja_to(client: Client, beboer: Resident) -> None:
    mine = make_event(beboer, title="Jeg kommer")
    make_event(beboer, title="Ikke svaret")
    declined = make_event(beboer, title="Sagt nej")
    services.set_answer(mine.pk, beboer, Answer.JA)
    services.set_answer(declined.pk, beboer, Answer.NEJ)

    body = Client().get(_feed_url(beboer)).content.decode()

    assert "Jeg kommer" in body
    assert "Ikke svaret" not in body
    assert "Sagt nej" not in body


def test_a_waitlisted_event_is_tentative_and_transparent(beboer: Resident, other: Resident) -> None:
    """It shows in the calendar but does not block the time — queuing for a trip should not make you
    look busy for an afternoon you may not get."""
    event = make_event(beboer, capacity=1, title="Fuld tur")
    services.set_answer(event.pk, other, Answer.JA)
    services.set_answer(event.pk, beboer, Answer.JA)  # beboer is second, so queued

    body = Client().get(_feed_url(beboer)).content.decode()

    assert "STATUS:TENTATIVE" in body
    assert "TRANSP:TRANSPARENT" in body


def test_the_feed_never_contains_a_private_event_i_was_not_invited_to(
    beboer: Resident, other: Resident
) -> None:
    """The leak that would matter most, because the file leaves the building."""
    event = make_private(other, [], title="Hemmelig fest")
    Rsvp.objects.create(event=event, resident=beboer, answer=Answer.JA, answered_at=timezone.now())

    body = Client().get(_feed_url(beboer)).content.decode()

    assert "Hemmelig fest" not in body


def test_the_calendar_declares_a_name_and_a_refresh_interval(beboer: Resident) -> None:
    """Both spellings of the name: NAME is RFC 7986, X-WR-CALNAME is what Apple and Google read.
    Without them the subscription is called "gahk.dk"."""
    body = Client().get(_feed_url(beboer)).content.decode()

    assert "X-WR-CALNAME:GAHK" in body
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT1H" in body


def test_an_unknown_token_is_404_not_403(client: Client) -> None:
    """403 would confirm a token once existed."""
    assert Client().get("/kalender/ikke-et-rigtigt-token.ics").status_code == 404


def test_a_rotated_token_stops_working_and_the_new_one_works(beboer: Resident) -> None:
    old_url = _feed_url(beboer)
    CalendarFeedToken.for_resident(beboer).rotate()

    assert Client().get(old_url).status_code == 404
    assert Client().get(_feed_url(beboer)).status_code == 200


def test_the_feed_response_refuses_to_be_cached_or_indexed(beboer: Resident) -> None:
    """The URL is a bearer credential, so it must not sit in a proxy or turn up in a crawler."""
    response = Client().get(_feed_url(beboer))

    assert "no-store" in response["Cache-Control"]
    assert response["X-Robots-Tag"] == "noindex, nofollow"
    assert response["Referrer-Policy"] == "no-referrer"
    # inline, not attachment: a subscribing client GETs this, and attachment would make a resident
    # who pastes the URL into a browser download a file instead of seeing it work.
    assert response["Content-Disposition"].startswith("inline")


def test_last_used_is_recorded_at_most_once_an_hour(beboer: Resident) -> None:
    """A write on every GET turns a read endpoint into a write storm — sixty phones, hourly."""
    url = _feed_url(beboer)
    Client().get(url)
    first = CalendarFeedToken.objects.get(resident=beboer).last_used_at
    assert first is not None

    Client().get(url)
    assert CalendarFeedToken.objects.get(resident=beboer).last_used_at == first


def test_the_token_appears_on_no_page_but_the_subscription_page(client: Client, beboer: Resident) -> None:
    make_event(beboer)
    token = CalendarFeedToken.for_resident(beboer).token
    client.force_login(beboer)

    assert token in client.get(f"{EVENTS}kalender/abonnement").content.decode()
    for path in (EVENTS, f"{EVENTS}kalender"):
        assert token not in client.get(path).content.decode()


def test_deleting_a_resident_deletes_their_token(beboer: Resident) -> None:
    CalendarFeedToken.for_resident(beboer)
    beboer.delete()
    assert not CalendarFeedToken.objects.exists()


# --- the deadline reminder ----------------------------------------------------------------------------


def test_the_reminder_goes_only_to_people_who_have_not_answered(
    beboer: Resident, other: Resident, third: Resident, pushes: list
) -> None:
    event = make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    for who in (beboer, other, third):
        _residency(who)
        subscribe(who, f"https://push.example/{who.pk}")
    services.set_answer(event.pk, other, Answer.JA)
    pushes.clear()

    services.send_deadline_reminder(event)

    # beboer is the organiser and other has answered, so only third is left.
    assert [p[0] for p in pushes] == [[third.pk]]


def test_running_the_reminder_twice_sends_one_reminder(
    beboer: Resident, other: Resident, pushes: list
) -> None:
    """Claimed with a conditional UPDATE whose rowcount decides, so two overlapping cron runs send
    one reminder between them. DEPLOY.md §4b requires every scheduled command to be idempotent."""
    event = make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    for who in (beboer, other):
        _residency(who)
    subscribe(other, "https://push.example/other")
    pushes.clear()

    services.send_deadline_reminder(event)
    services.send_deadline_reminder(event)

    assert len(pushes) == 1


def test_the_reminder_is_claimed_before_it_is_sent(beboer: Resident, other: Resident) -> None:
    """Order matters: a crash between claim and send costs one reminder, and the other order would
    push the whole house twice."""
    event = make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    services.send_deadline_reminder(event)

    event.refresh_from_db()
    assert event.reminder_sent_at is not None


def test_a_cancelled_event_sends_no_reminder(beboer: Resident) -> None:
    event = make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    services.cancel(event)

    assert services.events_needing_reminder() == []


def test_a_deadline_outside_the_window_sends_nothing_yet(beboer: Resident) -> None:
    make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(days=5))
    assert services.events_needing_reminder() == []


def test_a_private_events_reminder_stays_with_its_invitees(
    beboer: Resident, other: Resident, third: Resident, pushes: list
) -> None:
    event = make_private(beboer, [other], rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    for who in (other, third):
        _residency(who)
        subscribe(who, f"https://push.example/{who.pk}")
    pushes.clear()

    services.send_deadline_reminder(event)

    assert [p[0] for p in pushes] == [[other.pk]]


def test_the_lazy_backstop_sends_a_missed_reminder_on_page_load(
    client: Client, beboer: Resident, other: Resident, pushes: list
) -> None:
    """The AK idiom. A missed purge is wrong for nobody for months; a missed reminder is
    unrecoverable — the deadline passes and nobody learns why the event was under-attended."""
    event = make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    for who in (beboer, other):
        _residency(who)
    subscribe(other, "https://push.example/other")
    client.force_login(beboer)
    pushes.clear()

    client.get(EVENTS)

    event.refresh_from_db()
    assert event.reminder_sent_at is not None
    assert [p[0] for p in pushes] == [[other.pk]]


def test_the_backstop_never_breaks_the_page(
    client: Client, beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page load must never fail because push is misconfigured."""
    make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("push is down")

    monkeypatch.setattr(services, "send_deadline_reminder", explode)
    client.force_login(beboer)

    assert client.get(EVENTS).status_code == 200


def test_the_command_delivers_inline(beboer: Resident, other: Resident, pushes: list) -> None:
    """core.push defers to a daemon thread by default, and a management command's daemon threads are
    killed at interpreter shutdown — mid-fan-out, silently, a different number every night."""
    from django.core.management import call_command

    event = make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    for who in (beboer, other):
        _residency(who)
    subscribe(other, "https://push.example/other")
    pushes.clear()

    call_command("remind_rsvp_deadlines")

    event.refresh_from_db()
    assert event.reminder_sent_at is not None
    assert [p[0] for p in pushes] == [[other.pk]]


def test_the_command_dry_run_changes_nothing(beboer: Resident, pushes: list) -> None:
    from django.core.management import call_command

    event = make_event(beboer, rsvp_deadline_at=timezone.now() + datetime.timedelta(hours=2))
    pushes.clear()

    call_command("remind_rsvp_deadlines", "--dry-run")

    event.refresh_from_db()
    assert event.reminder_sent_at is None
    assert pushes == []


# --- things that must stay absent ---------------------------------------------------------------------


def test_views_never_reach_event_objects_directly() -> None:
    """Every read has to start from access.visible_to, or a private event leaks the first time
    somebody adds a view and forgets to filter it.

    A source check because no request-level test can prove the absence of a query nobody wrote yet.
    `Event.objects.purge_expired()` is the one allowed use — it deletes what is already past
    retention and reads nothing back to a reader.
    """
    source = (Path(__file__).resolve().parent.parent / "events" / "views.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    # Parsed rather than grepped: the module docstring and the comment beside the one allowed
    # use both mention `Event.objects`, and a substring search would flag its own explanation.
    uses = [
        lines[node.lineno - 1].strip()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and node.attr == "objects"
        and isinstance(node.value, ast.Name)
        and node.value.id == "Event"
        and "purge_expired" not in lines[node.lineno - 1]
    ]
    assert uses == [], "views must go through access.visible_to:\n" + "\n".join(uses)


def test_event_pages_leak_no_template_syntax(client: Client, beboer: Resident) -> None:
    """Django's {# … #} is single-line only, and an unclosed {% comment %} swallows the rest of the
    page silently. This has bitten three templates in this project already."""
    event = make_event(beboer, description="**Hej**", location="Køkkenet")
    services.set_answer(event.pk, beboer, Answer.JA)
    client.force_login(beboer)

    for path in (
        EVENTS,
        f"{EVENTS}{event.pk}",
        f"{EVENTS}opret",
        f"{EVENTS}{event.pk}/rediger",
        f"{EVENTS}kalender",
        f"{EVENTS}kalender/abonnement",
    ):
        body = client.get(path).content.decode()
        assert "Begivenhed" in body, f"{path} rendered nothing — the check would be vacuous"
        for leaked in ("{#", "#}", "{%", "%}", "{{", "}}"):
            assert leaked not in body, f"{path} leaked {leaked!r}"


def test_a_calendar_token_is_minted_lazily_and_is_unique(beboer: Resident, other: Resident) -> None:
    """Never backfilled by a migration: sixty secrets for people who may never use the feature, and
    a RunPython calling secrets.token_urlsafe bakes a non-deterministic step into the history."""
    assert not CalendarFeedToken.objects.exists()

    mine = CalendarFeedToken.for_resident(beboer)
    theirs = CalendarFeedToken.for_resident(other)

    assert mine.token != theirs.token
    assert len(mine.token) >= 32
    assert CalendarFeedToken.for_resident(beboer).pk == mine.pk  # get_or_create, not a second row


def test_rotating_a_token_changes_it(beboer: Resident) -> None:
    token = CalendarFeedToken.for_resident(beboer)
    before = token.token

    after = token.rotate()

    assert after != before
    assert token.rotated_at is not None


# --- the other end of the link, on the event page ----------------------------------------------------


def _notice(author: Resident, event: Event) -> object:
    from opslagstavle.models import Category, Notice

    return Notice.objects.create(
        author=author, category=Category.BEGIVENHED, body="Vi spiser sammen.", event=event
    )


@pytest.fixture
def board_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opslagstavlen is still gated to a trial group; these tests are about the link, not the gate."""
    from opslagstavle import access as board_access

    monkeypatch.setattr(board_access, "ACCESS_ROLES", None)


def test_the_event_page_lists_the_posts_about_it(client: Client, beboer: Resident, board_open: None) -> None:
    event = make_event(beboer)
    _notice(beboer, event)
    client.force_login(beboer)

    response = client.get(f"{EVENTS}{event.pk}")
    body = response.content.decode()

    assert "Omtalt på opslagstavlen" in body
    assert len(response.context["notices"]) == 1
    assert f"/intern/opslagstavle/{response.context['notices'][0].pk}" in body
    # The excerpt, not just the byline: on an event with two or three posts about it, what the post
    # says is the only thing that lets a reader pick. And rendered, so no `**` reaches the page.
    assert "Vi spiser sammen." in body
    assert "**" not in body


def test_no_board_link_for_somebody_who_cannot_open_the_board(
    client: Client, beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opslagstavlen is behind a role trial. Pointing a resident at a page that will 403 them is
    worse than not mentioning it — so the section is gated on the READER, not on the event."""
    from opslagstavle import access as board_access

    monkeypatch.setattr(board_access, "ACCESS_ROLES", (Role.ADMINISTRATOR,))
    event = make_event(beboer)
    _notice(beboer, event)
    client.force_login(beboer)

    response = client.get(f"{EVENTS}{event.pk}")

    assert response.context["notices"] == []
    assert "Omtalt på opslagstavlen" not in response.content.decode()
