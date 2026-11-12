"""The old site's images moving into the library by themselves (cms.legacy_images), downloaded from
the pinned commit on GitHub — here from a fake GitHub, because tests never use the network."""

import json
import threading
from collections.abc import Iterator
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import psycopg
import pytest
import requests
from django.core.management import CommandError, call_command
from django.db import connection
from PIL import Image

from cms import legacy_images
from cms.legacy_images import (
    IMPORT_LOCK_ID,
    LEGACY_SOURCE_URL,
    DirectorySource,
    convert_legacy_images,
    import_pending_legacy_images,
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
from cms.snapshot import build_snapshot


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


def fake_response(status: int, body: bytes, content_type: str, length: bool = True) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    if length:
        response.headers["Content-Length"] = str(len(body))
    response.raw = BytesIO(body)
    return response


TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}


class FakeGitHub:
    """raw.githubusercontent.com for the pinned commit: `files` maps a legacy path to its bytes
    (served with the content type of its extension) or to a ready response."""

    def __init__(self, files: dict[str, bytes | requests.Response] | None = None) -> None:
        self.files = files or {}
        self.urls: list[str] = []
        self.down = False
        self.lock = threading.Lock()

    def __call__(self, url: str, **kwargs: Any) -> requests.Response:  # noqa: ANN401
        assert kwargs["stream"] is True and kwargs["timeout"]
        with self.lock:
            self.urls.append(url)
        if self.down:
            raise requests.ConnectionError("network unreachable")
        assert url.startswith(LEGACY_SOURCE_URL), url
        path = unquote(url.removeprefix(LEGACY_SOURCE_URL))
        entry = self.files.get(path)
        if entry is None:
            return fake_response(404, b"404: Not Found", "text/plain; charset=utf-8")
        if isinstance(entry, requests.Response):
            return entry
        return fake_response(200, entry, TYPES.get(Path(path).suffix.lower(), "application/octet-stream"))

    def paths(self) -> list[str]:
        return [unquote(url.removeprefix(LEGACY_SOURCE_URL)) for url in self.urls]


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub(
        {
            "upload/images/gang.jpg": image_bytes(size=(3000, 1000)),
            "upload/images/Hall(1).png": image_bytes("PNG"),
            "upload/images/aaretsgang/nytår 2014-15.jpg": image_bytes(),
        }
    )
    monkeypatch.setattr(legacy_images, "http_get", fake)
    return fake


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
@pytest.mark.django_db
def test_referenced_images_are_downloaded_from_the_pinned_commit_and_the_content_rewritten(
    github: FakeGitHub, front_page: Page
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

    # Each file once, quoted, from the pinned commit; the svg is never asked for (not an image name),
    # nor "Hall(1).png)" with the ")" of the unquoted url().
    assert sorted(github.urls) == sorted(
        LEGACY_SOURCE_URL + path
        for path in (
            "upload/images/gang.jpg",
            "upload/images/aaretsgang/nyt%C3%A5r%202014-15.jpg",
            "upload/images/Hall%281%29.png",
            "upload/images/findes-ikke.jpg",
        )
    )
    assert LEGACY_SOURCE_URL.startswith(
        "https://raw.githubusercontent.com/GAHK-org/gahk_intern/3fc1817b0d5ef07a2b636b4a01c892ba3fe3a8ba/"
    )
    assert sorted(result.imported) == [
        "upload/images/Hall(1).png",
        "upload/images/aaretsgang/nytår 2014-15.jpg",
        "upload/images/gang.jpg",
    ]
    assert result.missing == ["upload/images/findes-ikke.jpg"] and result.unusable == ["elements/logo.svg"]
    gang = CmsImage.objects.get(legacy_path="upload/images/gang.jpg")
    assert gang.uploaded_by is None and gang.caption == "gang" and gang.sha256
    with gang.file.open("rb") as handle:
        assert max(Image.open(handle).size) == 2400  # downscaled like an upload
    nytaar = CmsImage.objects.get(legacy_path="upload/images/aaretsgang/nytår 2014-15.jpg")
    assert nytaar.url.isascii()  # stored under an ASCII name
    front_page.refresh_from_db()
    assert front_page.background_image == gang.url
    assert f'src="{gang.url}"' in front_page.body and f'src="{nytaar.url}"' in front_page.body
    assert "url(/media/cms/" in front_page.body and "Hall(1)" not in front_page.body
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

    # The next run: everything is in the library or recorded, so it asks GitHub nothing.
    github.urls.clear()
    again = import_pending_legacy_images()
    assert github.urls == []
    assert again.imported == [] and again.rewritten == 0
    assert sorted(again.skipped) == ["elements/logo.svg", "upload/images/findes-ikke.jpg"]
    assert CmsImage.objects.filter(legacy_path__isnull=False).count() == 3


@pytest.mark.django_db
def test_nothing_referenced_means_no_request_and_no_change(github: FakeGitHub, front_page: Page) -> None:
    result = import_pending_legacy_images()

    assert result.summary() == {
        "imported": 0,
        "rewritten": 0,
        "missing": 0,
        "unusable": 0,
        "deferred": 0,
        "unavailable": "",
        "error": "",
        "busy": False,
    }
    assert github.urls == []
    assert not ContentChange.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (fake_response(404, b"404: Not Found", "text/plain"), "missing"),
        (fake_response(200, b"<html>login</html>", "text/html; charset=utf-8"), "unusable"),
        (fake_response(200, b"not an image at all", "image/jpeg"), "unusable"),
        (fake_response(200, image_bytes("BMP"), "image/jpeg"), "unusable"),  # decodes, but not a web format
        (fake_response(200, image_bytes()[:200], "image/jpeg"), "unusable"),  # cut off
    ],
)
def test_a_file_that_cannot_be_used_is_recorded_and_not_asked_for_again(
    github: FakeGitHub, front_page: Page, response: requests.Response, reason: str
) -> None:
    github.files["upload/images/bad.jpg"] = response
    _refer(front_page, "upload/images/bad.jpg")

    first = import_pending_legacy_images()

    failure = LegacyImageFailure.objects.get()
    assert (failure.legacy_path, failure.reason) == ("upload/images/bad.jpg", reason)
    assert (first.missing if reason == "missing" else first.unusable) == ["upload/images/bad.jpg"]
    assert not CmsImage.objects.exists()
    front_page.refresh_from_db()
    assert "/public/image/upload/images/bad.jpg" in front_page.body

    github.urls.clear()
    for _run in range(3):
        assert import_pending_legacy_images().skipped == ["upload/images/bad.jpg"]
    assert github.urls == []


