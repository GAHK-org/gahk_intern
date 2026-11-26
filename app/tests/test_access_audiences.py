"""Audiences (access.audiences): who sees a channel, an opslag and an open event. Marked
real_stages, so former residents are former residents."""

import datetime
from collections.abc import Callable
from itertools import count

import pytest
from django.test import Client
from django.utils import timezone

from access import audiences
from access.audiences import FORMER, HOUSE, HOUSE_AND_KITCHEN, LIVING
from core.clock import current_date
from core.models import Room
from den_hurtige import channels
from den_hurtige.channels import Channel
from events.models import Event
from notifications.models import Notification
from opslagstavle import access as board_access
from opslagstavle import services as board_services
from opslagstavle.models import Notice
from residents.models import Account, Residency

pytestmark = [pytest.mark.django_db, pytest.mark.real_stages]

_numbers = count(700)


def _on_list(resident: Account, *offsets: int) -> Account:
    today = current_date()
    for offset in offsets:
        n = next(_numbers)
        room = Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")
        index = today.year * 12 + today.month - 1 + offset
        Residency.objects.create(resident=resident.house, room=room, year=index // 12, month=index % 12 + 1)
    return resident


@pytest.fixture
def people(make_resident: Callable[..., Account]) -> dict[str, Account]:
    return {
        "resident": _on_list(make_resident(email="bor@gahk.dk", first_name="Bo"), 0),
        "former": _on_list(make_resident(email="gl@gahk.dk", first_name="Gitte"), -6),
        "kitchen": make_resident(email="kok@kok.dk", first_name="Kok", is_kitchen=True),
    }


def _client(user: Account) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_the_presets_are_role_sets() -> None:
    assert LIVING < HOUSE and FORMER < HOUSE and not LIVING & FORMER
    assert audiences.preset_of(sorted(HOUSE_AND_KITCHEN)).key == "house_and_kitchen"
    assert audiences.label(["inspektion"]) == "Inspektionen"


# ---- opslag -------------------------------------------------------------------------------------


def test_a_post_for_those_living_here_is_hidden_from_former_residents(people: dict[str, Account]) -> None:
    notice = Notice.objects.create(author=people["resident"], body="Husmøde", audience=sorted(LIVING))
    assert _client(people["resident"]).get(f"/opslagstavle/{notice.pk}").status_code == 200
    assert _client(people["former"]).get(f"/opslagstavle/{notice.pk}").status_code == 404
    assert "Husmøde" not in _client(people["former"]).get("/opslagstavle/").content.decode()


def test_an_administrator_reads_every_post_whatever_its_audience(
    people: dict[str, Account], make_resident: Callable[..., Account]
) -> None:
    """An administrator by a grant alone has no stage, so no audience names them: they are in all."""
    notice = Notice.objects.create(author=people["kitchen"], body="Kun køkkenet", audience=["kitchen"])
    admin = _client(make_resident(email="admin@gahk.dk", roles=("administrator",)))
    assert admin.get(f"/opslagstavle/{notice.pk}").status_code == 200
    assert "Kun køkkenet" in admin.get("/opslagstavle/").content.decode()


def test_the_author_always_reads_their_own(people: dict[str, Account]) -> None:
    notice = Notice.objects.create(author=people["former"], body="Hilsen", audience=sorted(LIVING))
    assert _client(people["former"]).get(f"/opslagstavle/{notice.pk}").status_code == 200


def test_a_post_is_for_an_audience_its_author_is_in(people: dict[str, Account]) -> None:
    def keys(user: Account) -> list[str]:
        request = _client(user).get("/opslagstavle/opret").wsgi_request
        return [preset.key for preset in board_access.audience_presets(request)]

    assert keys(people["kitchen"]) == ["house_and_kitchen"]
    assert keys(people["resident"]) == ["house", "living", "house_and_kitchen"]
    assert keys(people["former"]) == ["house", "former", "house_and_kitchen"]


def test_posting_to_the_kitchen(people: dict[str, Account]) -> None:
    _client(people["resident"]).post(
        "/opslagstavle/opret",
        {"category": "nyt", "body": "Tak for maden!", "audience_preset": "house_and_kitchen"},
    )
    notice = Notice.objects.get()
    assert set(notice.audience) == HOUSE_AND_KITCHEN
    assert "Tak for maden" in _client(people["kitchen"]).get("/opslagstavle/").content.decode()


def test_a_preset_one_is_not_in_cannot_be_posted(people: dict[str, Account]) -> None:
    response = _client(people["resident"]).post(
        "/opslagstavle/opret", {"category": "nyt", "body": "Hej", "audience_preset": "former"}
    )
    assert response.status_code == 200 and not Notice.objects.exists()


def test_a_new_post_is_announced_to_its_audience(
    people: dict[str, Account], make_resident: Callable[..., Account]
) -> None:
    author = _on_list(make_resident(email="forf@gahk.dk"), 0)
    notice = Notice.objects.create(author=author, body="Kun nuværende", audience=sorted(LIVING))
    board_services.notify_new_notice(notice)
    told = set(Notification.objects.values_list("recipient_id", flat=True))
    assert (
        people["resident"].pk in told and people["former"].pk not in told and people["kitchen"].pk not in told
    )


# ---- events -------------------------------------------------------------------------------------


def _event(organiser: Account, audience: frozenset[str]) -> Event:
    start = timezone.now() + datetime.timedelta(days=3)
    return Event.objects.create(organiser=organiser, title="Fest", starts_at=start, audience=sorted(audience))


def test_an_open_event_is_open_to_its_audience(people: dict[str, Account]) -> None:
    event = _event(people["resident"], LIVING)
    assert _client(people["resident"]).get(f"/begivenheder/{event.pk}").status_code == 200
    assert _client(people["former"]).get(f"/begivenheder/{event.pk}").status_code == 404
    reunion = _event(people["resident"], FORMER)
    assert _client(people["former"]).get(f"/begivenheder/{reunion.pk}").status_code == 200


def test_the_event_form_offers_audiences_the_organiser_is_in(people: dict[str, Account]) -> None:
    html = _client(people["resident"]).get("/begivenheder/opret").content.decode()
    assert "Kun dem, der bor her nu" in html and "Kun tidligere beboere" not in html
    assert "køkkenet" not in html.split("Åben for")[1].split("</select>")[0]


# ---- Den Hurtige ----------------------------------------------------------------------------------


def test_a_channel_for_former_residents(people: dict[str, Account], monkeypatch: pytest.MonkeyPatch) -> None:
    alumni = Channel("alumner", "Alumner", "users", "For dem, der er flyttet.", audience=FORMER)
    monkeypatch.setattr(channels, "CHANNELS", (*channels.CHANNELS, alumni))
    monkeypatch.setattr(channels, "BY_SLUG", {**channels.BY_SLUG, "alumner": alumni})
    assert _client(people["former"]).get("/den-hurtige/alumner/").status_code == 200
    assert _client(people["resident"]).get("/den-hurtige/alumner/").status_code == 404
    assert _client(people["kitchen"]).get("/den-hurtige/alumner/").status_code == 404


def test_the_kitchen_reads_the_channel_whose_audience_has_it(people: dict[str, Account]) -> None:
    kitchen = _client(people["kitchen"])
    assert kitchen.get("/den-hurtige/koekken/").status_code == 200
    assert kitchen.get("/den-hurtige/generelt/").status_code == 404
    assert kitchen.get("/den-hurtige/").url == "/den-hurtige/koekken/"
