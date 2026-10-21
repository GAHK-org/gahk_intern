"""The kitchen-staff account (Resident.is_kitchen): Den Hurtige's køkken channel, Reparationer and its
own opslag, nothing else. Also as an administrator previewing it ("Se side som")."""

from collections.abc import Callable

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpRequest
from django.test import Client

from core import live
from core.models import PushSubscription
from den_hurtige import services as den_hurtige_services
from den_hurtige.models import QuickPost
from notifications import categories, services
from notifications.models import Notification, NotificationPreference
from opslagstavle import services as opslagstavle_services
from opslagstavle.models import Notice
from residents.models import Resident, Role

pytestmark = pytest.mark.django_db

HOME = "/intern/den-hurtige/koekken/"


@pytest.fixture
def kitchen(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="hagemannsmad@gmail.com", first_name="Køkkenet", is_kitchen=True)


@pytest.fixture
def beboer(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk")


def _client(user: Resident) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _previewing_admin(make_resident: Callable[..., Resident]) -> Client:
    c = _client(make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR]))
    c.post("/admin/preview/set", {"mode": "kitchen"})
    return c


@pytest.mark.parametrize(
    "path",
    [
        "/intern/alumneliste/",
        "/intern/ak/",
        "/intern/begivenheder/",
        "/intern/stamtree/",
        "/admin/",
        "/django-admin/",
    ],
)
def test_the_rest_of_the_site_is_closed(kitchen: Resident, path: str) -> None:
    assert _client(kitchen).get(path).status_code == 403


def test_its_own_profile_is_closed(kitchen: Resident) -> None:
    assert _client(kitchen).get(f"/intern/beboer/{kitchen.pk}/profil").status_code == 403


@pytest.mark.parametrize(
    "path", [HOME, "/intern/reparationer/", "/intern/opslagstavle/", "/intern/opslagstavle/opret"]
)
def test_its_pages_open(kitchen: Resident, path: str) -> None:
    assert _client(kitchen).get(path).status_code == 200


@pytest.mark.parametrize("path", ["/intern/", "/intern/den-hurtige/"])
def test_the_dashboard_and_default_channel_lead_home(kitchen: Resident, path: str) -> None:
    response = _client(kitchen).get(path)
    assert response.status_code == 302 and response["Location"] == HOME


def test_the_sidebar_lists_only_its_pages(kitchen: Resident) -> None:
    html = _client(kitchen).get("/intern/reparationer/").content.decode()
    assert "Alumneliste" not in html and "Min profil" not in html
    assert HOME in html


def test_den_hurtige_shows_only_the_kitchen_channel(kitchen: Resident, beboer: Resident) -> None:
    QuickPost.objects.create(author=beboer, content="Hemmelig fest", channel="generelt")
    c = _client(kitchen)
    assert c.get("/intern/den-hurtige/tv-rezz/").status_code == 404
    assert c.get("/intern/den-hurtige/generelt/").status_code == 404
    assert "Hemmelig fest" not in c.get(HOME).content.decode()
    assert c.post("/intern/den-hurtige/opret", {"content": "x", "kanal": "generelt"}).status_code == 403


def test_kitchen_and_residents_share_the_kitchen_channel(kitchen: Resident, beboer: Resident) -> None:
    _client(kitchen).post(
        "/intern/den-hurtige/opret", {"content": "Der er boller!", "kanal": "koekken", "duration": "60"}
    )
    assert QuickPost.objects.get().channel == "koekken"
    resident = _client(beboer)
    assert "Der er boller!" in resident.get(HOME).content.decode()
    resident.post("/intern/den-hurtige/opret", {"content": "Tak!", "kanal": "koekken", "duration": "60"})
    assert QuickPost.objects.filter(channel="koekken").count() == 2