@pytest.mark.django_db
def test_a_file_over_the_size_cap_is_refused_without_reading_it_all(
    github: FakeGitHub, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(legacy_images, "MAX_FILE_BYTES", 1000)
    big = image_bytes(size=(400, 400))
    assert len(big) > 1000
    github.files["upload/images/stor.jpg"] = fake_response(200, big, "image/jpeg")
    github.files["upload/images/stor-uden-laengde.jpg"] = fake_response(200, big, "image/jpeg", length=False)
    _refer(front_page, "upload/images/stor.jpg", "upload/images/stor-uden-laengde.jpg")

    result = import_pending_legacy_images()

    assert sorted(result.unusable) == ["upload/images/stor-uden-laengde.jpg", "upload/images/stor.jpg"]
    assert set(LegacyImageFailure.objects.values_list("detail", flat=True)) == {
        f"{len(big)} bytes, more than 1000",
        "more than 1000 bytes",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("trouble", ["down", "503", "429"])
def test_an_unreachable_github_is_recorded_nowhere_and_tried_again_next_run(
    github: FakeGitHub, front_page: Page, trouble: str
) -> None:
    _refer(front_page, "upload/images/gang.jpg", "upload/images/Hall(1).png")
    if trouble == "down":
        github.down = True
    else:
        for path in ("upload/images/gang.jpg", "upload/images/Hall(1).png"):
            github.files[path] = fake_response(int(trouble), b"busy", "text/plain")

    result = import_pending_legacy_images()

    assert len(github.urls) == 1  # stops at the first: the rest would fail the same way
    assert result.unavailable and result.deferred == 2 and result.imported == []
    assert not LegacyImageFailure.objects.exists()

    github.down = False
    github.files["upload/images/gang.jpg"] = image_bytes()
    github.files["upload/images/Hall(1).png"] = image_bytes("PNG")
    assert sorted(import_pending_legacy_images().imported) == [
        "upload/images/Hall(1).png",
        "upload/images/gang.jpg",
    ]


@pytest.mark.django_db
def test_a_run_stops_at_its_budget_and_the_next_one_goes_on(
    github: FakeGitHub, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [f"upload/images/billede{number}.jpg" for number in range(5)]
    github.files.update({path: image_bytes() for path in paths})
    _refer(front_page, *paths)
    monkeypatch.setattr(legacy_images, "AUTO_MAX_FILES", 2)

    first = import_pending_legacy_images()
    assert (len(first.imported), first.deferred, len(github.urls)) == (2, 3, 2)
    front_page.refresh_from_db()
    assert front_page.body.count("/public/image/") == 3  # what was imported is already rewritten

    second = import_pending_legacy_images()
    third = import_pending_legacy_images()
    assert (len(second.imported), len(third.imported), third.deferred) == (2, 1, 0)
    front_page.refresh_from_db()
    assert "/public/image/" not in front_page.body
    assert sorted(github.paths()) == sorted(paths)  # each file asked for once


@pytest.mark.django_db
def test_a_run_out_of_time_downloads_nothing_more(
    github: FakeGitHub, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refer(front_page, "upload/images/gang.jpg")
    monkeypatch.setattr(legacy_images, "AUTO_SECONDS", 0)

    result = import_pending_legacy_images()

    assert (result.deferred, github.urls) == (1, [])
    assert not LegacyImageFailure.objects.exists()


@pytest.mark.django_db
def test_an_edit_saved_during_the_import_is_kept(
    github: FakeGitHub, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refer(front_page, "upload/images/gang.jpg")
    real_get = legacy_images.http_get

    def download_while_an_editor_saves(url: str, **kwargs: Any) -> requests.Response:  # noqa: ANN401
        # The editor's save lands while the (slow) download runs, after the rows were first read.
        Page.objects.filter(pk=front_page.pk).update(
            body='<p>Ny tekst</p><img src="/public/image/upload/images/gang.jpg">'
        )
        return real_get(url, **kwargs)

    monkeypatch.setattr(legacy_images, "http_get", download_while_an_editor_saves)

    import_pending_legacy_images()

    front_page.refresh_from_db()
    gang = CmsImage.objects.get(legacy_path="upload/images/gang.jpg")
    assert front_page.body == f'<p>Ny tekst</p><img src="{gang.url}">'


@pytest.mark.django_db
def test_a_reference_no_file_can_have_does_not_stop_the_import(github: FakeGitHub, front_page: Page) -> None:
    _refer(front_page, "upload/a%00.jpg", f"upload/{'a' * 260}.jpg", "upload/images/gang.jpg")

    result = import_pending_legacy_images()

    assert result.imported == ["upload/images/gang.jpg"] and github.paths() == ["upload/images/gang.jpg"]
    assert not LegacyImageFailure.objects.exists()
    assert legacy_state().waiting == 0


@pytest.mark.django_db
def test_one_file_referenced_in_two_forms_is_imported_once(github: FakeGitHub, front_page: Page) -> None:
    front_page.body = (
        '<p style="background:url(/public/image/upload/images/Hall(1).png)">x</p>'
        '<img src="/public/image/upload/images/Hall(1).png">'
    )
    front_page.save()

    result = import_pending_legacy_images()

    assert result.imported == ["upload/images/Hall(1).png"] and github.paths() == [
        "upload/images/Hall(1).png"
    ]
    hall = CmsImage.objects.get(legacy_path="upload/images/Hall(1).png")
    front_page.refresh_from_db()
    assert front_page.body == f'<p style="background:url({hall.url})">x</p><img src="{hall.url}">'


@pytest.mark.django_db
def test_an_unexpected_error_still_rewrites_what_was_imported_before_it(
    github: FakeGitHub, front_page: Page, monkeypatch: pytest.MonkeyPatch
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

    gang = CmsImage.objects.get()
    front_page.refresh_from_db()
    assert (
        f'src="{gang.url}"' in front_page.body
        and "/public/image/upload/images/Hall(1).png" in front_page.body
    )
    assert result.imported == [gang.legacy_path] and result.rewritten == 1
    assert result.error == "upload/images/Hall(1).png: OSError: S3 is down" and result.deferred == 1
    assert not LegacyImageFailure.objects.exists()  # tried again next run

    monkeypatch.setattr(services, "store_prepared_image", real_store)
    assert import_pending_legacy_images().imported == ["upload/images/Hall(1).png"]
    front_page.refresh_from_db()
    assert "/public/image/" not in front_page.body


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
    github: FakeGitHub, front_page: Page, lock_held_elsewhere: None
) -> None:
    _refer(front_page, "upload/images/gang.jpg")

    result = import_pending_legacy_images()

    assert result.busy and github.urls == []
    assert not CmsImage.objects.exists()
    with pytest.raises(CommandError, match="Another legacy image import is running"):
        call_command("import_legacy_images", stdout=StringIO())


# --------------------------------------------------------------------------------- the command
@pytest.mark.django_db
def test_the_command_downloads_from_the_pinned_commit_and_retries_recorded_failures(
    github: FakeGitHub, front_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cms.management.commands import import_legacy_images

    queued: list[str] = []
    monkeypatch.setattr(import_legacy_images, "publish_on_commit", queued.append)
    _refer(front_page, "upload/images/gang.jpg", "upload/x.jpg")
    LegacyImageFailure.objects.create(legacy_path="upload/images/gang.jpg", reason="missing")
    out = StringIO()

    call_command("import_legacy_images", stdout=out)

    assert f"Reading from {LEGACY_SOURCE_URL}" in out.getvalue()
    assert "Imported 1 images; rewrote 1 rows." in out.getvalue()
    assert "Missing: upload/x.jpg" in out.getvalue()
    assert sorted(github.paths()) == ["upload/images/gang.jpg", "upload/x.jpg"]  # the recorded one too
    assert list(LegacyImageFailure.objects.values_list("legacy_path", flat=True)) == ["upload/x.jpg"]
    assert queued == ["save"]


@pytest.mark.django_db
def test_the_command_says_when_github_cannot_be_reached(github: FakeGitHub, front_page: Page) -> None:
    _refer(front_page, "upload/images/gang.jpg")
    github.down = True
    with pytest.raises(CommandError, match="Stopped with 1 images left"):
        call_command("import_legacy_images", stdout=StringIO())


@pytest.fixture
def legacy_dir(tmp_path: Path) -> Path:
    """A local copy of the old site's static/legacy/image/."""
    root = tmp_path / "legacy-image"
    (root / "upload" / "images").mkdir(parents=True)
    (root / "upload" / "images" / "gang.jpg").write_bytes(image_bytes())
    (root / "elements").mkdir()
    (root / "elements" / "logo.svg").write_text("<svg/>")
    return root


@pytest.mark.django_db
def test_the_command_can_read_a_local_copy_instead(
    github: FakeGitHub, front_page: Page, legacy_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cms.management.commands import import_legacy_images

    monkeypatch.setattr(import_legacy_images, "publish_on_commit", lambda reason: None)
    _refer(front_page, "upload/images/gang.jpg", "elements/logo.svg")
    out = StringIO()

    # A directory without the file is passed over; the next one has it.
    call_command(
        "import_legacy_images", "--from", str(legacy_dir.parent), "--from", str(legacy_dir), stdout=out
    )

    assert "Imported 1 images; rewrote 1 rows." in out.getvalue()
    assert "Not a usable image: elements/logo.svg" in out.getvalue()
    assert github.urls == []
    with pytest.raises(CommandError):
        call_command("import_legacy_images", "--from", str(legacy_dir / "nowhere"))


@pytest.mark.django_db
def test_a_directory_source_never_reads_outside_its_directory(legacy_dir: Path, front_page: Page) -> None:
    (legacy_dir.parent / "secret.jpg").write_bytes(image_bytes())
    _refer(front_page, "upload/../../secret.jpg")

    result = convert_legacy_images(DirectorySource([legacy_dir]))

    assert result.imported == [] and not CmsImage.objects.exists()


# ---------------------------------------------------------------------------------- overview
@pytest.mark.django_db
def test_the_overview_shows_what_is_waiting_and_what_failed(github: FakeGitHub, front_page: Page) -> None:
    _refer(front_page, "upload/images/gang.jpg", "upload/images/findes-ikke.jpg")
    assert legacy_state().waiting == 2

    import_pending_legacy_images()

    state = legacy_state()
    assert state.waiting == 0
    assert [failure.legacy_path for failure in state.failures] == ["upload/images/findes-ikke.jpg"]
