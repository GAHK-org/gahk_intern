"""The Hjemmeside editor's screens (/hjemmeside/): who gets in, and that every write goes through
cms.services (a ContentChange row per write, versions for pages, a publish queued)."""

from collections.abc import Callable
from io import BytesIO
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from cms import services
from cms.middleware import EDITOR_HINT_COOKIE
from cms.models import (
    CmsImage,
    ContentChange,
    Event,
    GahkWebSync,
    LegacyImageFailure,
    MenuItem,
    NewsItem,
    Page,
    PageRedirect,
    Publication,
    SiteSettings,
)
from cms.views import with_base
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


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every publish the services queue, instead of running it."""
    calls: list[dict[str, Any]] = []
    from cms import tasks

    monkeypatch.setattr(tasks.publish_gahk_web, "delay", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def front(db: None) -> Page:
    return Page.put_front_page(slug="velkommen", header="Velkommen", body="<p>Hej</p>")


def _png(name: str = "billede.png") -> SimpleUploadedFile:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), "green").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def _image(editor: Resident, caption: str = "Gangen") -> CmsImage:
    return services.upload_image(_png(), caption, editor)


# ------------------------------------------------------------------------------------ who gets in
GET_URLS = [
    ("cms:overview", []),
    ("cms:status", []),
    ("cms:page_list", []),
    ("cms:page_create", []),
    ("cms:page_edit", ["page"]),
    ("cms:page_history", ["page"]),
    ("cms:news_list", []),
    ("cms:news_create", []),
    ("cms:news_edit", ["news"]),
    ("cms:event_list", []),
    ("cms:event_create", []),
    ("cms:event_edit", ["event"]),
    ("cms:image_list", []),
    ("cms:image_detail", ["image"]),
    ("cms:image_library_json", []),
    ("cms:menu", []),
    ("cms:redirect_list", []),
    ("cms:change_log", []),
]
POST_URLS = [
    ("cms:publish_now", []),
    ("cms:preview", []),
    ("cms:page_restore", ["page", "version"]),
    ("cms:news_delete", ["news"]),
    ("cms:event_delete", ["event"]),
    ("cms:image_delete", ["image"]),
    ("cms:image_upload_json", []),
    ("cms:site_settings", []),
    ("cms:redirect_delete", ["redirect"]),
]


@pytest.fixture
def objects(editor: Resident, front: Page) -> dict[str, int]:
    page = services.save_page(None, {"header": "Vision", "slug": "vision"}, editor)
    services.save_page(page, {"slug": "vores-vision"}, editor)
    return {
        "page": page.pk,
        "version": page.versions.first().pk,  # type: ignore[union-attr]
        "news": services.save_news(None, {"title": "Nyt"}, editor).pk,
        "event": services.save_event(None, {"title": "Fest", "starts_on": "2030-01-01"}, editor).pk,
        "image": _image(editor).pk,
        "redirect": PageRedirect.objects.get(old_path="vision").pk,
    }


def _url(name: str, args: list[str], objects: dict[str, int]) -> str:
    return reverse(name, args=[objects[arg] for arg in args])


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "args"), GET_URLS + POST_URLS)
def test_anonymous_visitors_are_sent_to_the_login(
    name: str, args: list[str], objects: dict[str, int]
) -> None:
    url = _url(name, args, objects)
    response = client_for(None).post(url) if (name, args) in POST_URLS else client_for(None).get(url)
    assert response.status_code == 302 and "/login" in response["Location"]


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "args"), GET_URLS + POST_URLS)
def test_a_resident_without_an_editor_role_is_refused(
    name: str, args: list[str], objects: dict[str, int], resident: Resident
) -> None:
    url = _url(name, args, objects)
    client = client_for(resident)
    response = client.post(url) if (name, args) in POST_URLS else client.get(url)
    assert response.status_code == 403
    assert EDITOR_HINT_COOKIE not in response.cookies


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "args"), GET_URLS)
def test_the_pr_group_opens_every_screen(
    name: str, args: list[str], objects: dict[str, int], editor: Resident
) -> None:
    response = client_for(editor).get(_url(name, args, objects))
    assert response.status_code == 200


@pytest.mark.django_db
def test_the_sidebar_links_editors_to_hjemmeside(editor: Resident, resident: Resident) -> None:
    def nav_urls(who: Resident) -> list[str]:
        nav = client_for(who).get("/").context["nav_intern"]
        return [url for _section, items in nav for url, _label, _icon in items]

    assert reverse("cms:overview") in nav_urls(editor)
    assert reverse("cms:overview") not in nav_urls(resident)
    assert not any("django-admin/cms" in url for url in nav_urls(editor))


@pytest.mark.django_db
def test_only_an_administrator_may_delete_a_page(
    editor: Resident, admin: Resident, objects: dict[str, int], published: list[dict[str, Any]]
) -> None:
    url = reverse("cms:page_delete", args=[objects["page"]])
    assert client_for(editor).get(url).status_code == 403
    assert client_for(editor).post(url).status_code == 403
    assert Page.objects.filter(pk=objects["page"]).exists()

    response = client_for(admin).get(url)
    assert response.status_code == 200
    assert "/vision" in response.content.decode()  # the redirect that goes with it is named

    response = client_for(admin).post(url)

    assert response.status_code == 302
    assert not Page.objects.filter(pk=objects["page"]).exists()
    assert not PageRedirect.objects.filter(old_path="vision").exists()
    assert ContentChange.objects.filter(
        action="delete", kind="page", object_id=objects["page"], by=admin
    ).exists()


@pytest.mark.django_db
def test_the_front_page_cannot_be_deleted(admin: Resident, front: Page) -> None:
    response = client_for(admin).post(reverse("cms:page_delete", args=[front.pk]))
    assert response.status_code == 302
    assert Page.objects.filter(pk=front.pk).exists()


# ------------------------------------------------------------------------------------------ pages
@pytest.mark.django_db
def test_creating_a_page_goes_through_the_services(
    editor: Resident,
    front: Page,
    published: list[dict[str, Any]],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    Page.objects.create(slug="faciliteter", header="Faciliteter")
    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(editor).post(
            reverse("cms:page_create"),
            {
                "header": "Køkkenet",
                "path_parent": "faciliteter",
                "path_segment": " Kokken ",
                "description": "Om køkkenet",
                "background_image": "",
                "body": "<p>Stort</p><script>x()</script>",
            },
        )

    page = Page.objects.get(slug="faciliteter/kokken")
    assert response.status_code == 302 and response["Location"] == reverse("cms:page_edit", args=[page.pk])
    assert page.body == "<p>Stort</p>" and page.updated_by == editor
    assert page.versions.count() == 1
    assert ContentChange.objects.filter(action="create", kind="page", object_id=page.pk, by=editor).exists()
    assert published == [{"reason": "save", "triggered_by_id": editor.pk}]


@pytest.mark.django_db
def test_renaming_a_page_leaves_a_redirect_and_says_so(editor: Resident, objects: dict[str, int]) -> None:
    page = Page.objects.get(pk=objects["page"])
    response = client_for(editor).post(
        reverse("cms:page_edit", args=[page.pk]),
        {
            "header": page.header,
            "path_parent": "",
            "path_segment": "visionen",
            "body": page.body,
            "loaded_at": page.updated_at.isoformat(),
        },
        follow=True,
    )

    assert PageRedirect.objects.get(old_path="vores-vision").page_id == page.pk
    assert "/vores-vision sender nu besøgende videre til /visionen" in response.content.decode()
    assert ContentChange.objects.filter(action="update", kind="page", object_id=page.pk).count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("parent", "segment", "message"),
    [
        ("", "api", "reserveret"),
        ("optagelse", "ansoeg", "bruges af gahk.dk selv"),
        ("", "vores-vision", "bruges allerede"),
        ("faciliteter", "", "Skriv den sidste del"),
        ("", "a/b", "må ikke indeholde “/”"),
        ("", "Fælles", "må kun indeholde"),
    ],
)
def test_bad_addresses_are_refused_on_the_address_field(
    editor: Resident, objects: dict[str, int], parent: str, segment: str, message: str
) -> None:
    Page.objects.create(slug="optagelse", header="Optagelse")
    Page.objects.create(slug="faciliteter", header="Faciliteter")
    before = ContentChange.objects.count()
    response = client_for(editor).post(
        reverse("cms:page_create"),
        {"header": "Ny", "path_parent": parent, "path_segment": segment, "body": ""},
    )
    assert response.status_code == 200
    assert message in str(response.context["form"].errors.get("path_segment"))
    assert ContentChange.objects.count() == before


@pytest.mark.django_db
def test_a_save_over_a_newer_edit_is_stopped_once(
    editor: Resident, admin: Resident, objects: dict[str, int]
) -> None:
    page = Page.objects.get(pk=objects["page"])
    opened_at = page.updated_at.isoformat()
    services.save_page(page, {"body": "<p>Andens rettelse</p>"}, admin)
    data = {
        "header": "Min titel",
        "path_segment": "vores-vision",
        "body": "<p>Min</p>",
        "loaded_at": opened_at,
    }

    response = client_for(editor).post(reverse("cms:page_edit", args=[page.pk]), data)

    assert response.status_code == 200
    assert "Ada Admin har gemt siden" in str(response.context["form"].non_field_errors())
    page.refresh_from_db()
    assert page.body == "<p>Andens rettelse</p>"
    # The form comes back with the new stamp, so "Gem" again overwrites knowingly.
    data["loaded_at"] = response.context["loaded_at"]
    client_for(editor).post(reverse("cms:page_edit", args=[page.pk]), data)
    page.refresh_from_db()
    assert page.body == "<p>Min</p>"


@pytest.mark.django_db
def test_the_front_page_has_no_address_fields(editor: Resident, front: Page) -> None:
    response = client_for(editor).post(
        reverse("cms:page_edit", args=[front.pk]), {"header": "Velkommen!", "body": "<p>Hej</p>"}
    )
    assert response.status_code == 302
    front.refresh_from_db()
    assert (front.header, front.slug) == ("Velkommen!", "velkommen")


@pytest.mark.django_db
def test_the_page_list_groups_by_section_and_shows_menu_hints(editor: Resident, front: Page) -> None:
    MenuItem.objects.all().delete()
    MenuItem.objects.create(label="Faciliteter", url="/faciliteter", order=1)
    Page.objects.create(slug="faciliteter", header="Faciliteter")
    Page.objects.create(slug="faciliteter/kokken", header="Køkken")
    Page.objects.create(slug="legater", header="Legater")
    Page.objects.create(slug=None, header="Gammel optagelsestekst")

    response = client_for(editor).get(reverse("cms:page_list"))

    titles = [section.title for section in response.context["sections"]]
    assert titles == ["Forsiden", "/faciliteter", "/legater", "Uden adresse (vises ikke på gahk.dk)"]
    states = {row.page.header: row.menu_state for s in response.context["sections"] for row in s.rows}
    assert states == {
        "Velkommen": "front",
        "Faciliteter": "menu",
        "Køkken": "section",
        "Legater": "none",
        "Gammel optagelsestekst": "hidden",
    }
    assert "https://gahk.dk/faciliteter/kokken" in response.content.decode()


@pytest.mark.django_db
def test_history_shows_a_diff_and_restores_through_the_services(
    editor: Resident, objects: dict[str, int]
) -> None:
    page = Page.objects.get(pk=objects["page"])
    services.save_page(page, {"body": "<p>Første afsnit</p><p>Andet</p>"}, editor)
    services.save_page(page, {"body": "<p>Første afsnit</p><p>Ændret</p>"}, editor)
    response = client_for(editor).get(reverse("cms:page_history", args=[page.pk]))
    newest = response.context["entries"][0]
    assert ("del", "<p>Andet</p>") in newest.body_diff and ("add", "<p>Ændret</p>") in newest.body_diff

    old = page.versions.get(body="<p>Første afsnit</p><p>Andet</p>")
    response = client_for(editor).post(reverse("cms:page_restore", args=[page.pk, old.pk]))

    assert response.status_code == 302
    page.refresh_from_db()
    assert page.body == "<p>Første afsnit</p><p>Andet</p>"
    assert ContentChange.objects.filter(action="restore", object_id=page.pk, by=editor).exists()


@pytest.mark.django_db
def test_a_version_of_another_page_cannot_be_restored(editor: Resident, objects: dict[str, int]) -> None:
    other = services.save_page(None, {"header": "Anden"}, editor)
    response = client_for(editor).post(reverse("cms:page_restore", args=[other.pk, objects["version"]]))
    assert response.status_code == 404


# ---------------------------------------------------------------------------------------- preview
@pytest.mark.django_db
def test_preview_shows_gahk_dk_s_rendering_with_a_base_and_no_scripts(
    editor: Resident,
    objects: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,  # noqa: ANN401
) -> None:
    settings.GAHK_WEB_KEY = "k"
    settings.GAHK_WEB_URL = "https://gahk.dk"
    sent: list[dict[str, Any]] = []

    def fake_preview(self: object, request: dict[str, Any]) -> str:
        sent.append(request)
        return "<!doctype html><html><head><title>x</title></head><body>gahk.dk</body></html>"

    monkeypatch.setattr("cms.gahk_web.GahkWebClient.preview", fake_preview)
    before = ContentChange.objects.count()

    response = client_for(editor).post(
        reverse("cms:preview"),
        {
            "preview_kind": "page",
            "preview_id": objects["page"],
            "header": "Ny titel",
            "path_segment": "vores-vision",
            "body": "<p>Kladde</p>",
        },
    )

    html = response.content.decode()
    assert response.status_code == 200 and response["X-Hjemmeside-Preview"] == "gahk.dk"
    assert '<head><base href="https://gahk.dk/">' in html
    assert "allow-scripts" not in response["Content-Security-Policy"]
    assert response["Content-Security-Policy"].startswith("sandbox")
    assert sent[0]["kind"] == "page" and sent[0]["item"]["title"] == "Ny titel"
    assert sent[0]["item"]["body"] == "<p>Kladde</p>"
    assert ContentChange.objects.count() == before  # nothing saved


@pytest.mark.django_db
def test_preview_falls_back_to_the_text_when_gahk_dk_is_unreachable(
    editor: Resident,
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,  # noqa: ANN401
) -> None:
    from cms.gahk_web import GahkWebUnavailable

    settings.GAHK_WEB_KEY = "k"

    def down(self: object, request: dict[str, Any]) -> str:
        raise GahkWebUnavailable("connection refused")

    monkeypatch.setattr("cms.gahk_web.GahkWebClient.preview", down)

    response = client_for(editor).post(
        reverse("cms:preview"),
        {"preview_kind": "news", "title": "Nyhed", "body": "<p>Tekst</p><script>alert(1)</script>"},
    )

    html = response.content.decode()
    assert response["X-Hjemmeside-Preview"] == "lokal"
    assert "kunne ikke nås" in html and "<p>Tekst</p>" in html and "<script>alert" not in html


@pytest.mark.django_db
def test_preview_without_a_key_says_intern_is_not_connected(editor: Resident) -> None:
    response = client_for(editor).post(
        reverse("cms:preview"), {"preview_kind": "event", "title": "Fest", "description": "<p>Kom</p>"}
    )
    assert "ikke forbundet" in response.content.decode()
    assert client_for(editor).post(reverse("cms:preview"), {"preview_kind": "x"}).status_code == 400


def test_with_base_keeps_a_base_that_is_there() -> None:
    html = '<html><head><base href="https://gahk.dk/"></head></html>'
    assert with_base(html, "http://other") == html
    assert with_base("<p>x</p>", "https://gahk.dk") == '<base href="https://gahk.dk/"><p>x</p>'


# ------------------------------------------------------------------------------ news and events
@pytest.mark.django_db
def test_news_are_created_edited_and_deleted_through_the_services(editor: Resident) -> None:
    client = client_for(editor)
    client.post(
        reverse("cms:news_create"),
        {"title": "Julefrokost", "published_at": "2026-11-12T10:30", "body": "<p>Kom</p>"},
    )
    item = NewsItem.objects.get(title="Julefrokost")
    assert item.published_at.isoformat().startswith("2026-11-12T09:30")  # 10:30 in Copenhagen

    client.post(reverse("cms:news_edit", args=[item.pk]), {"title": "Julefrokost!", "body": "<p>Kom</p>"})
    client.post(reverse("cms:news_delete", args=[item.pk]))

    assert not NewsItem.objects.exists()
    actions = list(ContentChange.objects.filter(kind="news").order_by("id").values_list("action", flat=True))
    assert actions == ["create", "update", "delete"]


@pytest.mark.django_db
def test_saving_a_news_item_untouched_keeps_its_time_and_writes_nothing(
    editor: Resident,
    published: list[dict[str, Any]],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    item = services.save_news(None, {"title": "Nyhed", "body": "<p>x</p>"}, editor)  # now, with seconds
    item.published_at = item.published_at.replace(second=42, microsecond=123)
    item.save()
    stored = item.published_at
    client = client_for(editor)
    form = client.get(reverse("cms:news_edit", args=[item.pk])).context["form"]
    shown = form["published_at"].value().strftime("%Y-%m-%dT%H:%M")  # what the field sends back

    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("cms:news_edit", args=[item.pk]),
            {"title": "Nyhed", "published_at": shown, "body": "<p>x</p>"},
        )

    item.refresh_from_db()
    assert item.published_at == stored
    assert ContentChange.objects.filter(kind="news", action="update").count() == 0 and published == []


@pytest.mark.django_db
def test_events_need_a_date_and_are_deleted_through_the_services(editor: Resident) -> None:
    client = client_for(editor)
    response = client.post(reverse("cms:event_create"), {"title": "Fest", "starts_on": ""})
    assert response.status_code == 200 and "starts_on" in response.context["form"].errors

    client.post(reverse("cms:event_create"), {"title": "Fest", "starts_on": "2030-05-01"})
    event = Event.objects.get()
    client.post(reverse("cms:event_delete", args=[event.pk]))

    assert not Event.objects.exists()
    assert ContentChange.objects.filter(kind="event", action="delete").exists()


# ----------------------------------------------------------------------------------------- images
@pytest.mark.django_db
def test_images_are_uploaded_into_the_library(editor: Resident) -> None:
    response = client_for(editor).post(
        reverse("cms:image_list"),
        {"file": [_png("a.png"), _png("b.png")], "caption": "Haven"},
    )
    assert response.status_code == 302
    assert CmsImage.objects.filter(caption="Haven").count() == 2
    assert ContentChange.objects.filter(kind="image", action="upload", by=editor).count() == 2


@pytest.mark.django_db
def test_an_svg_upload_is_refused(editor: Resident) -> None:
    svg = SimpleUploadedFile(
        "x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", content_type="image/svg+xml"
    )
    response = client_for(editor).post(reverse("cms:image_list"), {"file": svg}, follow=True)
    assert not CmsImage.objects.exists()
    assert "x.svg:" in response.content.decode()

    svg.seek(0)
    response = client_for(editor).post(reverse("cms:image_upload_json"), {"file": svg})
    assert response.status_code == 400 and response.json()["error"]


@pytest.mark.django_db
def test_the_editor_s_picker_lists_and_uploads_as_json(editor: Resident) -> None:
    client = client_for(editor)
    response = client.post(reverse("cms:image_upload_json"), {"file": _png(), "caption": "Gården"})
    assert response.status_code == 201
    image = response.json()["image"]
    assert image["caption"] == "Gården" and image["url"].startswith("/media/cms/")

    listing = client.get(reverse("cms:image_library_json")).json()["images"]
    assert [entry["id"] for entry in listing] == [image["id"]]


@pytest.mark.django_db
def test_an_image_in_use_cannot_be_deleted(editor: Resident, objects: dict[str, int]) -> None:
    image = CmsImage.objects.get(pk=objects["image"])
    page = Page.objects.get(pk=objects["page"])
    services.save_page(page, {"body": f'<p><img src="{image.url}" alt=""></p>'}, editor)

    tiles = client_for(editor).get(reverse("cms:image_list")).context["tiles"]
    assert {tile.pk: usage for tile, usage in tiles}[image.pk] == ["Side: Vision"]

    response = client_for(editor).post(reverse("cms:image_delete", args=[image.pk]), follow=True)
    assert CmsImage.objects.filter(pk=image.pk).exists()
    assert "bruges stadig" in response.content.decode()

    services.save_page(page, {"body": "<p>uden</p>"}, editor)
    client_for(editor).post(reverse("cms:image_delete", args=[image.pk]))
    assert not CmsImage.objects.filter(pk=image.pk).exists()
    assert ContentChange.objects.filter(kind="image", action="delete", object_id=image.pk).exists()


@pytest.mark.django_db
def test_an_image_caption_is_edited_through_the_services(editor: Resident, objects: dict[str, int]) -> None:
    client_for(editor).post(reverse("cms:image_detail", args=[objects["image"]]), {"caption": "Gården"})
    assert CmsImage.objects.get(pk=objects["image"]).caption == "Gården"
    assert ContentChange.objects.filter(kind="image", action="update").exists()


# ------------------------------------------------------------------------------- menu and settings
def _menu_post(rows: list[tuple[str, str]], action: str) -> dict[str, str]:
    data = {"rows": str(len(rows)), "action": action}
    for index, (label, url) in enumerate(rows):
        data[f"label_{index}"] = label
        data[f"url_{index}"] = url
    return data


@pytest.mark.django_db
def test_the_menu_is_reordered_extended_and_trimmed(editor: Resident) -> None:
    MenuItem.objects.all().delete()
    rows = [("Hjem", "/"), ("Vision", "/vision"), ("Kontakt", "/kontakt")]
    client = client_for(editor)

    client.post(reverse("cms:menu"), _menu_post(rows, "up-2"))
    assert list(MenuItem.objects.values_list("label", flat=True)) == ["Hjem", "Kontakt", "Vision"]

    rows = [("Hjem", "/"), ("Kontakt", "/kontakt"), ("Vision", "/vision"), ("Legater", "/legater")]
    client.post(reverse("cms:menu"), _menu_post(rows, "save"))
    client.post(reverse("cms:menu"), _menu_post([*rows, ("", "")], "remove-0"))

    assert list(MenuItem.objects.values_list("label", flat=True)) == ["Kontakt", "Vision", "Legater"]
    assert ContentChange.objects.filter(kind="menu").count() == 3


@pytest.mark.django_db
def test_a_bad_menu_link_is_shown_and_nothing_is_saved(editor: Resident) -> None:
    before = list(MenuItem.objects.values_list("label", "url"))
    response = client_for(editor).post(
        reverse("cms:menu"), _menu_post([("Ond", "javascript:alert(1)")], "save")
    )
    assert response.status_code == 200 and response.context["errors"]
    assert list(MenuItem.objects.values_list("label", "url")) == before


@pytest.mark.django_db
def test_the_default_background_is_picked_from_the_library(editor: Resident, objects: dict[str, int]) -> None:
    image = CmsImage.objects.get(pk=objects["image"])
    client_for(editor).post(reverse("cms:site_settings"), {"default_background": image.url})
    assert SiteSettings.load().default_background == image.url
    assert ContentChange.objects.filter(kind="settings").exists()


# -------------------------------------------------------------------------------------- redirects
@pytest.mark.django_db
def test_redirects_are_created_and_deleted_through_the_services(
    editor: Resident, objects: dict[str, int]
) -> None:
    client = client_for(editor)
    client.post(reverse("cms:redirect_list"), {"old_path": "/Folder/", "page": objects["page"]})
    created = PageRedirect.objects.get(old_path="folder")

    client.post(reverse("cms:redirect_delete", args=[created.pk]))

    assert not PageRedirect.objects.filter(pk=created.pk).exists()
    assert list(
        ContentChange.objects.filter(kind="redirect").order_by("id").values_list("action", flat=True)
    ) == ["create", "delete"]


# ------------------------------------------------------------------------------------ overview
@pytest.mark.django_db
def test_publish_now_queues_a_manual_publish(
    editor: Resident,
    settings: Any,  # noqa: ANN401
    published: list[dict[str, Any]],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    settings.GAHK_WEB_KEY = "k"
    client = client_for(editor)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("cms:publish_now"))

    assert published == [{"reason": "manual", "triggered_by_id": editor.pk}]
    status = client.get(reverse("cms:status"))
    assert status.context["publish_state"] == "waiting" and 'hx-trigger="every 3s"' in status.content.decode()

    Publication.objects.create(reason="manual", status="ok", revision="a" * 64)
    status = client.get(reverse("cms:status"))
    assert status.context["publish_state"] == "" and "hx-trigger" not in status.content.decode()


@pytest.mark.django_db
def test_only_an_administrator_may_ask_gahk_dk_to_update(
    editor: Resident,
    admin: Resident,
    settings: Any,  # noqa: ANN401
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    from cms import tasks

    settings.GAHK_WEB_KEY = "k"
    nudges: list[bool] = []
    monkeypatch.setattr(tasks.nudge_gahk_web_update, "delay", lambda: nudges.append(True))

    assert client_for(editor).post(reverse("cms:update_now")).status_code == 403
    assert "Opdatér gahk.dk nu" not in client_for(editor).get(reverse("cms:overview")).content.decode()

    client = client_for(admin)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("cms:update_now"))
    assert nudges == [True]
    assert client.get(reverse("cms:status")).context["update_state"] == "waiting"

    sync = GahkWebSync.load()
    sync.update_checked_at = timezone.now()
    sync.update_result = "Ingen ny version"
    sync.save()
    response = client.get(reverse("cms:status"))
    assert response.context["update_state"] == "" and "Ingen ny version" in response.content.decode()


@pytest.mark.django_db
def test_the_overview_says_when_gahk_dk_is_not_connected(editor: Resident) -> None:
    response = client_for(editor).get(reverse("cms:overview"))
    assert "Ikke forbundet" in response.content.decode()
    assert client_for(editor).post(reverse("cms:publish_now")).status_code == 302
    assert not ContentChange.objects.filter(kind="site").exists()


@pytest.mark.django_db
def test_the_overview_shows_a_failed_publication(editor: Resident, settings: Any) -> None:  # noqa: ANN401
    settings.GAHK_WEB_KEY = "k"
    Publication.objects.create(reason="save", status="failed", error="gahk.dk answered 400: bad snapshot")
    html = client_for(editor).get(reverse("cms:overview")).content.decode()
    assert "Fejl" in html and "bad snapshot" in html


@pytest.mark.django_db
def test_the_overview_shows_the_old_images_still_to_move_and_those_that_failed(editor: Resident) -> None:
    # The seeded default background points at the old gang.jpg: waiting for the next sync.
    html = client_for(editor).get(reverse("cms:overview")).content.decode()
    assert "Billeder fra den gamle hjemmeside" in html and "1 billede flyttes" in html

    LegacyImageFailure.objects.create(legacy_path="upload/images/gang.jpg", reason="missing")
    html = client_for(editor).get(reverse("cms:overview")).content.decode()
    assert "flyttes ind" not in html
    assert "<code>upload/images/gang.jpg</code>" in html and "findes ikke" in html

    SiteSettings.objects.filter(pk=1).update(default_background="")
    html = client_for(editor).get(reverse("cms:overview")).content.decode()
    assert "Billeder fra den gamle hjemmeside" not in html  # nothing refers to it any more


@pytest.mark.django_db
def test_the_overview_is_up_to_date_only_until_the_next_save(
    editor: Resident,
    front: Page,
    settings: Any,  # noqa: ANN401
    published: list[dict[str, Any]],
) -> None:
    settings.GAHK_WEB_KEY = "k"
    revision = "a" * 64
    Publication.objects.create(reason="save", status="ok", revision=revision)
    GahkWebSync.load()
    GahkWebSync.objects.filter(pk=1).update(remote_revision=revision)
    client = client_for(editor)
    assert client.get(reverse("cms:status")).context["in_sync"] is True

    services.upload_image(_png(), "Ny", editor)  # unused in any page: gahk.dk shows the same
    assert client.get(reverse("cms:status")).context["in_sync"] is True

    services.save_page(front, {"body": "<p>Ny tekst</p>"}, editor)  # its publish has not run yet
    status = client.get(reverse("cms:status"))
    assert status.context["in_sync"] is False and "det nyeste indhold" not in status.content.decode()

    # The publish ran and found nothing new to send (skipped): up to date again.
    Publication.objects.create(reason="save", status="skipped", revision=revision)
    assert client.get(reverse("cms:status")).context["in_sync"] is True


# ----------------------------------------------------------------------------------- change log
@pytest.mark.django_db
def test_the_change_log_filters_and_diffs_bodies(
    editor: Resident, admin: Resident, objects: dict[str, int]
) -> None:
    page = Page.objects.get(pk=objects["page"])
    services.save_page(page, {"body": "<p>Et</p><p>To</p>"}, admin)
    client = client_for(editor)

    rows = client.get(reverse("cms:change_log"), {"type": "page", "person": admin.pk}).context["rows"]

    assert len(rows) == 1
    change, link, fields = rows[0]
    assert change.by == admin and link == "cms:page_edit"
    body = next(f for f in fields if f.label == "Tekst")
    assert ("add", "<p>To</p>") in (body.diff or [])

    system = client.get(reverse("cms:change_log"), {"person": "system"}).context["rows"]
    assert system == []

    # Every automatic change counts as the system's, whichever job made it.
    services.record_change(action="update", kind="page", label="A", by_name=services.SYSTEM_NAME)
    services.record_change(
        action="upload",
        kind="image",
        label="B",
        by_name="System (gamle billeder flyttet til billedbiblioteket)",
    )
    services.record_change(action="update", kind="page", label="C", by_name="Slettet Beboer")
    system = client.get(reverse("cms:change_log"), {"person": "system"}).context["rows"]
    assert sorted(change.label for change, _link, _fields in system) == ["A", "B"]


@pytest.mark.django_db
def test_saving_a_page_unchanged_says_so_and_writes_nothing(editor: Resident, front: Page) -> None:
    client_for(editor).post(reverse("cms:page_edit", args=[front.pk]), {"header": "Velkommen!", "body": ""})
    before = ContentChange.objects.count()
    front.refresh_from_db()

    response = client_for(editor).post(
        reverse("cms:page_edit", args=[front.pk]),
        {"header": "Velkommen!", "body": "", "loaded_at": front.updated_at.isoformat()},
        follow=True,
    )

    assert "Der var ingen ændringer at gemme." in response.content.decode()
    assert ContentChange.objects.count() == before
