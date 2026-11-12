"""The website's content model and its write path (cms.services): every write is cleaned, versioned,
audited and published after the commit."""

import importlib
from collections.abc import Callable
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from PIL import Image

from cms import services
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
from cms.paths import validate_page_path
from residents.models import Resident


@pytest.fixture
def editor(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="redaktor@gahk.dk", first_name="Rita", last_name="Redaktør")


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every publish the services queue, instead of running it."""
    calls: list[dict[str, Any]] = []
    from cms import tasks

    monkeypatch.setattr(tasks.publish_gahk_web, "delay", lambda **kwargs: calls.append(kwargs))
    return calls


def _png(width: int, height: int, mode: str = "RGB", fmt: str = "PNG", **save: Any) -> bytes:  # noqa: ANN401
    buffer = BytesIO()
    Image.new(mode, (width, height), "green").save(buffer, format=fmt, **save)
    return buffer.getvalue()


# ------------------------------------------------------------------------------------- addresses
@pytest.mark.parametrize("path", ["optagelse", "faciliteter/kokken", "vision"])
def test_ordinary_addresses_are_accepted(path: str) -> None:
    validate_page_path(path)


@pytest.mark.parametrize(
    "path",
    ["api", "billeder/x", "begivenheder", "s", "wiki", "404", "optagelse/ansoeg", "optagelse/fremlej"],
)
def test_addresses_gahk_dk_uses_itself_are_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        validate_page_path(path)


# ----------------------------------------------------------------------------------------- pages
@pytest.mark.django_db
def test_creating_a_page_cleans_versions_audits_and_publishes(
    editor: Resident,
    published: list[dict[str, Any]],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        page = services.save_page(
            None,
            {
                "slug": "/Vision/",
                "header": "  Vores\n vision <b>nu</b> ",
                "body": '<p>Hej</p><script>alert(1)</script><a href="javascript:x()">x</a>',
                "description": "Kort\ntekst",
            },
            editor,
        )

    assert page.slug == "vision"
    assert page.header == "Vores vision <b>nu</b>"  # plain text: kept literally, escaped when shown
    assert "<script" not in page.body and "javascript:" not in page.body and "<p>Hej</p>" in page.body
    assert page.description == "Kort tekst"
    assert page.updated_by == editor
    assert page.versions.count() == 1
    change = ContentChange.objects.get()
    assert (change.action, change.kind, change.object_id, change.by, change.by_name) == (
        "create",
        "page",
        page.pk,
        editor,
        "Rita Redaktør",
    )
    assert change.changes["body"][0] is None and "<p>Hej</p>" in change.changes["body"][1]
    assert published == [{"reason": "save", "triggered_by_id": editor.pk}]


@pytest.mark.django_db
def test_nothing_is_published_before_the_commit(editor: Resident, published: list[dict[str, Any]]) -> None:
    services.save_page(None, {"header": "Kladde"}, editor)
    assert published == []  # pytest's transaction never commits, so on_commit never ran


@pytest.mark.django_db
def test_editing_snapshots_the_old_content_first_and_records_before_and_after(editor: Resident) -> None:
    page = Page.objects.create(slug="kontakt", header="Kontakt", body="<p>gammel</p>")  # e.g. the ETL

    services.save_page(page, {"body": "<p>ny</p>"}, editor)

    notes = list(page.versions.order_by("created_at", "id").values_list("note", "body"))
    assert notes == [
        ("Før første redigering (forfatter ukendt)", "<p>gammel</p>"),
        ("Redigeret", "<p>ny</p>"),
    ]
    change = ContentChange.objects.get()
    assert change.changes == {"body": ["<p>gammel</p>", "<p>ny</p>"]}


@pytest.mark.django_db
def test_saving_without_a_change_writes_nothing(editor: Resident) -> None:
    page = services.save_page(None, {"header": "Legater", "slug": "legater"}, editor)
    before = (PageVersion.objects.count(), ContentChange.objects.count())

    services.save_page(page, {"header": "Legater", "slug": "legater"}, editor)

    assert (PageVersion.objects.count(), ContentChange.objects.count()) == before


@pytest.mark.django_db
def test_renaming_a_page_keeps_the_old_address_as_a_redirect(editor: Resident) -> None:
    page = services.save_page(None, {"header": "Køkken", "slug": "faciliteter/kokken"}, editor)

    services.save_page(page, {"slug": "faciliteter/koekken"}, editor)

    assert PageRedirect.objects.get(old_path="faciliteter/kokken").page == page


@pytest.mark.django_db
def test_a_taken_or_reserved_address_is_a_field_error(editor: Resident) -> None:
    services.save_page(None, {"header": "A", "slug": "vision"}, editor)
    with pytest.raises(ValidationError) as taken:
        services.save_page(None, {"header": "B", "slug": "vision"}, editor)
    assert "slug" in taken.value.message_dict
    with pytest.raises(ValidationError) as reserved:
        services.save_page(None, {"header": "B", "slug": "optagelse/ansoeg"}, editor)
    assert "slug" in reserved.value.message_dict
    with pytest.raises(ValidationError) as blank:
        services.save_page(None, {"header": "  "}, editor)
    assert "header" in blank.value.message_dict


@pytest.mark.django_db
def test_a_background_must_come_from_the_library(editor: Resident) -> None:
    page = Page.objects.create(slug="x", header="X", background_image="/public/image/old.jpg")
    services.save_page(
        page, {"background_image": "/public/image/old.jpg", "header": "Y"}, editor
    )  # unchanged: ok
    with pytest.raises(ValidationError) as exc:
        services.save_page(page, {"background_image": "https://example.com/x.jpg"}, editor)
    assert "background_image" in exc.value.message_dict


@pytest.mark.django_db
def test_restore_puts_content_back_and_explains_a_lost_address(editor: Resident) -> None:
    page = services.save_page(None, {"header": "Før", "slug": "gammel", "body": "<p>1</p>"}, editor)
    first = page.versions.get()
    page = services.save_page(page, {"header": "Efter", "slug": "ny", "body": "<p>2</p>"}, editor)
    services.save_page(None, {"header": "Anden", "slug": "gammel"}, editor)  # takes the old address

    result = services.restore_version(page, first, editor)

    page.refresh_from_db()
    assert (page.header, page.body, page.slug) == ("Før", "<p>1</p>", "ny")
    assert result.warnings and "bruges nu af en anden side" in result.warnings[0]
    assert ContentChange.objects.filter(action="restore", object_id=page.pk).exists()
    assert page.versions.filter(note="Før gendannelse").exists()


@pytest.mark.django_db
def test_restore_sanitizes_again(editor: Resident) -> None:
    page = services.save_page(None, {"header": "P"}, editor)
    bad = PageVersion.objects.create(page=page, header="P", body="<p>ok</p><script>x()</script>")

    services.restore_version(page, bad, editor)

    page.refresh_from_db()
    assert "<script" not in page.body


@pytest.mark.django_db
def test_deleting_a_page_keeps_its_history_and_the_front_page_cannot_go(editor: Resident) -> None:
    front = Page.objects.create(pk=1, slug="velkommen", header="Velkommen")
    page = services.save_page(None, {"header": "Væk", "slug": "vaek", "body": "<p>tekst</p>"}, editor)

    services.delete_page(page, editor)

    assert not Page.objects.filter(slug="vaek").exists()
    assert PageVersion.objects.filter(header="Væk", page__isnull=True).count() >= 2
    deleted = ContentChange.objects.get(action="delete")
    assert deleted.changes["body"] == ["<p>tekst</p>", None]
    with pytest.raises(ValidationError):
        services.delete_page(front, editor)


# ------------------------------------------------------------------------------- news and events
@pytest.mark.django_db
def test_news_and_events_are_cleaned_and_audited(editor: Resident) -> None:
    news = services.save_news(None, {"title": "Fest\n", "body": "<p>Kom</p><img src=x onerror=y()>"}, editor)
    event = services.save_event(
        None, {"title": "Havedag", "description": "<p>Ja</p>", "starts_on": "2026-12-01"}, editor
    )
    services.save_event(event, {"title": "Havedag!"}, editor)

    assert news.title == "Fest" and "onerror" not in news.body and news.published_at is not None
    assert event.starts_on.isoformat() == "2026-12-01"
    assert list(ContentChange.objects.order_by("id").values_list("kind", "action")) == [
        ("news", "create"),
        ("event", "create"),
        ("event", "update"),
    ]
    assert ContentChange.objects.first().changes == {"title": ["Havedag", "Havedag!"]}  # type: ignore[union-attr]  # newest first

    services.delete_news(news, editor)
    services.delete_event(event, editor)
    assert not NewsItem.objects.exists() and not Event.objects.exists()
    with pytest.raises(ValidationError):
        services.save_event(None, {"title": "Uden dato"}, editor)


# ---------------------------------------------------------------------------------------- images
@pytest.mark.django_db
def test_an_upload_is_downscaled_stripped_and_hashed(editor: Resident) -> None:
    exif = Image.Exif()
    exif[0x8825] = {2: (55, 41, 0)}  # a GPS position
    buffer = BytesIO()
    Image.new("RGB", (4000, 3000), "green").save(buffer, format="JPEG", exif=exif)
    upload = SimpleUploadedFile("Ferie Foto.JPG", buffer.getvalue(), content_type="image/jpeg")

    image = services.upload_image(upload, "Haven om sommeren", editor)

    with image.file.open("rb") as handle:
        data = handle.read()
    stored = Image.open(BytesIO(data))
    assert max(stored.size) == 2400 and stored.format == "JPEG"
    assert not stored.getexif()  # the GPS position is gone
    assert image.sha256 == __import__("hashlib").sha256(data).hexdigest()
    assert (image.content_type, image.size) == ("image/jpeg", len(data))
    assert image.file.name.endswith(".jpg")
    assert ContentChange.objects.get().action == "upload"


@pytest.mark.django_db
def test_a_transparent_png_stays_a_png(editor: Resident) -> None:
    upload = SimpleUploadedFile("logo.png", _png(10, 10, mode="RGBA"), content_type="image/png")
    image = services.upload_image(upload, "", editor)
    with image.file.open("rb") as handle:
        assert Image.open(handle).mode == "RGBA"
    assert image.content_type == "image/png"


@pytest.mark.django_db
def test_an_svg_or_a_non_image_is_a_file_error(editor: Resident) -> None:
    for name, data, content_type in (
        ("x.svg", b"<svg/>", "image/svg+xml"),
        ("x.png", b"not an image", "image/png"),
    ):
        with pytest.raises(ValidationError) as exc:
            services.upload_image(SimpleUploadedFile(name, data, content_type=content_type), "", editor)
        assert "file" in exc.value.message_dict
    assert not CmsImage.objects.exists()


@pytest.mark.django_db
def test_image_usage_and_delete(editor: Resident) -> None:
    image = services.upload_image(
        SimpleUploadedFile("a.png", _png(5, 5), content_type="image/png"), "A", editor
    )
    Page.objects.create(slug="p", header="P", body=f'<img src="{image.url}">')
    SiteSettings.objects.update_or_create(pk=1, defaults={"default_background": image.url})

    assert services.image_usage(image) == ["Side: P", "Standardbaggrund"]
    services.update_image(image, "Ny tekst", editor)
    services.delete_image(image, editor)
    assert not CmsImage.objects.exists()
    assert list(ContentChange.objects.order_by("id").values_list("action", flat=True)) == [
        "upload",
        "update",
        "delete",
    ]


# ------------------------------------------------------------------------------------------ menu
@pytest.mark.django_db
def test_the_menu_is_replaced_as_a_whole_and_audited(editor: Resident) -> None:
    MenuItem.objects.all().delete()
    services.save_menu(
        [{"label": "Hjem", "url": "/"}, {"label": "Facebook", "url": "https://facebook.com/gahk"}], editor
    )

    services.save_menu(
        [{"label": "Facebook", "url": "https://facebook.com/gahk"}, {"label": " Hjem ", "url": "/"}], editor
    )

    assert [(m.label, m.url) for m in MenuItem.objects.all()] == [
        ("Facebook", "https://facebook.com/gahk"),
        ("Hjem", "/"),
    ]
    last = ContentChange.objects.filter(kind="menu").first()  # newest first
    assert last is not None and last.changes["menu"][0][0]["label"] == "Hjem"
    services.delete_menu_item(MenuItem.objects.get(label="Hjem"), editor)
    assert MenuItem.objects.count() == 1


@pytest.mark.django_db
def test_bad_menu_entries_are_reported_by_position(editor: Resident) -> None:
    with pytest.raises(ValidationError) as exc:
        services.save_menu(
            [
                {"label": "", "url": "/"},
                {"label": "Ond", "url": "javascript:alert(1)"},
                {"label": "X", "url": "//evil"},
            ],
            editor,
        )
    errors = exc.value.message_dict["items"]
    assert len(errors) == 3 and errors[0].startswith("Punkt 1")


@pytest.mark.django_db
def test_reachability_follows_the_menu() -> None:
    MenuItem.objects.all().delete()
    MenuItem.objects.create(label="Faciliteter", url="/faciliteter", order=0)
    front = Page.objects.create(pk=1, slug="velkommen", header="Velkommen")
    sub = Page.objects.create(slug="faciliteter/kokken", header="Køkken")
    orphan = Page.objects.create(slug="glemt", header="Glemt")
    Page.objects.create(slug=None, header="Uden adresse")

    assert services.is_reachable(front) and services.is_reachable(sub)
    assert services.unreachable_pages() == [orphan]


# ------------------------------------------------------------------------- redirects and settings
@pytest.mark.django_db
def test_redirects_are_validated_and_audited(editor: Resident) -> None:
    page = Page.objects.create(slug="kontakt", header="Kontakt")
    Page.objects.create(slug="adresseloes", header="Ingen")  # a live address

    redirect = services.save_redirect(None, {"old_path": "/Gammel-Kontakt/", "page": page.pk}, editor)

    assert redirect.old_path == "gammel-kontakt" and redirect.created_by == editor
    with pytest.raises(ValidationError) as live:
        services.save_redirect(None, {"old_path": "adresseloes", "page": page}, editor)
    assert "old_path" in live.value.message_dict
    with pytest.raises(ValidationError) as reserved:
        services.save_redirect(None, {"old_path": "api", "page": page}, editor)
    assert "old_path" in reserved.value.message_dict
    services.delete_redirect(redirect, editor)
    assert list(ContentChange.objects.values_list("kind", "action").order_by("id")) == [
        ("redirect", "create"),
        ("redirect", "delete"),
    ]


@pytest.mark.django_db
def test_site_settings_take_a_library_image(editor: Resident) -> None:
    image = services.upload_image(
        SimpleUploadedFile("b.png", _png(5, 5), content_type="image/png"), "", editor
    )

    services.save_site_settings({"default_background": image.url}, editor)

    assert SiteSettings.load().default_background == image.url
    with pytest.raises(ValidationError):
        services.save_site_settings({"default_background": "/elsewhere.jpg"}, editor)


@pytest.mark.django_db
def test_manual_publish_and_update_are_audited(
    editor: Resident,
    published: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    from cms import tasks

    nudges: list[bool] = []
    monkeypatch.setattr(tasks.nudge_gahk_web_update, "delay", lambda: nudges.append(True))
    with django_capture_on_commit_callbacks(execute=True):
        services.request_publish(editor)
        services.request_gahk_web_update(editor)

    assert published == [{"reason": "manual", "triggered_by_id": editor.pk}]
    assert nudges == [True]
    assert ContentChange.objects.filter(kind="site").count() == 2


# ------------------------------------------------------------------------------------ migrations
def _migration(name: str) -> Any:  # noqa: ANN401
    return importlib.import_module(f"cms.migrations.{name}")


@pytest.mark.django_db
def test_the_menu_is_seeded_from_the_old_hard_coded_one() -> None:
    migration = _migration("0006_seed_hjemmeside")
    MenuItem.objects.all().delete()

    migration.seed_menu(apps, None)
    migration.seed_menu(apps, None)  # idempotent

    assert [(m.label, m.url) for m in MenuItem.objects.all()][:3] == [
        ("Hjem", "/"),
        ("Historie", "/kollegielivet/historie"),
        ("Vision", "/vision"),
    ]
    assert MenuItem.objects.count() == 9
    migration.unseed_menu(apps, None)
    assert not MenuItem.objects.exists()


@pytest.mark.django_db
def test_the_hero_images_and_titles_become_content() -> None:
    migration = _migration("0006_seed_hjemmeside")
    SiteSettings.objects.all().delete()
    Page.objects.create(pk=1, slug="velkommen", header="Velkommen til <b>GAHK</b> &amp; co")
    NewsItem.objects.create(title="&quot;Fest&quot;", published_at="2026-01-01T00:00:00+01:00")

    migration.seed_backgrounds(apps, None)
    migration.titles_to_plain_text(apps, None)

    assert SiteSettings.load().default_background == "/public/image/upload/images/gang.jpg"
    front = Page.objects.get(pk=1)
    assert front.background_image == migration.HOME_HERO
    assert front.header == "Velkommen til GAHK & co"
    assert NewsItem.objects.get().title == '"Fest"'


@pytest.mark.django_db
def test_the_optagelse_text_becomes_a_page_once() -> None:
    migration = _migration("0007_optagelse_page")
    editor = SimpleNamespace(connection=connection)

    migration.create_optagelse_page(apps, editor)
    assert not Page.objects.filter(slug="optagelse").exists()  # an empty database gets nothing

    Page.objects.create(pk=1, slug="velkommen", header="Velkommen")
    migration.create_optagelse_page(apps, editor)
    migration.create_optagelse_page(apps, editor)

    page = Page.objects.get(slug="optagelse")
    assert page.header == "Optagelse" and page.pk != 1
    assert '<a href="/optagelse/ansoeg">' in page.body and '<a href="/optagelse/fremlej">' in page.body
    assert "5.616 kr. pr. måned" in page.body and "<blockquote>" in page.body
    assert "{%" not in page.body and "class=" not in page.body
    assert page.versions.count() == 1
    migration.remove_optagelse_page(apps, editor)
    assert not Page.objects.filter(slug="optagelse").exists()
