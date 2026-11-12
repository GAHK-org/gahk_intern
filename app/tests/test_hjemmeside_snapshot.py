"""What intern sends gahk.dk (cms.snapshot) and the one-time move of the legacy images into the
library (cms.legacy_images)."""

import hashlib
import json
import re
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.test import override_settings
from PIL import Image

from cms.legacy_images import convert_legacy_images, legacy_image_map, references, rewrite
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
    return Page.objects.create(pk=1, slug="velkommen", header="Velkommen", body="<p>Forside</p>")


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


# --------------------------------------------------------------------------------- legacy images
@pytest.fixture
def legacy_dir(tmp_path: Path, settings: Any) -> Path:  # noqa: ANN401
    root = tmp_path / "legacy-image"
    (root / "upload" / "images").mkdir(parents=True)
    (root / "upload" / "images" / "gang.jpg").write_bytes(_image_bytes(size=(3000, 1000)))
    (root / "upload" / "images" / "Hall(1).png").write_bytes(_image_bytes("PNG"))
    (root / "upload" / "images" / "Forårskoncert.jpg").write_bytes(_image_bytes())
    (root / "elements").mkdir()
    (root / "elements" / "logo.svg").write_text("<svg/>")
    return root


def test_every_legacy_form_is_recognised() -> None:
    text = (
        '<img src="/public/image/upload/images/gang.jpg">'
        '<img src="public/image/upload/images/gang.jpg">'
        '<img src="http://gahk.dk/public/image/upload/images/a.jpg">'
        '<img src="https://www.gahk.dk/public/image/upload/images/b.jpg">'
        '<img src="/static/legacy/image/upload/images/c.jpg">'
        '<div style="background:url(/public/image/upload/images/Hall(1).png)">'
        '<img src="https://example.com/public/image/upload/images/not-ours.jpg">'
    )
    assert references(text) == [
        ["upload/images/gang.jpg"],
        ["upload/images/a.jpg"],
        ["upload/images/b.jpg"],
        ["upload/images/c.jpg"],
        # Unquoted url(): with and without the ")" — the file that exists decides.
        ["upload/images/Hall(1).png)", "upload/images/Hall(1).png"],
    ]
    # The rewrite gives back the ")" of an unquoted CSS url() and leaves foreign URLs alone.
    known = {
        "upload/images/Hall(1).png": "/media/cms/hall.png",
        "upload/images/gang.jpg": "/media/cms/gang.jpg",
    }
    out = rewrite(text, known.get)
    assert "url(/media/cms/hall.png)" in out
    assert out.count('src="/media/cms/gang.jpg"') == 2
    assert "https://example.com/public/image/upload/images/not-ours.jpg" in out
    assert '"http://gahk.dk/public/image/upload/images/a.jpg"' in out  # unknown: untouched


