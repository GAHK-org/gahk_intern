"""The old site's images moving into the library by themselves (cms.legacy_images), read from
static/legacy/image/ — here from a temporary directory of the test's own."""

import json
from collections.abc import Callable, Iterator
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection
from django.urls import reverse
from PIL import Image

from cms import legacy_images
from cms.images import EXTENSION_TYPES
from cms.legacy_images import (
    IMPORT_LOCK_ID,
    convert_legacy_images,
    import_pending_legacy_images,
    legacy_files,
    legacy_image_map,
    legacy_state,
    references,
    rewrite,
)
from cms.models import (
    CmsImage,
    ContentChange,
    Event,
    LegacyImageFailure,
    NewsItem,
    Page,
    PageVersion,
    SiteSettings,
)
from cms.services import usage_map
from cms.snapshot import build_snapshot
from residents.models import Resident


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


class OldFiles:
    """static/legacy/image/ for one test: a temporary directory, and every legacy path the import
    reads from it, in order."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.reads: list[str] = []

    def put(self, legacy_path: str, data: bytes) -> None:
        path = self.root / legacy_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@pytest.fixture
def old_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> OldFiles:
    """Three old images. Nothing is listed (see _list_all): most tests look at what the content
    refers to alone."""
    files = OldFiles(tmp_path / "old-images")
    files.put("upload/images/gang.jpg", image_bytes(size=(3000, 1000)))
    files.put("upload/images/Hall(1).png", image_bytes("PNG"))
    files.put("upload/images/aaretsgang/nytår 2014-15.jpg", image_bytes())
    monkeypatch.setattr(legacy_images, "LEGACY_DIRECTORY", files.root)
    monkeypatch.setattr(legacy_images, "LISTED_DIRECTORY", "(nothing)/")
    real_read = legacy_images.read_legacy_file

    def recording_read(legacy_path: str, found: Any) -> bytes:  # noqa: ANN401
        files.reads.append(legacy_path)
        return real_read(legacy_path, found)

    monkeypatch.setattr(legacy_images, "read_legacy_file", recording_read)
    return files


def _list_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every file below upload/ is to be imported, as in production."""
    monkeypatch.setattr(legacy_images, "LISTED_DIRECTORY", "upload/")


@pytest.fixture
def front_page() -> Page:
    SiteSettings.objects.update_or_create(pk=1, defaults={"default_background": ""})  # seeded with gang.jpg
    return Page.put_front_page(slug="velkommen", header="Velkommen", body="<p>Forside</p>")


def _refer(page: Page, *paths: str) -> None:
    page.body = "".join(f'<img src="/public/image/{path}">' for path in paths)
    page.save()


# -------------------------------------------------------------------------------- references
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


def test_css_after_an_unquoted_or_entity_quoted_url_is_not_part_of_the_file_name() -> None:
    # nh3 writes the quotes of url("…") inside a style attribute as &quot;, and a declaration may
    # follow the ")" without a space; neither belongs to the file name.
    text = (
        '<p style="background:url(/public/image/upload/images/gang.jpg);color:red">'
        '<p style="background:url(&quot;/public/image/upload/images/Hall(1).png&quot;)">'
    )
    assert [legacy_images._name_of(candidates) for candidates in references(text)] == [
        "upload/images/gang.jpg",
        "upload/images/Hall(1).png",
    ]
    known = {
        "upload/images/gang.jpg": "/media/cms/gang.jpg",
        "upload/images/Hall(1).png": "/media/cms/hall.png",
    }
    assert rewrite(text, known.get) == (
        '<p style="background:url(/media/cms/gang.jpg);color:red">'
        '<p style="background:url(&quot;/media/cms/hall.png&quot;)">'
    )


def test_what_no_file_can_be_called_is_no_reference() -> None:
    # A NUL (PostgreSQL text cannot hold one) or a path longer than legacy_path takes would break
    # every run at the database instead of being recorded.
    assert references('<img src="/public/image/upload/a%00.jpg">') == []
    assert references(f'<img src="/public/image/upload/{"a" * 260}.jpg">') == []


