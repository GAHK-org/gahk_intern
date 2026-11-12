"""What intern sends gahk.dk (cms.snapshot)."""

import hashlib
import json
import re
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from django.test import override_settings
from PIL import Image

from cms.models import (
    CmsImage,
    ContentChange,
    Event,
    MenuItem,
    NewsItem,
    Page,
    PageRedirect,
    PageVersion,
    SiteSettings,
)
from cms.snapshot import (
    INTERN_LINK_RE,
    build_snapshot,
    canonical_json,
    compute_revision,
    preview_request,
    rewrite_intern_links,
)

BILLEDER_RE = re.compile(r"^/billeder/([0-9a-f]{16})/([a-z0-9]+(?:[-_.][a-z0-9]+)*)$")


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


def _library_image(name: str = "Have Foto.jpg") -> CmsImage:
    image = CmsImage(caption="Haven")
    image.file.save(name, ContentFile(_image_bytes()), save=True)
    return image  # no sha256 yet: like a row from before the field existed


@pytest.fixture
def front_page() -> Page:
    return Page.put_front_page(slug="velkommen", header="Velkommen", body="<p>Forside</p>")


# -------------------------------------------------------------------------------------- snapshot
@pytest.mark.django_db
def test_the_snapshot_has_the_contract_shape(front_page: Page) -> None:
    Page.objects.create(slug="faciliteter", header="Faciliteter", body="<p>x</p>", description="Om\nhuset")
    Page.objects.create(slug=None, header="Uden adresse", body="<p>skjult</p>")
    NewsItem.objects.create(title="Nyhed", body="<p>n</p>", published_at="2026-11-01T12:00:00+01:00")
    Event.objects.create(title="Havedag", description="<p>e</p>", starts_on="2026-12-01")
    PageRedirect.objects.create(old_path="gamle-faciliteter", page=Page.objects.get(slug="faciliteter"))

    data = build_snapshot().data

    assert set(data) == {
        "format",
        "revision",
        "published_at",
        "site",
        "legacy_images",
        "pages",
        "news",
        "events",
        "menu",
        "redirects",
        "images",
    }
    assert data["format"] == 1 and re.fullmatch(r"[0-9a-f]{64}", data["revision"])
    assert data["site"] == {"front_page_id": 1, "default_background": ""}
    assert [(p["id"], p["path"]) for p in data["pages"]] == [
        (1, ""),
        (Page.objects.get(slug="faciliteter").pk, "faciliteter"),
    ]
    page = data["pages"][1]
    assert set(page) == {"id", "path", "title", "body", "background", "description", "updated_at"}
    assert page["description"] == "Om huset" and "+" in page["updated_at"]  # local time with offset
    assert data["news"] == [
        {
            "id": NewsItem.objects.get().pk,
            "title": "Nyhed",
            "body": "<p>n</p>",
            "published_at": "2026-11-01T12:00:00+01:00",
        }
    ]
    assert data["events"][0]["starts_on"] == "2026-12-01"
    assert data["menu"][0] == {"label": "Hjem", "url": "/"}  # the seeded menu
    # The front page's own slug and the old address both lead somewhere real.
    assert data["redirects"] == {"velkommen": "", "gamle-faciliteter": "faciliteter"}
    assert "Uden adresse" not in json.dumps(data)


@pytest.mark.django_db
def test_bodies_are_sanitized_before_they_are_sent(front_page: Page) -> None:
    """gahk.dk trusts the snapshot's HTML. A row stored without the editor (the old ETL stored the
    legacy HTML verbatim) must not reach it raw."""
    Page.objects.filter(pk=front_page.pk).update(body='<p onclick="x()">Hej</p><script>x()</script>')
    NewsItem.objects.create(
        title="N", body='<a href="javascript:x()">l</a>', published_at="2026-11-01T12:00:00+01:00"
    )
    Event.objects.create(
        title="E", description="<iframe src=//evil></iframe><p>e</p>", starts_on="2026-12-01"
    )

    data = build_snapshot().data

    assert data["pages"][0]["body"] == "<p>Hej</p>"
    assert "javascript" not in data["news"][0]["body"]
    assert data["events"][0]["description"] == "<p>e</p>"


@pytest.mark.django_db
def test_the_stored_html_is_cleaned_once_and_audited(front_page: Page) -> None:
    import importlib

    from django.apps import apps

    migration = importlib.import_module("cms.migrations.0008_clean_stored_html")
    Page.objects.filter(pk=front_page.pk).update(body="<p>ok</p><script>x()</script>")
    clean = Page.objects.create(slug="ren", header="Ren", body="<p>ren</p>")
    news = NewsItem.objects.create(
        title="N", body='<img src="x" onerror="y()">', published_at="2026-11-01T12:00:00+01:00"
    )
    version = PageVersion.objects.create(page=front_page, header="V", body="<script>x()</script><p>v</p>")
    ContentChange.objects.all().delete()

    migration.clean_stored_html(apps, None)

    front_page.refresh_from_db()
    news.refresh_from_db()
    version.refresh_from_db()
    assert front_page.body == "<p>ok</p>" and news.body == '<img src="x">' and version.body == "<p>v</p>"
    system = ContentChange.objects.filter(by__isnull=True, by_name__startswith="System")
    page_change = system.get(kind="page", object_id=front_page.pk)
    assert page_change.changes == {"body": ["<p>ok</p><script>x()</script>", "<p>ok</p>"]}
    assert system.filter(kind="news", object_id=news.pk).exists()
    assert system.filter(label__contains="1 gamle sideversioner").exists()
    assert not system.filter(object_id=clean.pk, kind="page").exists()  # unchanged: no entry


