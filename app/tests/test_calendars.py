"""The resident's calendar subscription (/calendar/settings): the settings page, and the feed it hands out."""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.utils import timezone

from calendars.models import CalendarSubscription, EventChoice
from calendars.views import STALE_AFTER
from events import services
from events.models import Answer, Rsvp
from residents.models import Resident
from tests.test_events import _residency, make_event, make_private

pytestmark = pytest.mark.django_db

SETTINGS = "/calendar/settings"


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="a@gahk.dk", first_name="Anton", last_name="Storgaard")


@pytest.fixture
def other(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="b@gahk.dk", first_name="Mette", last_name="Hansen")


def _subscription(resident: Resident, **choices: object) -> CalendarSubscription:
    subscription = CalendarSubscription.for_resident(resident)
    for name, value in choices.items():
        setattr(subscription, name, value)
    subscription.save()
    return subscription


def _feed_url(resident: Resident) -> str:
    return f"/calendar/feed/{CalendarSubscription.for_resident(resident).token}.ics"


def _unfolded(resident: Resident) -> str:
    return Client().get(_feed_url(resident)).content.decode().replace("\r\n ", "")


def _summaries(body: str) -> list[str]:
    return [line.removeprefix("SUMMARY:") for line in body.split("\r\n") if line.startswith("SUMMARY:")]


# --- the feed: who may fetch it ------------------------------------------------------------------


def test_the_feed_needs_no_session(beboer: Resident) -> None:
    """The whole point. Google's servers fetch this, not the subscriber's browser — no cookies, no
    login form. Behind @login_required this would 200 with the login page's HTML and Google would
    show an empty calendar with no error anywhere."""
    make_event(beboer)

    response = Client().get(_feed_url(beboer))  # deliberately not logged in

    assert response.status_code == 200
    assert response["Content-Type"] == "text/calendar; charset=utf-8"


def test_an_unknown_token_is_404_not_403() -> None:
    """403 would confirm a token once existed."""
    assert Client().get("/calendar/feed/ikke-et-rigtigt-token.ics").status_code == 404


def test_a_rotated_token_stops_working_and_the_new_one_works(beboer: Resident) -> None:
    old_url = _feed_url(beboer)
    CalendarSubscription.for_resident(beboer).rotate()

    assert Client().get(old_url).status_code == 404
    assert Client().get(_feed_url(beboer)).status_code == 200


def test_the_old_address_redirects_permanently(beboer: Resident) -> None:
    """gahk.dk's pass-through and anybody subscribed straight to intern still ask for /kalender/."""
    token = CalendarSubscription.for_resident(beboer).token

    response = Client().get(f"/kalender/{token}.ics")

    assert response.status_code == 301
    assert response["Location"] == f"/calendar/feed/{token}.ics"
    assert Client().get(f"/kalender/{token}.ics", follow=True).status_code == 200


def test_the_feed_response_refuses_to_be_cached_or_indexed(beboer: Resident) -> None:
    """The URL is a bearer credential, so it must not sit in a proxy or turn up in a crawler."""
    response = Client().get(_feed_url(beboer))

    assert "no-store" in response["Cache-Control"]
    assert response["X-Robots-Tag"] == "noindex, nofollow"
    assert response["Referrer-Policy"] == "no-referrer"
    # inline, not attachment: a subscribing client GETs this, and attachment would make a resident
    # who pastes the URL into a browser download a file instead of seeing it work.
    assert response["Content-Disposition"].startswith("inline")


def test_the_calendar_declares_a_name_and_a_refresh_interval(beboer: Resident) -> None:
    """Both spellings of the name: NAME is RFC 7986, X-WR-CALNAME is what Apple and Google read."""
    body = Client().get(_feed_url(beboer)).content.decode()

    assert "X-WR-CALNAME:GAHK" in body
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT1H" in body


# --- the feed: what is in it ---------------------------------------------------------------------


def test_by_default_every_visible_event_is_in_it_marked_by_my_answer(
    beboer: Resident, other: Resident
) -> None:
    coming = make_event(other, title="Jeg kommer")
    make_event(other, title="Fest")
    declined = make_event(other, title="Tur")
    services.set_answer(coming.pk, beboer, Answer.JA)
    services.set_answer(declined.pk, beboer, Answer.NEJ)

    summaries = _summaries(_unfolded(beboer))

    assert sorted(summaries) == ["Ikke svaret: Fest", "Jeg kommer", "Sagt nej: Tur"]


def test_only_a_ja_blocks_the_time(beboer: Resident, other: Resident) -> None:
    coming = make_event(other, title="Jeg kommer")
    make_event(other, title="Fest")
    services.set_answer(coming.pk, beboer, Answer.JA)

    body = _unfolded(beboer)

    assert body.count("TRANSP:OPAQUE") == 1
    assert body.count("TRANSP:TRANSPARENT") == 1


