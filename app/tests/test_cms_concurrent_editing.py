"""Two people in the same Hjemmeside editor: a save over somebody's newer save is stopped once (pages,
news, events, the menu), and an editor shows who else has the same thing open (cms.presence)."""

import datetime
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpRequest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from cms import presence, services
from cms.models import Event, MenuItem, NewsItem, OpenEditor, Page
from core import live
from residents.models import Resident


def client_for(resident: Resident | None) -> Client:
    client = Client()
    if resident is not None:
        client.force_login(resident)
    return client


@pytest.fixture
def editor(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="pr@gahk.dk", first_name="Rita", last_name="Redaktør", roles=("pr",))


@pytest.fixture
def admin(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="admin@gahk.dk", first_name="Ada", last_name="Admin", roles=("administrator",))


@pytest.fixture
def resident(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk")


@pytest.fixture(autouse=True)
def no_publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    from cms import tasks

    monkeypatch.setattr(tasks.publish_gahk_web, "delay", lambda **kwargs: None)


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Every live (topic, data) published, recorded instead of sent (as in test_live)."""
    sent: list[tuple[str, dict[str, object]]] = []

    def record(batch: list[tuple[str, dict[str, object]]]) -> None:
        sent.extend((topic, {k: v for k, v in data.items() if k != "topic"}) for topic, data in batch)

    monkeypatch.setattr(live, "_send", record)
    monkeypatch.setattr(live, "transaction", SimpleNamespace(on_commit=lambda fn: fn()))
    return sent


@pytest.fixture
def page(editor: Resident) -> Page:
    return services.save_page(None, {"header": "Vision", "slug": "vision", "body": "<p>Først</p>"}, editor)


def _page_form(page: Page, body: str, opened: str) -> dict[str, str]:
    return {"header": page.header, "path_segment": page.slug or "", "body": body, "opened_version": opened}


# ---------------------------------------------------------------------------- saving over a save
@pytest.mark.django_db
def test_a_save_over_a_newer_edit_is_stopped_once(editor: Resident, admin: Resident, page: Page) -> None:
    opened = services.page_version(page)
    services.save_page(page, {"body": "<p>Andens rettelse</p>"}, admin)
    data = _page_form(page, "<p>Min</p>", opened)

    response = client_for(editor).post(reverse("cms:page_edit", args=[page.pk]), data)

    assert response.status_code == 200
    assert "Ada Admin har gemt siden kl." in str(response.context["form"].non_field_errors())
    page.refresh_from_db()
    assert page.body == "<p>Andens rettelse</p>"
    # The form comes back with the new version, so "Gem" again overwrites knowingly.
    data["opened_version"] = response.context["opened_version"]
    client_for(editor).post(reverse("cms:page_edit", args=[page.pk]), data)
    page.refresh_from_db()
    assert page.body == "<p>Min</p>"


@pytest.mark.django_db
def test_a_form_shown_again_for_a_bad_address_keeps_the_version_it_was_opened_with(
    editor: Resident, admin: Resident, page: Page
) -> None:
    opened = services.page_version(page)
    services.save_page(page, {"body": "<p>Andens rettelse</p>"}, admin)
    data = {**_page_form(page, "<p>Min</p>", opened), "path_segment": "api"}

    response = client_for(editor).post(reverse("cms:page_edit", args=[page.pk]), data)

    assert "reserveret" in response.content.decode()
    # Not the version stored now: fixing the address and saving must still be stopped.
    assert response.context["opened_version"] == opened


@pytest.mark.django_db
def test_saving_what_somebody_else_already_saved_is_no_conflict(
    editor: Resident, admin: Resident, page: Page
) -> None:
    opened = services.page_version(page)
    services.save_page(page, {"body": "<p>Samme</p>"}, admin)

    response = client_for(editor).post(
        reverse("cms:page_edit", args=[page.pk]), _page_form(page, "<p>Samme</p>", opened), follow=True
    )

    assert "Der var ingen ændringer at gemme." in response.content.decode()


@pytest.mark.django_db
def test_the_service_refuses_a_stale_version_and_names_the_last_save(
    editor: Resident, admin: Resident, page: Page
) -> None:
    opened = services.page_version(page)
    services.save_page(page, {"body": "<p>Andens rettelse</p>"}, admin)

    with pytest.raises(services.EditConflict) as caught:
        services.save_page(page, {"body": "<p>Min</p>"}, editor, opened=opened)

    assert caught.value.change is not None and caught.value.change.by == admin
    page.refresh_from_db()
    assert caught.value.version == services.page_version(page)
    assert page.body == "<p>Andens rettelse</p>"


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["news", "event"])
def test_news_and_events_are_stopped_once_too(editor: Resident, admin: Resident, kind: str) -> None:
    if kind == "news":
        item: NewsItem | Event = services.save_news(None, {"title": "Nyt"}, editor)
        opened = services.news_version(item)
        services.save_news(item, {"title": "Andens titel"}, admin)
        data = {
            "title": "Min titel",
            "published_at": timezone.localtime(item.published_at).strftime("%Y-%m-%dT%H:%M"),
        }
    else:
        item = services.save_event(None, {"title": "Fest", "starts_on": "2030-01-01"}, editor)
        opened = services.event_version(item)
        services.save_event(item, {"title": "Andens titel"}, admin)
        data = {"title": "Min titel", "starts_on": "2030-01-01"}
    url = reverse(f"cms:{kind}_edit", args=[item.pk])

    response = client_for(editor).post(url, {**data, "opened_version": opened})

    assert response.status_code == 200
    errors = str(response.context["form"].non_field_errors())
    assert "Ada Admin har gemt" in errors and "Ændringsloggen" in errors
    item.refresh_from_db()
    assert item.title == "Andens titel"
    client_for(editor).post(url, {**data, "opened_version": response.context["opened_version"]})
    item.refresh_from_db()
    assert item.title == "Min titel"


@pytest.mark.django_db
def test_the_menu_is_stopped_once_and_keeps_the_editors_version(editor: Resident, admin: Resident) -> None:
    services.save_menu([{"label": "Vision", "url": "/vision"}], editor)
    opened = services.menu_version()
    services.save_menu([{"label": "Visionen", "url": "/vision"}], admin)
    data = {"rows": "1", "label_0": "Om os", "url_0": "/vision", "action": "save", "opened_version": opened}

    response = client_for(editor).post(reverse("cms:menu"), data)

    assert response.status_code == 200
    assert "Ada Admin har gemt menuen" in response.content.decode()
    assert response.context["rows"] == [{"label": "Om os", "url": "/vision"}]
    assert list(MenuItem.objects.values_list("label", flat=True)) == ["Visionen"]
    client_for(editor).post(
        reverse("cms:menu"), {**data, "opened_version": response.context["opened_version"]}
    )
    assert list(MenuItem.objects.values_list("label", flat=True)) == ["Om os"]


# ------------------------------------------------------------------------------ who else is here
@pytest.mark.django_db
def test_opening_an_editor_shows_who_else_has_it_open(editor: Resident, admin: Resident, page: Page) -> None:
    url = reverse("cms:page_edit", args=[page.pk])
    first = client_for(admin).get(url).content.decode()
    assert "har også siden åben" not in first

    second = client_for(editor).get(url).content.decode()

    assert "<b>Ada Admin</b> (åbnet kl." in second and "har også siden åben" in second
    # And the first one hears of the second at once, through the box read again on its signal.
    box = client_for(admin).get(reverse("cms:editing", args=[f"page-{page.pk}"])).content.decode()
    assert "<b>Rita Redaktør</b>" in box and "Ada Admin" not in box


@pytest.mark.django_db
def test_somebody_whose_browser_went_quiet_is_not_shown(
    editor: Resident, admin: Resident, page: Page
) -> None:
    presence.arrive(f"page-{page.pk}", page.pk, admin)
    OpenEditor.objects.update(seen_at=timezone.now() - presence.FRESH - datetime.timedelta(seconds=1))

    response = client_for(editor).get(reverse("cms:page_edit", args=[page.pk]))

    assert "har også siden åben" not in response.content.decode()


@pytest.mark.django_db
def test_arriving_and_leaving_are_signalled_but_staying_is_not(
    admin: Resident, page: Page, signals: list[tuple[str, dict[str, object]]]
) -> None:
    heartbeat = reverse("cms:editing_heartbeat", args=[f"page-{page.pk}"])
    client = client_for(admin)

    assert client.post(heartbeat).status_code == 204
    client.post(heartbeat)
    client.post(heartbeat, {"leave": "1"})

    assert signals == [(f"cms.page-{page.pk}", {"kind": "changed", "pk": page.pk})] * 2
    assert not OpenEditor.objects.exists()


@pytest.mark.django_db
def test_the_box_says_when_somebody_saved_after_it_was_opened(
    editor: Resident, admin: Resident, page: Page
) -> None:
    opened = services.page_version(page)
    box_url = reverse("cms:editing", args=[f"page-{page.pk}"])
    assert "har gemt" not in client_for(editor).get(box_url, {"opened": opened}).content.decode()

    services.save_page(page, {"body": "<p>Andens rettelse</p>"}, admin)
    box = client_for(editor).get(box_url, {"opened": opened}).content.decode()

    assert "<b>Ada Admin</b> har gemt siden kl." in box
    assert reverse("cms:page_history", args=[page.pk]) in box


@pytest.mark.django_db
def test_saving_and_deleting_are_signalled(
    editor: Resident, admin: Resident, page: Page, signals: list[tuple[str, dict[str, object]]]
) -> None:
    services.save_page(page, {"body": "<p>Ny</p>"}, editor)
    item = services.save_news(None, {"title": "Nyt"}, editor)
    news_pk, page_pk = item.pk, page.pk
    signals.clear()
    services.save_menu([{"label": "Vision", "url": "/vision"}], editor)
    services.delete_news(item, editor)
    services.delete_page(page, admin)

    assert signals == [
        ("cms.menu", {"kind": "changed", "pk": None}),
        (f"cms.news-{news_pk}", {"kind": "deleted", "pk": news_pk}),
        (f"cms.page-{page_pk}", {"kind": "deleted", "pk": page_pk}),
    ]


@pytest.mark.django_db
def test_the_box_is_gone_with_its_page_and_refuses_unknown_targets(editor: Resident, page: Page) -> None:
    client = client_for(editor)
    assert client.get(reverse("cms:editing", args=["menu"])).status_code == 200
    assert client.get(reverse("cms:editing", args=["page-999999"])).status_code == 404
    assert client.get(reverse("cms:editing", args=["image-1"])).status_code == 404
    assert client.post(reverse("cms:editing_heartbeat", args=["page-x"])).status_code == 404


@pytest.mark.django_db
def test_only_editors_report_in_or_listen(resident: Resident, editor: Resident, page: Page) -> None:
    def as_request(who: Resident) -> HttpRequest:
        request = HttpRequest()
        request.user = who
        request.session = SessionStore()
        return request

    target = f"page-{page.pk}"
    assert client_for(resident).post(reverse("cms:editing_heartbeat", args=[target])).status_code == 403
    assert client_for(resident).get(reverse("cms:editing", args=[target])).status_code == 403
    assert not OpenEditor.objects.exists()
    assert live.may_subscribe(as_request(editor), f"cms.{target}")
    assert live.may_subscribe(as_request(editor), "cms.menu")
    assert not live.may_subscribe(as_request(editor), "cms.image-1")
    assert not live.may_subscribe(as_request(resident), f"cms.{target}")


def test_versions_are_fingerprints_of_the_content() -> None:
    state: dict[str, Any] = {"title": "Nyt", "when": datetime.date(2030, 1, 1)}
    assert services.edit_version(state) == services.edit_version(dict(reversed(state.items())))
    assert services.edit_version(state) != services.edit_version({**state, "title": "Nyt!"})