# ------------------------------------------------------------------------------------ import
# ------------------------------------------------------------------------------------ import
@pytest.mark.django_db
def test_referenced_images_are_imported_and_the_content_rewritten(
    old_files: OldFiles, front_page: Page
) -> None:
    front_page.body = (
        '<img src="/public/image/upload/images/gang.jpg">'
        '<img src="http://www.gahk.dk/public/image/upload/images/aaretsgang/nyt%C3%A5r%202014-15.jpg">'
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
    SiteSettings.objects.filter(pk=1).update(default_background="/public/image/upload/images/gang.jpg")

    result = import_pending_legacy_images()

    # Each file once; the svg is never asked for (not an image name), nor "Hall(1).png)" with the ")"
    # of the unquoted url().
    assert sorted(old_files.reads) == [
        "upload/images/Hall(1).png",
        "upload/images/aaretsgang/nytår 2014-15.jpg",
        "upload/images/findes-ikke.jpg",
        "upload/images/gang.jpg",
    ]
    assert sorted(result.imported) == [
        "upload/images/Hall(1).png",
        "upload/images/aaretsgang/nytår 2014-15.jpg",
        "upload/images/gang.jpg",
    ]
    assert result.missing == ["upload/images/findes-ikke.jpg"] and result.unusable == ["elements/logo.svg"]
    hallway = CmsImage.objects.get(legacy_path="upload/images/gang.jpg")
    assert hallway.uploaded_by is None and hallway.caption == "gang" and hallway.sha256
    with hallway.file.open("rb") as handle:
        assert max(Image.open(handle).size) == 2400  # downscaled like an upload
    new_year = CmsImage.objects.get(legacy_path="upload/images/aaretsgang/nytår 2014-15.jpg")
    assert "/nytår_2014-15" in new_year.file.name  # the name keeps its letters
    assert "/nyt%C3%A5r_2014-15" in new_year.url  # and is percent-encoded as a URL
    front_page.refresh_from_db()
    assert front_page.background_image == hallway.url
    assert f'src="{hallway.url}"' in front_page.body and f'src="{new_year.url}"' in front_page.body
    assert "url(/media/cms/" in front_page.body and "Hall(1)" not in front_page.body
    assert (
        "findes-ikke.jpg" in front_page.body and "elements/logo.svg" in front_page.body
    )  # left as they were
    for row in (news, event, version):
        row.refresh_from_db()
    assert hallway.url in news.body and hallway.url in event.description and hallway.url in version.body
    assert SiteSettings.load().default_background == hallway.url
    system = ContentChange.objects.filter(by__isnull=True, by_name__startswith="System")
    assert system.filter(action="upload", kind="image").count() == 3
    assert system.filter(action="update", kind="page", object_id=front_page.pk).exists()
    assert system.filter(kind="settings").exists() and system.filter(label__contains="sideversioner").exists()

    mapping = legacy_image_map()
    assert set(mapping) == {
        "/static/legacy/image/upload/images/gang.jpg",
        "/static/legacy/image/upload/images/Hall(1).png",
        "/static/legacy/image/upload/images/aaretsgang/nytår 2014-15.jpg",
    }
    snapshot = build_snapshot().data
    assert snapshot["legacy_images"] == mapping
    assert set(mapping.values()) <= {image["url"] for image in snapshot["images"]}
    assert snapshot["site"]["default_background"] == mapping["/static/legacy/image/upload/images/gang.jpg"]
    assert "/public/image/upload/images/gang.jpg" not in json.dumps(snapshot)

    # The next run: everything is in the library or recorded, so it reads nothing.
    old_files.reads.clear()
    again = import_pending_legacy_images()
    assert old_files.reads == []
    assert again.imported == [] and again.rewritten == 0
    assert sorted(again.skipped) == ["elements/logo.svg", "upload/images/findes-ikke.jpg"]
    assert CmsImage.objects.filter(legacy_path__isnull=False).count() == 3


@pytest.mark.django_db
def test_nothing_referenced_means_nothing_read_and_no_change(old_files: OldFiles, front_page: Page) -> None:
    result = import_pending_legacy_images()

    assert result.summary() == {
        "imported": 0,
        "rewritten": 0,
        "missing": 0,
        "unusable": 0,
        "deferred": 0,
        "error": "",
        "busy": False,
    }
    assert old_files.reads == []
    assert not ContentChange.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (None, "missing"),
        (b"<html>login</html>", "unusable"),
        (b"not an image at all", "unusable"),
        (image_bytes("BMP"), "unusable"),  # decodes, but not a web format
        (image_bytes()[:200], "unusable"),  # cut off
    ],
)
def test_a_file_that_cannot_be_used_is_recorded_and_not_asked_for_again(
    old_files: OldFiles, front_page: Page, data: bytes | None, reason: str
) -> None:
    if data is not None:
        old_files.put("upload/images/bad.jpg", data)
    _refer(front_page, "upload/images/bad.jpg")

    first = import_pending_legacy_images()

    failure = LegacyImageFailure.objects.get()
    assert (failure.legacy_path, failure.reason) == ("upload/images/bad.jpg", reason)
    assert (first.missing if reason == "missing" else first.unusable) == ["upload/images/bad.jpg"]
    assert not CmsImage.objects.exists()
    front_page.refresh_from_db()
    assert "/public/image/upload/images/bad.jpg" in front_page.body

    old_files.reads.clear()
    for _run in range(3):
        assert import_pending_legacy_images().skipped == ["upload/images/bad.jpg"]
    assert old_files.reads == []