def test_accepted_only_leaves_out_what_i_have_not_said_ja_to(beboer: Resident, other: Resident) -> None:
    _subscription(beboer, events=EventChoice.ACCEPTED)
    coming = make_event(other, title="Jeg kommer")
    make_event(other, title="Ikke svaret endnu")
    declined = make_event(other, title="Sagt nej")
    services.set_answer(coming.pk, beboer, Answer.JA)
    services.set_answer(declined.pk, beboer, Answer.NEJ)

    assert _summaries(_unfolded(beboer)) == ["Jeg kommer"]


def test_a_waitlisted_event_is_tentative_and_transparent(beboer: Resident, other: Resident) -> None:
    """It shows in the calendar but does not block the time — queuing for a trip should not make you
    look busy for an afternoon you may not get."""
    event = make_event(beboer, capacity=1, title="Fuld tur")
    services.set_answer(event.pk, other, Answer.JA)
    services.set_answer(event.pk, beboer, Answer.JA)  # beboer is second, so queued

    body = _unfolded(beboer)

    assert "SUMMARY:Venteliste: Fuld tur" in body
    assert "STATUS:TENTATIVE" in body
    assert "TRANSP:TRANSPARENT" in body


def test_every_entry_links_to_its_event(beboer: Resident, settings: object) -> None:
    """In DESCRIPTION as well as URL: Google and Outlook never show URL."""
    settings.SITE_URL = "https://intern.gahk.dk"  # type: ignore[attr-defined]
    event = make_event(beboer, description="Tag en ret med")

    body = _unfolded(beboer)

    url = f"https://intern.gahk.dk/begivenheder/{event.pk}"
    assert f"URL:{url}" in body
    assert f"DESCRIPTION:Tag en ret med\\n\\n{url}" in body


def test_the_feed_never_contains_a_private_event_i_was_not_invited_to(
    beboer: Resident, other: Resident
) -> None:
    """The leak that would matter most, because the file leaves the building."""
    event = make_private(other, [], title="Hemmelig fest")
    Rsvp.objects.create(event=event, resident=beboer, answer=Answer.JA, answered_at=timezone.now())

    assert "Hemmelig fest" not in _unfolded(beboer)


def _celebrant(make_resident: Callable[..., Resident], born: datetime.date) -> Resident:
    resident = make_resident(email="f@gahk.dk", first_name="Mette", last_name="Hansen", birthday=born)
    _residency(resident)
    return resident


def test_birthdays_are_all_day_entries_that_do_not_block_time(
    beboer: Resident, make_resident: Callable[..., Resident]
) -> None:
    soon = timezone.localdate() + datetime.timedelta(days=10)
    celebrant = _celebrant(make_resident, soon.replace(year=soon.year - 23))

    body = _unfolded(beboer)

    assert "SUMMARY:Mette Hansen fylder 23" in body
    assert f"DTSTART;VALUE=DATE:{soon:%Y%m%d}" in body
    assert f"UID:fodselsdag-{celebrant.pk}-{soon.year}@gahk.dk" in body
    assert "TRANSP:TRANSPARENT" in body


def test_birthdays_can_be_switched_off(beboer: Resident, make_resident: Callable[..., Resident]) -> None:
    soon = timezone.localdate() + datetime.timedelta(days=10)
    _celebrant(make_resident, soon.replace(year=soon.year - 23))
    _subscription(beboer, include_birthdays=False)

    body = _unfolded(beboer)

    assert "Mette" not in body
    assert "BEGIN:VEVENT" not in body


def test_the_feed_parses_with_an_independent_implementation(
    beboer: Resident, other: Resident, make_resident: Callable[..., Resident]
) -> None:
    """Events and all-day birthdays together, read back by the `icalendar` package (dev only)."""
    import icalendar as oracle

    make_event(other, title="Fællesspisning, med æøå", location="Køkkenet")
    soon = timezone.localdate() + datetime.timedelta(days=10)
    _celebrant(make_resident, soon.replace(year=soon.year - 23))

    parsed = oracle.Calendar.from_ical(Client().get(_feed_url(beboer)).content)

    entries = {str(e["SUMMARY"]): e for e in parsed.walk("VEVENT")}
    assert set(entries) == {"Ikke svaret: Fællesspisning, med æøå", "Mette Hansen fylder 23"}
    assert entries["Mette Hansen fylder 23"].decoded("DTSTART") == soon


# --- the feed: how often it is fetched -----------------------------------------------------------


