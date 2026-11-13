"""What intern sends gahk.dk (cms.snapshot)."""

import hashlib
import json
import re
import unicodedata
from io import BytesIO
from urllib.parse import unquote

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
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
from cms.services import upload_image
from cms.snapshot import (
    INTERN_LINK_RE,
    build_snapshot,
    canonical_json,
    compute_revision,
    preview_request,
    rewrite_intern_links,
)

# What gahk.dk takes: lower-case NFC letters and digits, words joined by one "-", "_" or ".".
IMAGE_URL_RE = re.compile(r"^/images/([0-9a-f]{16})/([^\W_]+(?:[-_.][^\W_]+)*)$")


def _gahk_dk_takes(name: str) -> bool:
    match = IMAGE_URL_RE.match(f"/images/{'0' * 16}/{name}")
    return bool(match) and name == name.lower() and unicodedata.is_normalized("NFC", name)


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
def test_library_images_become_content_hashed_public_image_urls(front_page: Page) -> None:
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
    match = IMAGE_URL_RE.match(entry["url"])
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
def test_a_preview_background_with_a_danish_letter_is_percent_encoded(front_page: Page) -> None:
    """gahk.dk takes a preview background as /media/cms/ plus a percent-encoded path."""
    old = CmsImage(caption="Gammel")
    old.file.save("Værelse (1).jpg", ContentFile(_image_bytes()), save=True)  # Django's own name
    new = upload_image(SimpleUploadedFile("Værelse.jpg", _image_bytes(), content_type="image/jpeg"), "", None)
    for image in (old, new):
        draft = Page(slug="kladde", header="Kladde", body="", background_image=image.url)

        background = preview_request(draft)["item"]["background"]

        assert background.startswith("/media/cms/") and "V%C3%A6relse" in background
        assert re.fullmatch(r"/media/cms(?:/(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)+", background)
        assert unquote(background) == f"/media/{image.file.name}"


@pytest.mark.parametrize(
    ("file_name", "stem"),
    [
        ("Tovtrækning_2019_1.jpg", "Tovtrækning_2019_1"),
        ("Forårskoncert 2020 (2).JPG", "Forårskoncert_2020_2"),
        ("Ølkælder-søndag.png", "Ølkælder-søndag"),
        ("Café.jpg", "Café"),
        (unicodedata.normalize("NFD", "Værelse på Østergården.jpg"), "Værelse_på_Østergården"),
        ("Skærmbillede 2026-09-04 kl. 10.11.12.png", "Skærmbillede_2026-09-04_kl.10.11.12"),
        ("İstanbul.jpg", "İstanbul"),
        ("…….jpg", "image"),
        ("._.jpg", "image"),
    ],
)
def test_library_names_keep_their_letters(file_name: str, stem: str) -> None:
    from cms.images import stored_stem

    assert stored_stem(file_name) == stem
    assert unicodedata.is_normalized("NFC", stored_stem(file_name))


@pytest.mark.parametrize(
    ("file_name", "content_type", "name"),
    [
        ("cms/2026/11/Tovtrækning_2019_1.jpg", "image/jpeg", "tovtrækning-2019-1.jpg"),
        ("Forårskoncert 2020 (2).JPG", "image/jpeg", "forårskoncert-2020-2.jpg"),
        ("ØLKÆLDER søndag.jpeg", "image/jpeg", "ølkælder-søndag.jpeg"),
        (unicodedata.normalize("NFD", "Værelse på Øen.png"), "image/png", "værelse-på-øen.png"),
        ("Crème brûlée.gif", "image/gif", "crème-brûlée.gif"),
        ("STRAẞE.jpg", "image/jpeg", "straße.jpg"),
        ("İstanbul.jpg", "image/jpeg", "istanbul.jpg"),
        ("Have_Foto_aB3x9Zq.png", "image/png", "have-foto-ab3x9zq.png"),  # ASCII: as it always was
        ("_960w.webp", "image/webp", "960w.webp"),  # never one of gahk.dk's own variant names
        (".skjult.jpg", "image/jpeg", "skjult.jpg"),
        ("…….jpg", "image/jpeg", "image.jpg"),
        ("foto.jpg", "image/png", "foto.png"),  # the extension follows the content
    ],
)
def test_public_image_names_keep_their_letters_in_lower_case(
    file_name: str, content_type: str, name: str
) -> None:
    from cms.images import public_image_name

    assert public_image_name(file_name, content_type) == name
    assert _gahk_dk_takes(name)


def test_a_long_public_image_name_is_cut_by_bytes_as_well_as_letters() -> None:
    from cms.images import PUBLIC_IMAGE_STEM_MAX_BYTES, public_image_name

    for letter, length in (("a", 80), ("æ", 60), ("あ", 40)):
        name = public_image_name(letter * 300 + ".jpg", "image/jpeg")
        stem = name.removesuffix(".jpg")
        assert len(stem.encode()) <= PUBLIC_IMAGE_STEM_MAX_BYTES and stem == letter * length
        assert _gahk_dk_takes(name)


def test_a_long_ascii_public_image_name_is_cut_where_it_always_was() -> None:
    """Already published URLs must not move: an ASCII stem is still cut at 80 characters."""
    from cms.images import public_image_name

    stored = "cms/2024/03/" + "Sommerfest_i_gaarden_" * 5 + "aB3x9Zq.jpg"  # a 100-letter stem + suffix
    name = public_image_name(stored, "image/jpeg")

    assert name == ("sommerfest-i-gaarden-" * 4)[:80].strip("-") + ".jpg"


@pytest.mark.django_db
def test_an_upload_from_a_mac_keeps_its_letters_in_both_urls(front_page: Page) -> None:
    """A Mac sends "å" decomposed (NFD); the library stores it composed, and gahk.dk's URL has it."""
    upload = SimpleUploadedFile(
        unicodedata.normalize("NFD", "Forår i gården.jpg"), _image_bytes(), "image/jpeg"
    )
    image = upload_image(upload, "Forår", None)
    front_page.body = f'<img src="{image.url}">'
    front_page.save()

    assert image.file.name.startswith("cms/") and "/Forår_i_gården" in image.file.name
    assert unicodedata.is_normalized("NFC", image.file.name)
    assert "/For%C3%A5r_i_g%C3%A5rden" in image.url  # percent-encoded UTF-8, as a URL
    snapshot = build_snapshot().data
    [entry] = snapshot["images"]
    assert entry["url"].endswith("/forår-i-gården.jpg") and _gahk_dk_takes(entry["url"].rsplit("/", 1)[1])
    assert f'src="{entry["url"]}"' in snapshot["pages"][0]["body"]