@pytest.mark.django_db
def test_legacy_images_are_imported_once_and_the_content_rewritten(
    legacy_dir: Path, front_page: Page
) -> None:
    front_page.body = (
        '<img src="/public/image/upload/images/gang.jpg">'
        '<img src="http://www.gahk.dk/public/image/upload/images/Forårskoncert.jpg">'
        '<p style="background:url(/static/legacy/image/upload/images/Hall(1).png)">x</p>'
        '<img src="/public/image/upload/images/findes-ikke.jpg">'
        '<img src="/public/image/elements/logo.svg">'
    )
    front_page.background_image = "/public/image/upload/images/gang.jpg"
    front_page.save()
    news = NewsItem.objects.create(
        title="N",
        body='<img src="public/image/upload/images/gang.jpg">',
        published_at="2026-01-01T00:00+01:00",
    )
    event = Event.objects.create(
        title="E", description='<img src="/public/image/upload/images/gang.jpg">', starts_on="2026-01-01"
    )
    version = PageVersion.objects.create(
        page=front_page, header="V", body='<img src="/public/image/upload/images/gang.jpg">'
    )
    SiteSettings.objects.update_or_create(
        pk=1, defaults={"default_background": "/public/image/upload/images/gang.jpg"}
    )

    # A directory that does not exist is skipped; the next one has the files.
    result = convert_legacy_images([legacy_dir.parent / "not-collected", legacy_dir])

    assert sorted(result.imported) == [
        "upload/images/Forårskoncert.jpg",
        "upload/images/Hall(1).png",
        "upload/images/gang.jpg",
    ]
    assert result.missing == ["upload/images/findes-ikke.jpg"] and result.unusable == ["elements/logo.svg"]
    gang = CmsImage.objects.get(legacy_path="upload/images/gang.jpg")
    assert gang.uploaded_by is None and gang.caption == "gang" and gang.sha256
    with gang.file.open("rb") as handle:
        assert max(Image.open(handle).size) == 2400  # downscaled like an upload
    front_page.refresh_from_db()
    assert front_page.background_image == gang.url
    assert f'src="{gang.url}"' in front_page.body
    assert (
        "url(/media/cms/" in front_page.body
        and "Hall(1)" not in front_page.body.split("url(")[1].split(")")[0][-12:]
    )
    assert (
        "findes-ikke.jpg" in front_page.body and "elements/logo.svg" in front_page.body
    )  # left as they were
    for row in (news, event, version):
        row.refresh_from_db()
    assert gang.url in news.body and gang.url in event.description and gang.url in version.body
    assert SiteSettings.load().default_background == gang.url
    system = ContentChange.objects.filter(by__isnull=True, by_name__startswith="System")
    assert system.filter(action="upload", kind="image").count() == 3
    assert system.filter(action="update", kind="page", object_id=front_page.pk).exists()
    assert system.filter(kind="settings").exists() and system.filter(label__contains="sideversioner").exists()

    # Idempotent: nothing more to import or rewrite (the missing file is reported again).
    again = convert_legacy_images([legacy_dir])
    assert again.imported == [] and again.rewritten == 0
    assert CmsImage.objects.filter(legacy_path__isnull=False).count() == 3

    mapping = legacy_image_map()
    assert set(mapping) == {
        "/static/legacy/image/upload/images/gang.jpg",
        "/static/legacy/image/upload/images/Hall(1).png",
        "/static/legacy/image/upload/images/Forårskoncert.jpg",
    }
    snapshot = build_snapshot().data
    assert snapshot["legacy_images"] == mapping
    shipped = {image["url"] for image in snapshot["images"]}
    assert set(mapping.values()) <= shipped
    assert snapshot["site"]["default_background"] == mapping["/static/legacy/image/upload/images/gang.jpg"]
    assert "/public/image/upload/images/gang.jpg" not in json.dumps(snapshot)


@pytest.mark.django_db
def test_nothing_to_convert_is_a_no_op(legacy_dir: Path, front_page: Page) -> None:
    SiteSettings.objects.update_or_create(pk=1, defaults={"default_background": ""})  # seeded with gang.jpg

    result = convert_legacy_images([legacy_dir])
    assert (result.imported, result.rewritten, result.missing) == ([], 0, [])
    assert not ContentChange.objects.exists()


@pytest.mark.django_db
def test_an_edit_saved_during_the_import_is_kept(
    legacy_dir: Path, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cms import legacy_images

    front_page.body = '<img src="/public/image/upload/images/gang.jpg">'
    front_page.save()
    real_import = legacy_images._import

    def import_while_an_editor_saves(*args: Any, **kwargs: Any) -> CmsImage | None:  # noqa: ANN401
        # The editor's save lands while the (slow) import runs, after the rows were first read.
        Page.objects.filter(pk=front_page.pk).update(
            body='<p>Ny tekst</p><img src="/public/image/upload/images/gang.jpg">'
        )
        return real_import(*args, **kwargs)

    monkeypatch.setattr(legacy_images, "_import", import_while_an_editor_saves)

    convert_legacy_images([legacy_dir])

    front_page.refresh_from_db()
    gang = CmsImage.objects.get(legacy_path="upload/images/gang.jpg")
    assert front_page.body == f'<p>Ny tekst</p><img src="{gang.url}">'


@pytest.mark.django_db
def test_the_import_command_reads_the_directory_it_is_given(
    legacy_dir: Path, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.core.management import CommandError, call_command

    from cms.management.commands import import_legacy_images

    queued: list[str] = []
    monkeypatch.setattr(import_legacy_images, "publish_on_commit", queued.append)
    front_page.body = '<img src="/public/image/upload/images/gang.jpg"><img src="/public/image/upload/x.jpg">'
    front_page.save()
    out = StringIO()

    call_command("import_legacy_images", "--from", str(legacy_dir), stdout=out)

    assert "Imported 1 images; rewrote 2 rows." in out.getvalue()  # the page and the default background
    assert "Missing: upload/x.jpg" in out.getvalue()
    assert CmsImage.objects.filter(legacy_path="upload/images/gang.jpg").exists()
    assert queued == ["save"]
    with pytest.raises(CommandError):
        call_command("import_legacy_images", "--from", str(legacy_dir / "nowhere"))