def test_every_fetch_is_counted(beboer: Resident) -> None:
    url = _feed_url(beboer)
    Client().get(url)
    Client().get(url)

    subscription = CalendarSubscription.objects.get(resident=beboer)
    assert subscription.fetch_count == 2
    assert subscription.last_used_at is not None


def test_a_new_address_starts_unused(beboer: Resident) -> None:
    Client().get(_feed_url(beboer))
    subscription = CalendarSubscription.objects.get(resident=beboer)

    subscription.rotate()

    subscription.refresh_from_db()
    assert subscription.fetch_count == 0
    assert subscription.last_used_at is None


# --- the settings page ---------------------------------------------------------------------------


def test_the_settings_page_needs_a_login() -> None:
    response = Client().get(SETTINGS)
    assert response.status_code == 302


def test_the_old_settings_address_redirects(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)
    response = client.get("/begivenheder/kalender/abonnement")
    assert response.status_code == 301
    assert response["Location"] == SETTINGS


def test_the_page_shows_the_address_to_copy_and_to_subscribe_with(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)
    token = CalendarSubscription.for_resident(beboer).token

    body = client.get(SETTINGS).content.decode()

    assert f'value="http://testserver/calendar/feed/{token}.ics"' in body
    assert f'href="webcal://testserver/calendar/feed/{token}.ics"' in body
    assert "data-copy=" in body
    for app in ("Google Kalender", "Outlook", "Apple Kalender"):
        assert app in body


def test_the_token_appears_on_no_page_but_the_settings_page(client: Client, beboer: Resident) -> None:
    make_event(beboer)
    token = CalendarSubscription.for_resident(beboer).token
    client.force_login(beboer)

    assert token in client.get(SETTINGS).content.decode()
    for path in ("/begivenheder/", "/begivenheder/kalender"):
        assert token not in client.get(path).content.decode()


def test_everything_is_on_by_default(client: Client, beboer: Resident) -> None:
    subscription = CalendarSubscription.for_resident(beboer)
    assert subscription.events == EventChoice.ALL
    assert subscription.include_birthdays


def test_saving_the_choices(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)

    response = client.post(SETTINGS, {"events": "accepted"})  # birthdays unticked

    assert response.status_code == 302
    subscription = CalendarSubscription.objects.get(resident=beboer)
    assert subscription.events == EventChoice.ACCEPTED
    assert not subscription.include_birthdays


def test_kitchen_and_cleaning_shifts_are_shown_but_not_yet_choosable(
    client: Client, beboer: Resident
) -> None:
    client.force_login(beboer)
    body = client.get(SETTINGS).content.decode()

    assert "Køkkenvagter" in body
    assert "Rengøringsvagter" in body
    assert body.count("checked disabled") == 2


def test_the_page_says_when_the_address_was_last_fetched(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)
    assert "Ikke hentet af en kalender-app endnu" in client.get(SETTINGS).content.decode()

    Client().get(_feed_url(beboer))

    body = client.get(SETTINGS).content.decode()
    assert "Sidst hentet af en kalender-app" in body
    assert "ikke hentet adressen i over en uge" not in body


def test_a_week_without_a_fetch_warns(client: Client, beboer: Resident) -> None:
    _subscription(beboer, last_used_at=timezone.now() - STALE_AFTER - datetime.timedelta(hours=1))
    client.force_login(beboer)

    assert "ikke hentet adressen i over en uge" in client.get(SETTINGS).content.decode()


def test_a_new_link_is_minted_on_post(client: Client, beboer: Resident) -> None:
    before = CalendarSubscription.for_resident(beboer).token
    client.force_login(beboer)

    response = client.post("/calendar/settings/new-link")

    assert response.status_code == 302
    assert CalendarSubscription.objects.get(resident=beboer).token != before


def test_a_token_is_minted_lazily_and_is_unique(beboer: Resident, other: Resident) -> None:
    """Never backfilled by a migration: sixty secrets for people who may never use the feature."""
    assert not CalendarSubscription.objects.exists()

    mine = CalendarSubscription.for_resident(beboer)
    theirs = CalendarSubscription.for_resident(other)

    assert mine.token != theirs.token
    assert len(mine.token) >= 32
    assert CalendarSubscription.for_resident(beboer).pk == mine.pk


def test_deleting_a_resident_deletes_their_subscription(beboer: Resident) -> None:
    CalendarSubscription.for_resident(beboer)
    beboer.delete()
    assert not CalendarSubscription.objects.exists()


def test_the_kitchen_account_may_not_open_the_settings_page(make_resident: Callable[..., Resident]) -> None:
    from core.middleware import kitchen_may_open

    assert not kitchen_may_open(SETTINGS)
    assert kitchen_may_open("/calendar/feed/abc.ics")
    assert not kitchen_may_open("/calendar/")