@pytest.mark.django_db
def test_the_revision_is_stable_and_follows_the_content(front_page: Page) -> None:
    first = build_snapshot(published_at=None)
    again = build_snapshot()
    assert first.revision == again.revision  # published_at differs; the revision does not
    assert compute_revision(first.data) == first.revision
    assert json.loads(first.body()) == first.data
    assert first.body() == canonical_json(first.data)

    MenuItem.objects.create(label="Ny", url="/ny", order=999)
    assert build_snapshot().revision != first.revision


@pytest.mark.django_db
def test_library_images_become_content_hashed_billeder_urls(front_page: Page) -> None:
    image = _library_image()
    unused = _library_image("ubrugt.jpg")
    front_page.body = f'<p><img src="{image.url}" alt="x"> og <img src="https://gahk.dk{image.url}"></p>'
    front_page.background_image = image.url
    front_page.save()

    snapshot = build_snapshot()

    image.refresh_from_db()
    with image.file.open("rb") as handle:
        assert image.sha256 == hashlib.sha256(handle.read()).hexdigest()  # filled in and saved
    [entry] = snapshot.data["images"]
    match = BILLEDER_RE.match(entry["url"])
    assert match and match.group(1) == image.sha256[:16] and match.group(2).startswith("have-foto")
    assert entry["url"].endswith(".jpg") and entry["content_type"] == "image/jpeg"
    assert entry["size"] == image.size and entry["sha256"] == image.sha256
    body = snapshot.data["pages"][0]["body"]
    assert body.count(entry["url"]) == 2 and "/media/" not in body
    assert snapshot.data["pages"][0]["background"] == entry["url"]
    assert set(snapshot.images) == {image.sha256} and unused.sha256 == ""


@pytest.mark.django_db
def test_a_background_that_is_not_a_library_image_is_sent_empty(front_page: Page) -> None:
    front_page.background_image = "https://example.com/x.jpg"
    front_page.save()
    SiteSettings.objects.update_or_create(pk=1, defaults={"default_background": "/media/cms/gone.jpg"})

    data = build_snapshot().data

    assert data["pages"][0]["background"] == "" and data["site"]["default_background"] == ""


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('<a href="/intern/begivenheder/3">', '<a href="https://intern.gahk.dk/begivenheder/3">'),
        ('<a href="https://www.gahk.dk/intern/">', '<a href="https://intern.gahk.dk/">'),
        ('<a href="http://gahk.dk/intern">', '<a href="https://intern.gahk.dk/">'),
        ('<a href="/intern?x=1">', '<a href="https://intern.gahk.dk/?x=1">'),
        ('<a href="/nyintern/foo">', '<a href="https://intern.gahk.dk/foo">'),
        ('<a href="/internat">', '<a href="/internat">'),
        ('<a href="https://example.com/intern/x">', '<a href="https://example.com/intern/x">'),
        ('<a href="/kollegielivet/intern">', '<a href="/kollegielivet/intern">'),
    ],
)
@override_settings(SITE_URL="https://intern.gahk.dk")
def test_links_into_the_intranet_point_at_intern(before: str, after: str) -> None:
    assert rewrite_intern_links(before) == after
    assert INTERN_LINK_RE  # the pattern is public for the editor's link checks


@pytest.mark.django_db
def test_pages_gahk_dk_would_refuse_are_left_out(front_page: Page) -> None:
    Page.objects.create(slug="Ugyldig Adresse", header="Ugyldig")
    Page.objects.create(slug="api", header="Reserveret")

    paths = [page["path"] for page in build_snapshot().data["pages"]]

    assert paths == [""]


@pytest.mark.django_db
@override_settings(SITE_URL="https://intern.gahk.dk")
def test_a_preview_keeps_library_images_on_intern(front_page: Page) -> None:
    image = _library_image()
    draft = Page(slug="kladde", header="Kladde", body=f'<img src="{image.url}"><a href="/intern/">i</a>')
    draft.background_image = image.url

    request = preview_request(draft)

    assert request["kind"] == "page" and request["media_base"] == "https://intern.gahk.dk"
    item = request["item"]
    assert item["id"] > 0 and item["path"] == "kladde"
    assert image.url in item["body"] and "https://intern.gahk.dk/" in item["body"]
    assert item["background"] == image.url
    assert preview_request(NewsItem(title="", body="<p>x</p>"))["item"]["title"] == "Nyhed"


@pytest.mark.django_db
def test_a_preview_background_with_a_danish_letter_is_a_plain_ascii_path(front_page: Page) -> None:
    """gahk.dk takes a preview background only as /media/ plus ASCII; anything else falls back."""
    old = CmsImage(caption="Gammel")
    old.file.save("Værelse.jpg", ContentFile(_image_bytes()), save=True)  # stored before the fold
    draft = Page(slug="kladde", header="Kladde", body="", background_image=old.url)

    background = preview_request(draft)["item"]["background"]

    assert background.startswith("/media/cms/") and "V%C3%A6relse" in background
    assert background.isascii()


def test_library_names_are_stored_in_ascii() -> None:
    from cms.images import stored_stem

    assert stored_stem("Tovtrækning_2019_1.jpg") == "Tovtraekning_2019_1"
    assert stored_stem("Forårskoncert 2020 (2).JPG") == "Foraarskoncert_2020_2"
    assert stored_stem("Ølkælder-søndag.png") == "Oelkaelder-soendag"
    assert stored_stem("Café.jpg") == "Cafe"
    assert stored_stem("…….jpg") == "billede"
