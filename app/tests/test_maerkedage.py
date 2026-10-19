"""Kollegiets mærkedage (the founders' birthdays) on the calendar, and the dashboard's agenda that
replaced the embedded Google Calendar, and the invite picker's "Vælg alle beboere"."""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.utils import timezone

from events import agenda as agenda_mod
from events import maerkedage
from events.models import Event, Visibility
from residents.models import Resident

EVENTS = "/intern/begivenheder/"


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk")


def test_the_founders_birthdays_recur_every_year() -> None:
    span = maerkedage.in_span(datetime.date(2026, 1, 1), datetime.date(2027, 12, 31))

    assert [m.title for m in span[datetime.date(2026, 5, 16)]] == ["Gustav Adolf Hagemanns fødselsdag"]
    assert [m.title for m in span[datetime.date(2026, 9, 27)]] == ["Mathilde Hagemanns fødselsdag"]
    assert datetime.date(2027, 5, 16) in span and datetime.date(2027, 9, 27) in span
    assert len(span) == 4


def test_a_span_across_new_year_finds_nothing_it_should_not() -> None:
    assert maerkedage.in_span(datetime.date(2026, 12, 28), datetime.date(2027, 1, 10)) == {}


@pytest.mark.django_db
def test_the_calendar_marks_the_day_and_lists_it(client: Client, beboer: Resident) -> None:
    client.force_login(beboer)

    response = client.get(f"{EVENTS}kalender?maaned=2026-05&dag=2026-05-16")

    cell = next(
        c for week in response.context["weeks"] for c in week if c["date"] == datetime.date(2026, 5, 16)
    )
    assert [m.title for m in cell["maerkedage"]] == ["Gustav Adolf Hagemanns fødselsdag"]
    body = response.content.decode()
    assert "is-maerkedag" in body
    assert "Kollegiets stifter · 16. maj 1842" in body  # the day panel
    assert "Der sker ikke noget den dag." not in body


@pytest.mark.django_db
def test_the_dashboard_shows_the_kollegium_calendar_not_google(
    client: Client, beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agenda_mod, "current_date", lambda: datetime.date(2026, 9, 10))
    Event.objects.create(
        organiser=beboer,
        title="Fællesspisning",
        visibility=Visibility.AABENT,
        starts_at=(timezone.now() + datetime.timedelta(days=2)).replace(second=0, microsecond=0),
    )
    client.force_login(beboer)

    body = client.get("/intern/").content.decode()

    assert "calendar.google.com" not in body
    assert "Fællesspisning" in body
    assert "Mathilde Hagemanns fødselsdag" in body  # 27 September is within 30 days of the 10th
    assert "Gustav Adolf Hagemanns fødselsdag" not in body


@pytest.mark.django_db
def test_the_agenda_announces_only_maerkedage_within_30_days(
    beboer: Resident, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agenda_mod, "current_date", lambda: datetime.date(2026, 5, 1))
    items = agenda_mod.agenda(beboer)
    assert [i.title for i in items if i.maerkedag] == ["Gustav Adolf Hagemanns fødselsdag"]


@pytest.mark.django_db
def test_the_invite_picker_offers_to_invite_every_resident(client: Client, beboer: Resident) -> None:
    """Rendered hidden; events.ts reveals it, so without JS there is no button that does nothing."""
    client.force_login(beboer)

    body = client.get(f"{EVENTS}opret").content.decode()

    assert "data-invite-all hidden" in body
    assert "Vælg alle beboere" in body