def test_opslagstavlen_shows_only_its_own_posts(kitchen: Resident, beboer: Resident) -> None:
    mine = Notice.objects.create(author=kitchen, body="Ny ovn i køkkenet.")
    theirs = Notice.objects.create(author=beboer, body="Privat: kun for beboere.")
    c = _client(kitchen)
    board = c.get("/intern/opslagstavle/").content.decode()
    assert "Ny ovn" in board and "Privat" not in board
    assert c.get(f"/intern/opslagstavle/{mine.pk}").status_code == 200
    assert c.get(f"/intern/opslagstavle/{theirs.pk}").status_code == 404
    assert c.post(f"/intern/opslagstavle/{theirs.pk}/kommentar", {"body": "hej"}).status_code == 404


def test_a_resident_still_sees_the_kitchens_posts(kitchen: Resident, beboer: Resident) -> None:
    Notice.objects.create(author=kitchen, body="Ny ovn i køkkenet.")
    assert "Ny ovn" in _client(beboer).get("/intern/opslagstavle/").content.decode()


def _request(user: Resident) -> HttpRequest:
    request = HttpRequest()
    request.user = user
    request.session = SessionStore()
    return request


def test_sockets_only_for_its_features(kitchen: Resident) -> None:
    request = _request(kitchen)
    assert live.may_subscribe(request, "den-hurtige.koekken")
    assert live.may_subscribe(request, "reparationer")
    assert not live.may_subscribe(request, "den-hurtige.generelt")
    assert not live.may_subscribe(request, "opslagstavle")
    assert not live.may_subscribe(request, "begivenheder")


def test_it_is_not_in_everyones_audience_but_hears_its_channel_and_comments(
    kitchen: Resident, beboer: Resident
) -> None:
    PushSubscription.objects.create(
        user=kitchen, endpoint="https://push.example/k", auth="a" * 22, p256dh="p" * 87
    )
    assert not services.everyone().filter(pk=kitchen.pk).exists()
    assert den_hurtige_services._audience("koekken").filter(pk=kitchen.pk).exists()
    assert not den_hurtige_services._audience("generelt").filter(pk=kitchen.pk).exists()
    notice = Notice.objects.create(author=kitchen, body="Ny ovn i køkkenet.")
    assert opslagstavle_services._author_can_open(notice)


def test_an_admin_can_preview_as_the_kitchen(make_resident: Callable[..., Resident]) -> None:
    c = _previewing_admin(make_resident)
    assert c.get("/intern/alumneliste/").status_code == 403
    assert c.get("/intern/den-hurtige/tv-rezz/").status_code == 404
    assert c.get(HOME).status_code == 200
    assert c.get("/admin/preview").status_code == 200
    c.post("/admin/preview/set", {"mode": "clear"})
    assert c.get("/intern/alumneliste/").status_code == 200


def test_a_non_admin_cannot_preview_as_the_kitchen(beboer: Resident) -> None:
    c = _client(beboer)
    c.post("/admin/preview/set", {"mode": "kitchen"})
    assert c.get("/intern/alumneliste/").status_code == 200


def test_its_settings_list_only_what_it_can_be_sent(kitchen: Resident) -> None:
    html = _client(kitchen).get("/intern/notifikationer/indstillinger").content.decode()
    assert "Køkkenet" in html and "Kommentarer på dine opslag" in html and "Generelle beskeder" in html
    assert "TV-Rezz" not in html and "Nye opslag" not in html and "Nye begivenheder" not in html


def test_saving_its_settings_leaves_hidden_categories_alone(kitchen: Resident) -> None:
    _client(kitchen).post("/intern/notifikationer/indstillinger", {})
    saved = set(NotificationPreference.objects.filter(resident=kitchen).values_list("category", flat=True))
    assert saved == categories.KITCHEN_CATEGORIES  # everything unticked, but only what it was shown


def test_a_broadcast_reaches_it(make_resident: Callable[..., Resident], kitchen: Resident) -> None:
    admin = _client(make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR]))
    admin.post(
        "/admin/notifikationer",
        {
            "category": categories.GENERAL,
            "importance": "2",
            "title": "Vandet lukkes",
            "body": "",
            "url": "/intern/",
        },
    )
    assert Notification.objects.filter(recipient=kitchen, title="Vandet lukkes").exists()