@pytest.mark.django_db
def test_a_file_over_the_size_cap_is_refused_without_reading_it(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(legacy_images, "MAX_FILE_BYTES", 1000)
    big = image_bytes(size=(400, 400))
    assert len(big) > 1000
    old_files.put("upload/images/stor.jpg", big)
    _refer(front_page, "upload/images/stor.jpg")

    result = import_pending_legacy_images()

    assert result.unusable == ["upload/images/stor.jpg"]
    assert LegacyImageFailure.objects.get().detail == "more than 1000 bytes"


@pytest.mark.django_db
def test_a_run_stops_at_its_budget_and_the_next_one_goes_on(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [f"upload/images/billede{number}.jpg" for number in range(5)]
    for path in paths:
        old_files.put(path, image_bytes())
    _refer(front_page, *paths)
    monkeypatch.setattr(legacy_images, "AUTO_MAX_FILES", 2)

    first = import_pending_legacy_images()
    assert (len(first.imported), first.deferred, len(old_files.reads)) == (2, 3, 2)
    front_page.refresh_from_db()
    assert front_page.body.count("/public/image/") == 3  # what was imported is already rewritten

    second = import_pending_legacy_images()
    third = import_pending_legacy_images()
    assert (len(second.imported), len(third.imported), third.deferred) == (2, 1, 0)
    front_page.refresh_from_db()
    assert "/public/image/" not in front_page.body
    assert sorted(old_files.reads) == sorted(paths)  # each file read once


@pytest.mark.django_db
def test_a_run_out_of_time_imports_nothing_more(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refer(front_page, "upload/images/gang.jpg")
    monkeypatch.setattr(legacy_images, "AUTO_SECONDS", 0)

    result = import_pending_legacy_images()

    assert (result.deferred, old_files.reads) == (1, [])
    assert not LegacyImageFailure.objects.exists()


@pytest.mark.django_db
def test_an_edit_saved_during_the_import_is_kept(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refer(front_page, "upload/images/gang.jpg")
    real_read = legacy_images.read_legacy_file

    def read_while_an_editor_saves(legacy_path: str, found: Any) -> bytes:  # noqa: ANN401
        # The editor's save lands while the (slow) import runs, after the rows were first read.
        Page.objects.filter(pk=front_page.pk).update(
            body='<p>Ny tekst</p><img src="/public/image/upload/images/gang.jpg">'
        )
        return real_read(legacy_path, found)

    monkeypatch.setattr(legacy_images, "read_legacy_file", read_while_an_editor_saves)

    import_pending_legacy_images()

    front_page.refresh_from_db()
    hallway = CmsImage.objects.get(legacy_path="upload/images/gang.jpg")
    assert front_page.body == f'<p>Ny tekst</p><img src="{hallway.url}">'


@pytest.mark.django_db
def test_a_reference_no_file_can_have_does_not_stop_the_import(old_files: OldFiles, front_page: Page) -> None:
    _refer(front_page, "upload/a%00.jpg", f"upload/{'a' * 260}.jpg", "upload/images/gang.jpg")

    result = import_pending_legacy_images()

    assert result.imported == ["upload/images/gang.jpg"] and old_files.reads == ["upload/images/gang.jpg"]
    assert not LegacyImageFailure.objects.exists()
    assert legacy_state().waiting == 0


@pytest.mark.django_db
def test_one_file_referenced_in_two_forms_is_imported_once(old_files: OldFiles, front_page: Page) -> None:
    front_page.body = (
        '<p style="background:url(/public/image/upload/images/Hall(1).png)">x</p>'
        '<img src="/public/image/upload/images/Hall(1).png">'
    )
    front_page.save()

    result = import_pending_legacy_images()

    assert result.imported == ["upload/images/Hall(1).png"] and old_files.reads == [
        "upload/images/Hall(1).png"
    ]
    hall = CmsImage.objects.get(legacy_path="upload/images/Hall(1).png")
    front_page.refresh_from_db()
    assert front_page.body == f'<p style="background:url({hall.url})">x</p><img src="{hall.url}">'


@pytest.mark.django_db
def test_an_unexpected_error_still_rewrites_what_was_imported_before_it(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cms import services

    _refer(front_page, "upload/images/gang.jpg", "upload/images/Hall(1).png")
    real_store = services.store_prepared_image

    def storage_breaks_on_the_second(*args: Any, **kwargs: Any) -> CmsImage:  # noqa: ANN401
        if CmsImage.objects.exists():
            raise OSError("S3 is down")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(services, "store_prepared_image", storage_breaks_on_the_second)

    result = import_pending_legacy_images()

    hallway = CmsImage.objects.get()
    front_page.refresh_from_db()
    assert (
        f'src="{hallway.url}"' in front_page.body
        and "/public/image/upload/images/Hall(1).png" in front_page.body
    )
    assert result.imported == [hallway.legacy_path] and result.rewritten == 1
    assert result.error == "upload/images/Hall(1).png: OSError: S3 is down" and result.deferred == 1
    assert not LegacyImageFailure.objects.exists()  # tried again next run

    monkeypatch.setattr(services, "store_prepared_image", real_store)
    assert import_pending_legacy_images().imported == ["upload/images/Hall(1).png"]
    front_page.refresh_from_db()
    assert "/public/image/" not in front_page.body


@pytest.mark.django_db
def test_nothing_outside_the_directory_is_read(old_files: OldFiles, front_page: Page) -> None:
    (old_files.root.parent / "secret.jpg").write_bytes(image_bytes())
    (old_files.root / "upload" / "link.jpg").symlink_to(old_files.root.parent / "secret.jpg")
    _refer(front_page, "upload/../../secret.jpg", "upload/link.jpg")

    result = convert_legacy_images()

    assert result.imported == [] and result.missing == ["upload/link.jpg"]
    assert not CmsImage.objects.exists()


@pytest.fixture
def lock_held_elsewhere(db: None) -> Iterator[None]:
    """The import lock, held by another database session (a second worker's run)."""
    if connection.vendor != "postgresql":
        pytest.skip("advisory locks are PostgreSQL's")
    params = connection.settings_dict
    with psycopg.connect(
        dbname=params["NAME"],
        user=params["USER"],
        password=params["PASSWORD"],
        host=params["HOST"],
        port=params["PORT"] or None,
        autocommit=True,
    ) as other:
        other.execute("SELECT pg_advisory_lock(%s)", [IMPORT_LOCK_ID])
        yield


@pytest.mark.django_db
def test_a_run_while_another_import_runs_does_nothing(
    old_files: OldFiles, front_page: Page, lock_held_elsewhere: None
) -> None:
    _refer(front_page, "upload/images/gang.jpg")

    result = import_pending_legacy_images()

    assert result.busy and old_files.reads == []
    assert not CmsImage.objects.exists()
    with pytest.raises(CommandError, match="Another legacy image import is running"):
        call_command("import_legacy_images", stdout=StringIO())


# --------------------------------------------------------------------------------- the command
@pytest.mark.django_db
def test_the_command_imports_and_retries_recorded_failures(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cms.management.commands import import_legacy_images

    queued: list[str] = []
    monkeypatch.setattr(import_legacy_images, "publish_on_commit", queued.append)
    _refer(front_page, "upload/images/gang.jpg", "upload/x.jpg")
    LegacyImageFailure.objects.create(legacy_path="upload/images/gang.jpg", reason="missing")
    out = StringIO()

    call_command("import_legacy_images", stdout=out)

    assert f"Reading from {old_files.root}" in out.getvalue()
    assert "Imported 1 images; rewrote 1 rows." in out.getvalue()
    assert "Missing: upload/x.jpg" in out.getvalue()
    assert sorted(old_files.reads) == ["upload/images/gang.jpg", "upload/x.jpg"]  # the recorded one too
    assert list(LegacyImageFailure.objects.values_list("legacy_path", flat=True)) == ["upload/x.jpg"]
    assert queued == ["save"]


# ---------------------------------------------------------------------------------- overview
@pytest.mark.django_db
def test_the_overview_shows_what_is_waiting_and_what_failed(old_files: OldFiles, front_page: Page) -> None:
    _refer(front_page, "upload/images/gang.jpg", "upload/images/findes-ikke.jpg")
    assert legacy_state().waiting == 2

    import_pending_legacy_images()

    state = legacy_state()
    assert state.waiting == 0
    assert [failure.legacy_path for failure in state.failures] == ["upload/images/findes-ikke.jpg"]


# ------------------------------------------------------------------------- the whole old upload/
ALL = ("upload/images/gang.jpg", "upload/images/Hall(1).png", "upload/images/aaretsgang/nytår 2014-15.jpg")


def test_the_repository_holds_all_77_old_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """What production imports: the files below static/legacy/image/upload/, as git has them."""
    monkeypatch.setattr(legacy_images, "LEGACY_DIRECTORY", Path(settings.BASE_DIR) / "static/legacy/image")
    files = legacy_files()
    listed = sorted(path for path in files if path.startswith("upload/"))

    assert len(listed) == 77
    assert sum(files[path].stat().st_size for path in listed) == 34_165_111
    for path in listed:
        assert legacy_images._normalize(path) == path
        assert Path(path).suffix.lower() in EXTENSION_TYPES
        assert 0 < files[path].stat().st_size <= legacy_images.MAX_FILE_BYTES
    assert "elements/logo.svg" in files or any(path.startswith("elements/") for path in files)


@pytest.mark.django_db
def test_every_file_is_imported_even_when_no_content_uses_it(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    _list_all(monkeypatch)

    result = convert_legacy_images()

    assert sorted(result.imported) == sorted(ALL) and result.rewritten == 0
    assert sorted(old_files.reads) == sorted(ALL)
    mapping = legacy_image_map()
    assert set(mapping) == {f"/static/legacy/image/{path}" for path in ALL}
    # None of them is used, and every one is sent to gahk.dk, with the redirect from its old address.
    snapshot = build_snapshot()
    assert snapshot.data["legacy_images"] == mapping
    assert set(mapping.values()) <= {image["url"] for image in snapshot.data["images"]}
    assert {image.legacy_path for image in snapshot.images.values()} == set(ALL)
    assert all(not usage for usage in usage_map(CmsImage.objects.all()).values())
    state = legacy_state()
    assert (state.total, state.in_library, state.deleted, state.waiting) == (3, 3, 0, 0)

    old_files.reads.clear()
    assert convert_legacy_images().imported == [] and old_files.reads == []


@pytest.mark.django_db
def test_the_content_s_files_come_first_and_the_automatic_runs_keep_their_budget(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    _list_all(monkeypatch)
    _refer(front_page, "upload/images/aaretsgang/nyt%C3%A5r%202014-15.jpg")
    monkeypatch.setattr(legacy_images, "AUTO_MAX_FILES", 2)
    assert legacy_state().waiting == 3

    first = import_pending_legacy_images()

    assert first.imported[0] == "upload/images/aaretsgang/nytår 2014-15.jpg"
    assert (len(first.imported), first.deferred) == (2, 1)
    front_page.refresh_from_db()
    assert "/public/image/" not in front_page.body
    second = import_pending_legacy_images()
    assert (len(second.imported), second.deferred) == (1, 0)
    assert CmsImage.objects.filter(legacy_path__in=ALL).count() == 3


@pytest.mark.django_db
def test_an_unused_old_image_can_be_deleted_and_is_not_imported_again(
    old_files: OldFiles,
    front_page: Page,
    monkeypatch: pytest.MonkeyPatch,
    make_resident: Callable[..., Resident],
) -> None:
    from cms import services

    _list_all(monkeypatch)
    convert_legacy_images()
    hallway = CmsImage.objects.get(legacy_path="upload/images/gang.jpg")
    old_address = "/static/legacy/image/upload/images/gang.jpg"
    assert old_address in build_snapshot().data["legacy_images"]
    queued: list[str] = []
    monkeypatch.setattr(services, "publish_on_commit", lambda reason, by=None: queued.append(reason))

    services.delete_image(hallway, make_resident(email="redaktoer@gahk.dk"))

    assert queued == ["save"]
    marker = LegacyImageFailure.objects.get()
    assert (marker.legacy_path, marker.reason) == ("upload/images/gang.jpg", "deleted")
    snapshot = build_snapshot()
    assert old_address not in snapshot.data["legacy_images"] and hallway.sha256 not in snapshot.images
    old_files.reads.clear()
    for run in (import_pending_legacy_images, convert_legacy_images):  # nor by the command's retry
        assert run().imported == [] and old_files.reads == []
    state = legacy_state()
    assert (state.total, state.in_library, state.deleted, state.waiting, state.failures) == (3, 2, 1, 0, [])

    # Content that refers to it again brings it back.
    _refer(front_page, "upload/images/gang.jpg")
    assert import_pending_legacy_images().imported == ["upload/images/gang.jpg"]
    assert not LegacyImageFailure.objects.exists()


@pytest.mark.django_db
def test_the_command_imports_every_file_in_one_go(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cms.management.commands import import_legacy_images

    queued: list[str] = []
    monkeypatch.setattr(import_legacy_images, "publish_on_commit", queued.append)
    monkeypatch.setattr(legacy_images, "AUTO_MAX_FILES", 1)  # the automatic runs' budget is not its
    _list_all(monkeypatch)
    old_files.put("upload/images/ødelagt.jpg", b"not an image at all")
    out = StringIO()

    call_command("import_legacy_images", stdout=out)

    text = out.getvalue()
    assert "[1/4] upload/images/Hall(1).png: imported" in text
    assert "[3/4] upload/images/gang.jpg: imported" in text
    assert "upload/images/ødelagt.jpg: not a usable image" in text
    assert "Imported 3 images; rewrote 0 rows." in text
    assert "The library has 3 of the 4 old images." in text
    assert "Every old image is in the library." not in text
    assert queued == ["save"]

    # Again: nothing to read but the unusable one, which the command retries.
    old_files.reads.clear()
    out = StringIO()
    call_command("import_legacy_images", stdout=out)
    assert old_files.reads == ["upload/images/ødelagt.jpg"]
    assert "Imported 0 images" in out.getvalue() and queued == ["save"]


@pytest.mark.django_db
def test_the_command_says_when_everything_is_in(
    old_files: OldFiles, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cms.management.commands import import_legacy_images

    monkeypatch.setattr(import_legacy_images, "publish_on_commit", lambda reason: None)
    _list_all(monkeypatch)
    out = StringIO()

    call_command("import_legacy_images", stdout=out)

    assert "The library has 3 of the 3 old images." in out.getvalue()
    assert "Every old image is in the library." in out.getvalue()


@pytest.mark.django_db
def test_the_overview_counts_against_every_old_file(
    old_files: OldFiles,
    front_page: Page,
    monkeypatch: pytest.MonkeyPatch,
    make_resident: Callable[..., Resident],
) -> None:
    from django.test import Client

    _list_all(monkeypatch)
    monkeypatch.setattr(legacy_images, "AUTO_MAX_FILES", 1)
    import_pending_legacy_images()
    editor = make_resident(email="redaktoer@gahk.dk", roles=("pr",))
    client = Client()
    client.force_login(editor)

    html = client.get(reverse("cms:overview")).content.decode()

    assert "1 af 3 gamle billeder er i billedbiblioteket." in html
    assert "2 billeder flyttes ind i billedbiblioteket af sig selv" in html
