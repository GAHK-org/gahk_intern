"""GAHKFlix (gahkflix/): uploads and encoding, the time windows and the two-person rule, watching,
browsing and the pages. The encode tests run the real ffmpeg on a one-second clip."""

import hashlib
import json
import os
import secrets
import subprocess
from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import imageio_ffmpeg
import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from gahkflix import access, encoding, services, tasks
from gahkflix.models import (
    CastMember,
    ChangeRequest,
    FileState,
    Series,
    Video,
    VideoFile,
    VideoFrame,
    original_key,
)
from gahkflix.storage import get_video_storage
from notifications import categories
from notifications.models import Notification
from residents.models import Resident, Role

from .media_samples import jpeg_bytes, mp4_bytes

if TYPE_CHECKING:
    from PIL import Image

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:  # noqa: ANN401
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.MEDIA_CACHE_DIR = ""


@pytest.fixture
def admin(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,), first_name="Ada")


@pytest.fixture
def admin2(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="admin2@gahk.dk", roles=(Role.ADMINISTRATOR,), first_name="Bo")


@pytest.fixture
def admin3(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="admin3@gahk.dk", roles=(Role.FOTO,), first_name="Cille")


@pytest.fixture
def member(make_resident: Callable[..., Resident]) -> Resident:
    """Fotogruppen: inside the rollout gate, and an "administrator" in GAHKFlix's sense."""
    return make_resident(email="foto@gahk.dk", roles=(Role.FOTO,), first_name="Dorte")


@pytest.fixture
def plain(make_resident: Callable[..., Resident]) -> Resident:
    return make_resident(email="beboer@gahk.dk", first_name="Erik", last_name="Hansen")


@pytest.fixture
def open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "ACCESS_ROLES", None)


def client_for(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def make_video(
    owner: Resident,
    *,
    title: str = "Revy 2026",
    published: bool = True,
    days_old: int = 0,
    duration: float = 600.0,
    **fields: Any,  # noqa: ANN401
) -> Video:
    """A processed video with stand-in files, without running ffmpeg."""
    video = Video.objects.create(title=title, uploaded_by=owner, **fields)
    video_file = VideoFile(
        video=video,
        uploaded_by=owner,
        original_name="revy.mp4",
        size=3,
        fingerprint=secrets.token_hex(32),
        sha256=secrets.token_hex(32),
        state=FileState.READY,
        duration=duration,
        width=1280,
        height=720,
    )
    video_file.original.name = get_video_storage().save(
        original_key(video.uuid, video_file.uuid, "revy.mp4"), ContentFile(b"mp4")
    )
    video_file.hd.save("revy-hd.mp4", ContentFile(b"hd"), save=False)
    video_file.save()
    video.file = video_file
    video.thumbnail.save("thumbnail.jpg", ContentFile(jpeg_bytes()), save=False)
    video.banner.save("banner.jpg", ContentFile(jpeg_bytes()), save=False)
    if published:
        video.published_at = timezone.now() - timedelta(days=days_old)
    video.save()
    if days_old:
        Video.objects.filter(pk=video.pk).update(created_at=timezone.now() - timedelta(days=days_old))
        video.refresh_from_db()
    return video


def other_clip() -> bytes:
    """A second real clip, different bytes from mp4_bytes()."""
    with TemporaryDirectory() as directory:
        target = Path(directory) / "other.mp4"
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x240:rate=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        return target.read_bytes()


def upload(
    client: Client,
    content: bytes,
    *,
    name: str = "revy.mp4",
    video: Video | None = None,
    fingerprint: str | None = None,
    declared_size: int | None = None,
) -> Any:  # noqa: ANN401 — the commit response
    begin = client.post(
        reverse("gahkflix:upload_begin"),
        {
            "name": name,
            "size": declared_size or len(content),
            "type": "video/mp4",
            "fingerprint": fingerprint or hashlib.sha256(content).hexdigest(),
            "lastModified": "1600000000000",
            "video": video.pk if video else None,
        },
        content_type="application/json",
    )
    if begin.status_code != 200:
        return begin
    token = begin.json()["token"]
    assert begin.json()["upload"]["mode"] == "direct"
    sent = client.post(
        reverse("gahkflix:upload_direct"),
        {"token": token, "file": SimpleUploadedFile(name, content, "video/mp4")},
    )
    assert sent.status_code == 200
    return client.post(reverse("gahkflix:upload_commit"), {"token": token}, content_type="application/json")


def notified(resident: Resident, category: str) -> list[Notification]:
    return list(Notification.objects.filter(recipient=resident, category=category))


def details(video: Video, **changes: Any) -> dict[str, str]:  # noqa: ANN401
    """The edit form's POST for `video`, with `changes` on top."""
    current = services.snapshot(video)
    current.update(changes)
    return {
        "title": current["title"],
        "description": current["description"],
        "recorded_on": current["recorded_on"] or "",
        "series": str(current["series"] or ""),
        "season": str(current["season"] or ""),
        "episode": str(current["episode"] or ""),
        "cast": json.dumps(current["cast"]),
        "tags": json.dumps(current["tags"]),
    }


# --- The gate -----------------------------------------------------------------------------------


def test_gate_admits_administrators_and_fotogruppen_only(
    admin: Resident, member: Resident, plain: Resident
) -> None:
    assert client_for(admin).get(reverse("gahkflix:home")).status_code == 200
    assert client_for(member).get(reverse("gahkflix:home")).status_code == 200
    assert client_for(plain).get(reverse("gahkflix:home")).status_code == 403


def test_sidebar_entry_follows_the_gate(admin: Resident, plain: Resident) -> None:
    def urls(resident: Resident) -> list[str]:
        nav = client_for(resident).get("/intern/").context["nav_intern"]
        return [url for _section, items in nav for url, _label, _icon in items]

    assert "/intern/gahkflix/" in urls(admin)
    assert "/intern/gahkflix/" not in urls(plain)


def test_opening_the_gate_lets_every_resident_in(open_gate: None, plain: Resident) -> None:
    assert client_for(plain).get(reverse("gahkflix:home")).status_code == 200


def test_every_resident_is_let_in_with_debug_on(settings: Any, plain: Resident, admin: Resident) -> None:  # noqa: ANN401
    """So a non-administrator's GAHKFlix can be tried locally while it is still under test."""
    settings.DEBUG = True
    client = client_for(plain)
    assert client.get(reverse("gahkflix:home")).status_code == 200
    assert "/intern/gahkflix/" in [
        url for _s, items in client.get("/intern/").context["nav_intern"] for url, *_ in items
    ]
    # Inside, a resident is still a resident: none of the administrators' pages.
    assert client.get(reverse("gahkflix:drafts")).status_code == 403
    assert plain in access.audience(Resident.objects.all())


# --- Uploading and processing -------------------------------------------------------------------


def test_upload_is_encoded_and_offers_frames(
    admin: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    client = client_for(admin)
    with django_capture_on_commit_callbacks(execute=True):
        response = upload(client, mp4_bytes())
    assert response.status_code == 201
    video = Video.objects.get()
    assert response.json()["editUrl"] == reverse("gahkflix:edit", args=[video.pk])
    assert video.is_draft
    video_file = video.files.get()
    assert video_file.state == FileState.READY, video_file.error
    assert video_file.sha256 == hashlib.sha256(mp4_bytes()).hexdigest()
    assert video.file == video_file
    assert video_file.hd and not video_file.full_hd  # a 320x240 source is never upscaled
    assert video_file.duration == pytest.approx(1.0, abs=0.2)
    # The container says 2021-06-05; the browser's lastModified (2020) is only the fallback.
    assert video.recorded_on == date(2021, 6, 5)
    assert 1 <= video.frames.count() <= 10
    assert notified(admin, categories.GAHKFLIX_UPLOADS)


def test_a_draft_is_published_once_it_has_pictures(
    admin: Resident, admin2: Resident, plain: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    client = client_for(admin)
    with django_capture_on_commit_callbacks(execute=True):
        upload(client, mp4_bytes())
    video = Video.objects.get()
    response = client.post(reverse("gahkflix:publish", args=[video.pk]))
    video.refresh_from_db()
    assert video.is_draft  # no thumbnail or banner yet
    frame = video.frames.first()
    assert frame is not None
    for kind in ("thumbnail", "banner"):
        response = client.post(
            reverse("gahkflix:choose_picture", args=[video.pk]), {"kind": kind, "frame": frame.pk}
        )
        assert response.status_code == 302
    video.refresh_from_db()
    assert video.thumbnail_frame == frame and video.banner_frame == frame
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("gahkflix:publish", args=[video.pk]))
    video.refresh_from_db()
    assert video.is_published
    # Everyone who can open GAHKFlix hears of it; the gate keeps it from a plain resident for now.
    assert notified(admin2, categories.GAHKFLIX_NEW)
    assert not notified(plain, categories.GAHKFLIX_NEW)
    assert not notified(admin, categories.GAHKFLIX_NEW)


def test_custom_thumbnail_becomes_16_by_9(admin: Resident) -> None:
    video = make_video(admin, published=False)
    response = client_for(admin).post(
        reverse("gahkflix:choose_picture", args=[video.pk]),
        {
            "kind": "thumbnail",
            "picture": SimpleUploadedFile("mig.jpg", jpeg_bytes(size=(800, 600)), "image/jpeg"),
        },
    )
    assert response.status_code == 302
    video.refresh_from_db()
    from PIL import Image

    with video.thumbnail.open("rb") as stored, Image.open(stored) as image:
        assert image.size == (640, 360)  # 4:3, so cropped
    assert video.thumbnail_frame is None


def test_the_upload_page_shows_your_unpublished_videos(admin: Resident, member: Resident) -> None:
    make_video(admin, title="Min halvfærdige revy", published=False)
    make_video(admin, title="Allerede ude", days_old=30)
    make_video(member, title="Andens kladde", published=False)
    page = client_for(admin).get(reverse("gahkflix:upload")).content.decode()
    assert "Dine videoer, der kan få en ny version" in page
    assert "Min halvfærdige revy" in page
    assert "Allerede ude" not in page and "Andens kladde" not in page


def test_corrections_are_pointed_to_a_new_version(admin: Resident) -> None:
    recent = make_video(admin, title="Revy, lige udgivet", days_old=2)
    make_video(admin, title="Revy fra sidste måned", days_old=30)
    page = client_for(admin).get(reverse("gahkflix:upload")).content.decode()
    assert "Skal en video rettes?" in page
    assert "Revy, lige udgivet" in page and f"{reverse('gahkflix:edit', args=[recent.pk])}#ny-version" in page
    assert "Revy fra sidste måned" not in page  # past its seven days: no new version any more

    edit = client_for(admin).get(reverse("gahkflix:edit", args=[recent.pk])).content.decode()
    assert 'href="#ny-version"' in edit and 'id="ny-version"' in edit
    assert "Upload den rettede fil her" in edit


def test_after_seven_days_the_edit_page_says_why_there_is_no_new_version(admin: Resident) -> None:
    old = make_video(admin, days_old=10)
    edit = client_for(admin).get(reverse("gahkflix:edit", args=[old.pk])).content.decode()
    assert "kan kun skiftes de første 7 dage" in edit
    assert 'href="#ny-version"' not in edit and "data-flix-upload" not in edit


def test_pick_a_moment_yourself(
    admin: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    client = client_for(admin)
    with django_capture_on_commit_callbacks(execute=True):
        upload(client, mp4_bytes())
    video = Video.objects.get()
    response = client.post(
        reverse("gahkflix:choose_picture", args=[video.pk]), {"kind": "banner", "at": "0.5"}
    )
    assert response.status_code == 302
    video.refresh_from_db()
    assert video.banner and video.banner_frame is None
    before = video.thumbnail.name
    client.post(reverse("gahkflix:choose_picture", args=[video.pk]), {"kind": "thumbnail", "at": "99"})
    video.refresh_from_db()
    assert video.thumbnail.name == before  # outside the video: refused, nothing changed
    _dirs, pictures = get_video_storage().listdir(f"gahkflix/{video.uuid}/pictures")
    assert len(pictures) == 1  # the banner; nothing stored for the refused one


def test_ffmpeg_reads_stored_files_over_the_loopback() -> None:
    """The static ffmpeg cannot resolve host names, so it gets the file from this process, in ranges."""
    import urllib.error
    import urllib.request

    from gahkflix.storage import loopback_url

    key = get_video_storage().save("gahkflix/loopback/test.bin", ContentFile(bytes(range(256)) * 4))
    with loopback_url(key) as url:
        assert url.startswith("http://127.0.0.1:")
        request = urllib.request.Request(url, headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(request) as response:
            assert response.status == 206 and response.headers["Content-Range"] == "bytes 10-19/1024"
            assert response.read() == bytes(range(10, 20))
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"Range": "bytes=1020-"})
        ) as response:
            assert response.read() == bytes(range(252, 256))
        with urllib.request.urlopen(url) as response:
            assert response.status == 200 and len(response.read()) == 1024
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=5000-"}))
        assert refused.value.code == 416


def test_season_and_episode_fields(admin: Resident, member: Resident) -> None:
    series = Series.objects.create(title="Gangen", created_by=admin)
    make_video(admin, title="Pilot", series=series, season=1, episode=1)
    make_video(member, title="Hemmelig", series=series, season=1, episode=2, published=False)
    video = make_video(admin, title="Nyt afsnit", published=False)
    response = client_for(admin).get(reverse("gahkflix:edit", args=[video.pk]))
    page = response.content.decode()
    assert 'inputmode="numeric"' in page and 'data-flix-number="episode"' in page
    numbers = response.context["episode_numbers"][str(series.pk)]
    # An administrator sees every title, drafts included; the video being edited is left out.
    assert sorted((n["episode"], n["title"]) for n in numbers) == [(1, "Pilot"), (2, "Hemmelig")]


def test_season_and_episode_titles_of_drafts_you_cannot_see(
    open_gate: None, admin: Resident, plain: Resident
) -> None:
    series = Series.objects.create(title="Gangen", created_by=admin)
    make_video(admin, title="Hemmelig", series=series, season=1, episode=2, published=False)
    own = make_video(plain, title="Mit afsnit", published=False)
    numbers = client_for(plain).get(reverse("gahkflix:edit", args=[own.pk])).context["episode_numbers"]
    assert numbers[str(series.pk)] == [{"season": 1, "episode": 2, "title": "Kladde"}]
    response = client_for(plain).post(
        reverse("gahkflix:edit", args=[own.pk]), details(own, series=series.pk, season="1", episode="x")
    )
    assert response.status_code == 200 and not Video.objects.get(pk=own.pk).series


def test_one_upload_at_a_time(admin: Resident) -> None:
    video = make_video(admin, published=False)
    VideoFile.objects.filter(video=video).update(state=FileState.PROCESSING)
    response = upload(client_for(admin), other_clip())
    assert response.status_code == 409
    assert "behandles" in response.json()["error"]


def test_the_same_file_is_refused_before_it_is_sent(admin: Resident, member: Resident) -> None:
    existing = make_video(admin, title="Julerevy")
    existing_file = existing.file
    assert existing_file is not None
    response = upload(client_for(member), b"x" * existing_file.size, fingerprint=existing_file.fingerprint)
    assert response.status_code == 409
    assert "Julerevy" in response.json()["error"]


def test_a_duplicate_the_fingerprint_missed_is_caught_by_the_worker(
    admin: Resident, member: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        upload(client_for(admin), mp4_bytes())
    with django_capture_on_commit_callbacks(execute=True):
        upload(client_for(member), mp4_bytes(), name="kopi.mp4", fingerprint="f" * 64)
    copy = VideoFile.objects.get(original_name="kopi.mp4")
    assert copy.state == FileState.FAILED
    assert "findes allerede" in copy.error
    assert not get_video_storage().exists(copy.original.name)


@pytest.mark.parametrize(
    ("name", "size", "message"),
    [("revy.pdf", 10, "Filtypen"), ("revy.mp4", 0, "tom"), ("revy.mp4", 10 * 1024**3, "for stor")],
)
def test_begin_checks_the_file(admin: Resident, name: str, size: int, message: str) -> None:
    response = client_for(admin).post(
        reverse("gahkflix:upload_begin"),
        {"name": name, "size": size, "type": "", "fingerprint": "a" * 64},
        content_type="application/json",
    )
    assert response.status_code == 400
    assert message in response.json()["error"]


def test_commit_refuses_a_file_that_did_not_arrive_whole(admin: Resident) -> None:
    response = upload(client_for(admin), b"short", declared_size=1000)
    assert response.status_code == 409
    assert not Video.objects.exists()
    assert not list((Path(get_video_storage().location) / "gahkflix").rglob("*.mp4"))  # type: ignore[attr-defined]


def test_commit_refuses_somebody_elses_token(admin: Resident, member: Resident) -> None:
    begin = client_for(admin).post(
        reverse("gahkflix:upload_begin"),
        {"name": "revy.mp4", "size": 5, "type": "video/mp4", "fingerprint": "b" * 64},
        content_type="application/json",
    )
    response = client_for(member).post(
        reverse("gahkflix:upload_commit"), {"token": begin.json()["token"]}, content_type="application/json"
    )
    assert response.status_code == 409


def test_a_file_without_video_fails_and_says_so(
    admin: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        upload(client_for(admin), b"not a video at all" * 100)
    video_file = VideoFile.objects.get()
    assert video_file.state == FileState.FAILED
    assert video_file.video.file is None
    assert notified(admin, categories.GAHKFLIX_UPLOADS)


def test_a_new_version_replaces_the_file_and_keeps_the_rest(
    admin: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    client = client_for(admin)
    with django_capture_on_commit_callbacks(execute=True):
        upload(client, mp4_bytes())
    video = Video.objects.get()
    video.published_at = timezone.now()
    video.save()
    first = video.file
    assert first is not None
    old_keys = [first.original.name, first.hd.name]
    with django_capture_on_commit_callbacks(execute=True):
        response = upload(client, other_clip(), name="revy-v2.mp4", video=video)
    assert response.status_code == 201
    video.refresh_from_db()
    assert video.file is not None and video.file.original_name == "revy-v2.mp4"
    assert video.is_published
    assert not VideoFile.objects.filter(pk=first.pk).exists()
    assert not any(get_video_storage().exists(key) for key in old_keys)
    assert Video.objects.count() == 1


def test_no_new_version_after_seven_days(admin: Resident) -> None:
    video = make_video(admin, days_old=8)
    response = client_for(admin).post(
        reverse("gahkflix:upload_begin"),
        {"name": "v2.mp4", "size": 5, "type": "video/mp4", "fingerprint": "c" * 64, "video": video.pk},
        content_type="application/json",
    )
    assert response.status_code == 403


# --- Who sees what ------------------------------------------------------------------------------


def test_a_draft_is_seen_only_by_its_uploader_and_administrators(
    open_gate: None, plain: Resident, admin: Resident, make_resident: Callable[..., Resident]
) -> None:
    draft = make_video(plain, published=False)
    other = make_resident(email="anden@gahk.dk")
    assert client_for(plain).get(reverse("gahkflix:detail", args=[draft.pk])).status_code == 200
    assert client_for(admin).get(reverse("gahkflix:detail", args=[draft.pk])).status_code == 200
    assert client_for(other).get(reverse("gahkflix:detail", args=[draft.pk])).status_code == 404
    assert client_for(other).get(reverse("gahkflix:picture", args=[draft.pk, "thumbnail"])).status_code == 404


def test_files_are_served_after_the_access_check(admin: Resident) -> None:
    video = make_video(admin)
    client = client_for(admin)
    stream = client.get(reverse("gahkflix:stream", args=[video.pk, "hd"]))
    assert stream.status_code == 200 and stream["Content-Type"] == "video/mp4"
    assert client.get(reverse("gahkflix:stream", args=[video.pk, "fullhd"])).status_code == 404
    assert client.get(reverse("gahkflix:picture", args=[video.pk, "banner"])).status_code == 200


def test_download_in_hd_full_hd_or_the_original(admin: Resident) -> None:
    video = make_video(admin, title='Revy: "Den store"')
    client = client_for(admin)

    def download(variant: str) -> Any:  # noqa: ANN401
        return client.get(reverse("gahkflix:download", args=[video.pk, variant]))

    original = download("original")
    assert original.status_code == 200 and 'filename="revy.mp4"' in original["Content-Disposition"]
    hd = download("hd")
    assert hd.status_code == 200 and hd["Content-Type"] == "video/mp4"
    assert 'filename="Revy Den store (HD).mp4"' in hd["Content-Disposition"]
    assert download("fullhd").status_code == 404  # this one has no Full HD rendition
    VideoFile.objects.filter(pk=video.file_id).update(full_hd=video.file.hd.name)  # type: ignore[union-attr]
    assert "(Full HD).mp4" in download("fullhd")["Content-Disposition"]
    assert download("4k").status_code == 404
    # Encoded before the sizes were recorded: the page asks the storage once, and keeps the answer.
    VideoFile.objects.filter(pk=video.file_id).update(hd_size=None, full_hd_size=None)
    page = client.get(reverse("gahkflix:detail", args=[video.pk])).content.decode()
    for variant in ("hd", "fullhd", "original"):
        assert reverse("gahkflix:download", args=[video.pk, variant]) in page
    video.file.refresh_from_db()  # type: ignore[union-attr]
    assert (video.file.hd_size, video.file.full_hd_size) == (2, 2)  # type: ignore[union-attr]
    assert page.count("2\xa0bytes") == 2 and "3\xa0bytes" in page  # HD, Full HD, and the original


def test_the_player_config_for_the_overlay(admin: Resident) -> None:
    series = Series.objects.create(title="Gangen", created_by=admin)
    first = make_video(admin, title="En", series=series, season=1, episode=1)
    second = make_video(admin, title="To", series=series, season=1, episode=2)
    services.save_position(first, admin, 120.0)
    client = client_for(admin)

    config = client.get(reverse("gahkflix:play", args=[first.pk]), {"format": "json"}).json()

    assert config["title"] == "Gangen · S1:A1" and config["subtitle"] == "En"
    assert config["streams"] == {"hd": reverse("gahkflix:stream", args=[first.pk, "hd"])}
    assert config["startAt"] == 120.0
    assert config["next"] == {"url": reverse("gahkflix:play", args=[second.pk]), "title": "To"}
    assert (
        client.get(reverse("gahkflix:play", args=[first.pk]), {"format": "json", "t": "0"}).json()["startAt"]
        == 0
    )
    draft = make_video(admin, published=False)
    VideoFile.objects.filter(video=draft).delete()
    assert client.get(reverse("gahkflix:play", args=[draft.pk]), {"format": "json"}).status_code == 409


# --- Editing ------------------------------------------------------------------------------------


def test_edits_within_30_days_apply_directly(
    admin: Resident, member: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    video = make_video(admin, days_old=10)
    cast = [
        {"resident": member.pk, "name": "", "role": "Hamlet"},
        {"resident": None, "name": "Hans Gæst", "role": ""},
    ]
    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(admin).post(
            reverse("gahkflix:edit", args=[video.pk]),
            details(
                video,
                title="Revy 2026 – director's cut",
                cast=cast,
                tags=["revy", "Jul"],
                recorded_on="2026-03-01",
            ),
        )
    assert response.status_code == 302
    video.refresh_from_db()
    assert video.title == "Revy 2026 – director's cut"
    assert video.recorded_on == date(2026, 3, 1) and video.recorded_on_manual
    assert [(m.resident, m.name, m.role) for m in video.cast.all()] == [
        (member, member.full_name, "Hamlet"),
        (None, "Hans Gæst", ""),
    ]
    assert sorted(video.tags.values_list("name", flat=True)) == ["Jul", "revy"]
    assert notified(member, categories.GAHKFLIX_CAST)
    assert not ChangeRequest.objects.exists()


def test_after_30_days_an_edit_needs_another_administrator(
    admin: Resident,
    admin2: Resident,
    member: Resident,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    video = make_video(member, days_old=31)
    with django_capture_on_commit_callbacks(execute=True):
        client_for(member).post(reverse("gahkflix:edit", args=[video.pk]), details(video, title="Ny titel"))
    video.refresh_from_db()
    assert video.title == "Revy 2026"
    change = ChangeRequest.objects.get()
    assert change.kind == ChangeRequest.Kind.EDIT and change.changes == {"title": "Ny titel"}
    assert notified(admin, categories.GAHKFLIX_APPROVALS)
    assert not notified(member, categories.GAHKFLIX_APPROVALS)

    # The one who asked cannot approve it; somebody else's proposal must wait.
    assert (
        client_for(member)
        .post(reverse("gahkflix:request_vote", args=[change.pk]), {"decision": "approve"})
        .status_code
        == 403
    )
    client_for(admin2).post(reverse("gahkflix:edit", args=[video.pk]), details(video, title="Anden titel"))
    assert ChangeRequest.objects.count() == 1

    with django_capture_on_commit_callbacks(execute=True):
        client_for(admin).post(reverse("gahkflix:request_vote", args=[change.pk]), {"decision": "approve"})
    change.refresh_from_db()
    video.refresh_from_db()
    assert change.state == ChangeRequest.State.APPROVED
    assert video.title == "Ny titel"
    assert notified(member, categories.GAHKFLIX_UPLOADS)


def test_a_rejected_picture_proposal_is_thrown_away(
    admin: Resident, admin2: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    video = make_video(admin, days_old=40)
    frame = VideoFrame(video=video, at_seconds=1.0)
    frame.image.save("frame.jpg", ContentFile(jpeg_bytes(size=(320, 180))), save=False)
    frame.save()
    old_thumbnail = video.thumbnail.name
    client_for(admin).post(
        reverse("gahkflix:choose_picture", args=[video.pk]), {"kind": "thumbnail", "frame": frame.pk}
    )
    change = ChangeRequest.objects.get()
    proposed = change.changes["thumbnail"]["key"]
    assert get_video_storage().exists(proposed)
    video.refresh_from_db()
    assert video.thumbnail.name == old_thumbnail
    assert (
        client_for(admin2).get(reverse("gahkflix:request_picture", args=[change.pk, "thumbnail"])).status_code
        == 200
    )

    with django_capture_on_commit_callbacks(execute=True):
        client_for(admin2).post(
            reverse("gahkflix:request_vote", args=[change.pk]), {"decision": "reject", "comment": "Sløret"}
        )
    change.refresh_from_db()
    assert change.state == ChangeRequest.State.REJECTED and change.note == "Sløret"
    assert not get_video_storage().exists(proposed)


def test_episode_numbers_are_unique_within_a_series(admin: Resident) -> None:
    series = Series.objects.create(title="Gangen", created_by=admin)
    make_video(admin, title="Pilot", series=series, season=1, episode=1)
    second = make_video(admin, title="Afsnit to", series=series)
    response = client_for(admin).post(
        reverse("gahkflix:edit", args=[second.pk]), details(second, series=series.pk, season=1, episode=1)
    )
    assert response.status_code == 200
    assert "findes allerede" in response.content.decode()


# --- Deleting -----------------------------------------------------------------------------------


def test_the_uploader_bins_a_video_within_seven_days(
    open_gate: None, plain: Resident, admin: Resident
) -> None:
    video = make_video(plain, days_old=3)
    assert client_for(plain).post(reverse("gahkflix:delete", args=[video.pk])).status_code == 302
    video.refresh_from_db()
    assert video.is_binned and video.deleted_by == plain
    assert client_for(plain).get(reverse("gahkflix:detail", args=[video.pk])).status_code == 404
    client_for(admin).post(reverse("gahkflix:restore", args=[video.pk]))
    video.refresh_from_db()
    assert not video.is_binned


def test_after_seven_days_only_administrators_may_ask(open_gate: None, plain: Resident) -> None:
    video = make_video(plain, days_old=8)
    assert client_for(plain).post(reverse("gahkflix:delete", args=[video.pk])).status_code == 403
    assert client_for(plain).get(reverse("gahkflix:request_deletion", args=[video.pk])).status_code == 403


def test_deleting_an_old_video_takes_two_other_administrators(
    admin: Resident,
    admin2: Resident,
    admin3: Resident,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    video = make_video(admin2, days_old=60)
    assert client_for(admin).post(reverse("gahkflix:delete", args=[video.pk])).status_code == 403
    with django_capture_on_commit_callbacks(execute=True):
        client_for(admin).post(reverse("gahkflix:request_deletion", args=[video.pk]), {"reason": "Krænkende"})
    change = ChangeRequest.objects.get(kind=ChangeRequest.Kind.DELETE)
    assert notified(admin2, categories.GAHKFLIX_APPROVALS) and notified(admin3, categories.GAHKFLIX_APPROVALS)

    client_for(admin2).post(reverse("gahkflix:request_vote", args=[change.pk]), {"decision": "approve"})
    video.refresh_from_db()
    assert not video.is_binned  # one of two
    with django_capture_on_commit_callbacks(execute=True):
        client_for(admin3).post(reverse("gahkflix:request_vote", args=[change.pk]), {"decision": "approve"})
    video.refresh_from_db()
    change.refresh_from_db()
    assert video.is_binned and video.deleted_by == admin
    assert change.state == ChangeRequest.State.APPROVED
    assert notified(admin, categories.GAHKFLIX_UPLOADS)


def test_one_rejection_closes_a_deletion_request(admin: Resident, admin2: Resident) -> None:
    video = make_video(admin2, days_old=60)
    change = services.request_deletion(video, admin, "Dublet")
    services.vote(change, admin2, approve=False)
    change.refresh_from_db()
    video.refresh_from_db()
    assert change.state == ChangeRequest.State.REJECTED and not video.is_binned


def test_a_draft_is_deleted_outright(
    admin: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    video = make_video(admin, published=False, days_old=20)
    keys = [video.thumbnail.name, video.file.original.name]  # type: ignore[union-attr]
    with django_capture_on_commit_callbacks(execute=True):
        client_for(admin).post(reverse("gahkflix:delete", args=[video.pk]))
    assert not Video.objects.exists()
    assert not any(get_video_storage().exists(key) for key in keys)


def test_the_nightly_sweep(
    admin: Resident, admin2: Resident, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    binned = make_video(admin, title="Gammel")
    services.bin_video(binned, admin)
    Video.objects.filter(pk=binned.pk).update(deleted_at=timezone.now() - timedelta(days=31))
    recently_binned = make_video(admin, title="Ny")
    services.bin_video(recently_binned, admin)
    abandoned = make_video(admin, title="Glemt kladde", published=False, days_old=91)
    fresh_draft = make_video(admin, title="Kladde", published=False, days_old=40)
    old = make_video(admin2, days_old=90)
    change = services.request_deletion(old, admin, "Dårlig lyd")
    ChangeRequest.objects.filter(pk=change.pk).update(created_at=timezone.now() - timedelta(days=31))
    with django_capture_on_commit_callbacks(execute=True):
        tasks.housekeeping()
    assert set(Video.objects.values_list("title", flat=True)) == {"Ny", "Kladde", old.title}
    assert not Video.objects.filter(pk__in=[binned.pk, abandoned.pk]).exists()
    assert Video.objects.filter(pk=fresh_draft.pk).exists()
    change.refresh_from_db()
    assert change.state == ChangeRequest.State.EXPIRED
    assert notified(admin, categories.GAHKFLIX_UPLOADS)


def test_a_draft_is_deleted_90_calendar_days_after_its_upload(admin: Resident) -> None:
    video = make_video(admin, published=False)
    today = timezone.localdate()
    uploaded = timezone.make_aware(datetime.combine(today - timedelta(days=89), time(23, 59)))
    Video.objects.filter(pk=video.pk).update(created_at=uploaded)
    video.refresh_from_db()
    assert video.draft_deleted_on == today + timedelta(days=1)
    assert not services.expired_drafts(today).exists()
    assert services.expired_drafts(today + timedelta(days=1)).get() == video
    Video.objects.filter(pk=video.pk).update(created_at=uploaded - timedelta(days=1))  # 90 days ago
    assert services.purge() == (0, 1)


def test_weekly_reminders_about_unpublished_drafts(
    admin: Resident, admin2: Resident, member: Resident, open_gate: None, plain: Resident
) -> None:
    make_video(plain, title="Første", published=False, days_old=20)
    make_video(plain, title="Anden", published=False, days_old=10)
    make_video(member, title="Eneste", published=False, days_old=8)
    make_video(admin, title="Ny kladde", published=False, days_old=3)  # too fresh to nag about
    make_video(admin, title="Udgivet", days_old=30)

    assert services.send_weekly_draft_reminders() == 3

    [own] = notified(plain, categories.GAHKFLIX_UPLOADS)
    assert own.title == "Du har 2 videoer, der ikke er udgivet"
    assert "Første" in own.body and own.url == reverse("gahkflix:mine")
    [single] = notified(member, categories.GAHKFLIX_UPLOADS)
    assert single.title == "Eneste er ikke udgivet endnu"
    assert not notified(admin, categories.GAHKFLIX_UPLOADS)
    for administrator in (admin, admin2, member):
        [overview] = notified(administrator, categories.GAHKFLIX_DRAFTS)
        assert overview.title == "3 videoer på GAHKFlix er ikke udgivet"
        assert overview.url == reverse("gahkflix:drafts")
    assert not notified(plain, categories.GAHKFLIX_DRAFTS)


def test_a_last_warning_the_day_before_a_draft_is_deleted(
    admin: Resident, admin2: Resident, member: Resident
) -> None:
    expiring = make_video(member, title="Sidste chance", published=False, days_old=89)
    make_video(member, title="Masser af tid", published=False, days_old=60)

    assert services.warn_expiring_drafts() == 1
    assert services.warn_expiring_drafts() == 0  # once, however often the job runs

    [mine] = notified(member, categories.GAHKFLIX_UPLOADS)
    assert mine.title == "Sidste chance slettes i nat"
    assert mine.url == reverse("gahkflix:edit", args=[expiring.pk])
    for administrator in (admin, admin2):
        [warning] = notified(administrator, categories.GAHKFLIX_DRAFTS)
        assert warning.title == "Kladden Sidste chance slettes i nat"
    assert not notified(member, categories.GAHKFLIX_DRAFTS)  # the uploader is told once, as uploader


def test_the_drafts_page_is_for_administrators(admin: Resident, open_gate: None, plain: Resident) -> None:
    make_video(plain, title="Halvfærdig", published=False, days_old=12)
    page = client_for(admin).get(reverse("gahkflix:drafts"))
    assert page.status_code == 200 and "Halvfærdig" in page.content.decode()
    assert client_for(plain).get(reverse("gahkflix:drafts")).status_code == 403


# --- Files nothing refers to --------------------------------------------------------------------


def _store_aged(key: str, days: float) -> str:
    storage = get_video_storage()
    name = storage.save(key, ContentFile(b"x"))
    moment = (timezone.now() - timedelta(days=days)).timestamp()
    os.utime(storage.path(name), (moment, moment))
    return name


def test_files_nothing_refers_to_are_swept_after_two_days(admin: Resident, admin2: Resident) -> None:
    video = make_video(admin, days_old=40)
    abandoned = _store_aged("gahkflix/0b7d/0c1e/original/aflyst.mp4", days=3)
    in_flight = _store_aged("gahkflix/0b7d/0c1f/original/undervejs.mp4", days=1)
    frame = VideoFrame(video=video, at_seconds=1.0)
    frame.image.save("frame.jpg", ContentFile(jpeg_bytes()), save=False)
    frame.save()
    proposed = services.store_picture(video, "banner", jpeg_bytes())
    services.propose(video, {"banner": {"key": proposed, "frame": None}}, admin2)
    keep = [video.thumbnail.name, video.banner.name, video.file.original.name, frame.image.name, proposed]  # type: ignore[union-attr]
    storage = get_video_storage()
    for name in keep:
        os.utime(storage.path(name), (0, 0))  # old, but referenced

    result = services.sweep_orphans()

    assert result.deleted == [abandoned]
    assert not storage.exists(abandoned)
    assert storage.exists(in_flight)
    assert all(storage.exists(name) for name in keep)


def test_the_sweep_refuses_when_the_database_does_not_match_the_bucket(admin: Resident) -> None:
    make_video(admin)
    stray = [_store_aged(f"gahkflix/{index:04x}/original/fremmed.mp4", days=10) for index in range(25)]
    result = services.sweep_orphans()
    assert result.refused and result.deleted == []
    assert all(get_video_storage().exists(name) for name in stray)


def test_a_refused_picture_proposal_does_not_leave_its_file_behind(admin: Resident, admin2: Resident) -> None:
    video = make_video(admin, days_old=40)
    services.propose(video, {"title": "Andet navn"}, admin2)
    key = services.store_picture(video, "thumbnail", jpeg_bytes())
    with pytest.raises(ValidationError):
        services.set_picture(video, "thumbnail", key, None, admin, needs_approval=True)
    assert not get_video_storage().exists(key)


# --- Watching -----------------------------------------------------------------------------------


def test_views_count_once_per_ten_minutes(admin: Resident) -> None:
    video = make_video(admin)
    now = timezone.now()
    assert services.record_view(video, admin, now)
    assert not services.record_view(video, admin, now + timedelta(minutes=5))
    assert services.record_view(video, admin, now + timedelta(minutes=16))
    assert services.view_counts([video])[video.pk] == (2, 1)
    stats = services.view_stats(video)
    assert stats.views == 2 and stats.viewers == 1 and stats.peak >= 1


def test_a_draft_is_not_counted(admin: Resident) -> None:
    video = make_video(admin, published=False)
    response = client_for(admin).post(reverse("gahkflix:record_view", args=[video.pk]))
    assert response.json() == {"counted": False}


def test_trending_ranks_by_unique_viewers_this_week(
    admin: Resident, admin2: Resident, member: Resident
) -> None:
    popular = make_video(admin, title="Populær")
    quiet = make_video(admin, title="Stille")
    for resident in (admin, admin2, member):
        services.record_view(popular, resident)
    services.record_view(quiet, admin)
    old = make_video(admin, title="Sidste år")
    for resident in (admin, admin2, member):
        services.record_view(old, resident, timezone.now() - timedelta(days=8))
    assert [video.title for video in services.trending()] == ["Populær", "Stille"]


def test_resume_where_you_stopped(admin: Resident) -> None:
    video = make_video(admin, duration=600)
    client = client_for(admin)
    client.post(
        reverse("gahkflix:position", args=[video.pk]), {"position": 120.5}, content_type="application/json"
    )
    assert services.resume_position(video, admin) == 120.5
    assert services.continue_watching(admin) == [video]
    page = client.get(reverse("gahkflix:play", args=[video.pk]))
    assert page.context["player"]["startAt"] == 120.5
    client.post(
        reverse("gahkflix:position", args=[video.pk]), {"position": 598}, content_type="application/json"
    )
    assert services.resume_position(video, admin) == 0
    assert services.continue_watching(admin) == []
    bad = client.post(
        reverse("gahkflix:position", args=[video.pk]), {"position": "nan"}, content_type="application/json"
    )
    assert bad.status_code == 400


# --- Browsing -----------------------------------------------------------------------------------


def test_search_finds_titles_tags_credits_and_series(admin: Resident, plain: Resident) -> None:
    series = Series.objects.create(title="Kollegiekanalen", created_by=admin)
    video = make_video(admin, title="Sommerfest", series=series, episode=1)
    services.apply_changes(
        video,
        {
            "tags": ["fest"],
            "cast": [
                {"resident": None, "name": "Gitte Gæst", "role": "Vært"},
                {"resident": plain.pk, "name": "", "role": ""},
            ],
        },
        admin,
    )
    make_video(admin, title="Noget helt andet")
    for query in ("sommer", "fest", "gitte", "kollegiekanalen", "hansen", "vært"):
        assert list(services.search(query)) == [video], query
    assert not services.search("sommerfest julefrokost").exists()
    response = client_for(admin).get(reverse("gahkflix:search"), {"q": "gitte", "partial": "1"})
    assert "Sommerfest" in response.content.decode()


def test_cast_pages_and_the_profile_strip(admin: Resident, plain: Resident) -> None:
    video = make_video(admin, title="Revy")
    CastMember.objects.create(video=video, resident=plain, name=plain.full_name, role="Hamlet")
    CastMember.objects.create(video=video, name="Ole Udefra")
    client = client_for(admin)
    assert "Revy" in client.get(reverse("gahkflix:cast", args=[plain.pk])).content.decode()
    assert "Revy" in client.get(reverse("gahkflix:cast_name"), {"navn": "ole udefra"}).content.decode()
    assert client.get(reverse("gahkflix:cast_name"), {"navn": "ingen"}).status_code == 404
    assert "Medvirker i" in client.get(reverse("resident_profile", args=[plain.pk])).content.decode()
    # Behind the gate the strip is not drawn for a reader who cannot open GAHKFlix.
    assert (
        "Medvirker i"
        not in client_for(plain).get(reverse("resident_profile", args=[plain.pk])).content.decode()
    )


def test_series_episodes_and_next_episode(admin: Resident) -> None:
    series = Series.objects.create(title="Gangen", created_by=admin)
    second = make_video(admin, title="To", series=series, season=1, episode=2)
    first = make_video(admin, title="En", series=series, season=1, episode=1)
    later = make_video(admin, title="Tre", series=series, season=2, episode=1)
    make_video(admin, title="Kladde", series=series, season=2, episode=2, published=False)
    assert [(season, [v.title for v in videos]) for season, videos in services.episodes(series)] == [
        (1, ["En", "To"]),
        (2, ["Tre"]),
    ]
    assert services.next_episode(first) == second
    assert services.next_episode(second) == later
    assert services.next_episode(later) is None
    assert services.series_cover(series) == first


def test_series_rules(open_gate: None, plain: Resident, make_resident: Callable[..., Resident]) -> None:
    client = client_for(plain)
    client.post(reverse("gahkflix:series_create"), {"title": "Sketches", "description": ""})
    series = Series.objects.get()
    assert series.created_by == plain
    other = make_resident(email="anden@gahk.dk")
    assert client_for(other).get(reverse("gahkflix:series_edit", args=[series.pk])).status_code == 403
    make_video(plain, series=series)
    client.post(reverse("gahkflix:series_delete", args=[series.pk]))
    assert Series.objects.exists()
    Series.objects.filter(pk=series.pk).update(created_at=timezone.now() - timedelta(days=31))
    client.post(reverse("gahkflix:series_edit", args=[series.pk]), {"title": "Sketchshow", "description": ""})
    series.refresh_from_db()
    assert series.title == "Sketches"
    assert ChangeRequest.objects.get().series == series


def test_pages_render(admin: Resident, admin2: Resident) -> None:
    series = Series.objects.create(title="Gangen", created_by=admin)
    video = make_video(admin, series=series, season=1, episode=1)
    services.apply_changes(video, {"tags": ["revy"]}, admin)
    services.record_view(video, admin2)
    draft = make_video(admin, title="Kladde", published=False)
    binned = make_video(admin, title="Slettet")
    services.bin_video(binned, admin)
    change = services.request_deletion(make_video(admin2, title="Gammel", days_old=50), admin, "Fordi")
    client = client_for(admin)
    for url in [
        reverse("gahkflix:home"),
        reverse("gahkflix:series_list"),
        reverse("gahkflix:series", args=[series.pk]),
        reverse("gahkflix:series_create"),
        reverse("gahkflix:series_edit", args=[series.pk]),
        reverse("gahkflix:timeline"),
        reverse("gahkflix:search") + "?q=revy",
        reverse("gahkflix:tag", args=["revy"]),
        reverse("gahkflix:mine"),
        reverse("gahkflix:upload"),
        reverse("gahkflix:detail", args=[video.pk]),
        reverse("gahkflix:play", args=[video.pk]),
        reverse("gahkflix:edit", args=[video.pk]),
        reverse("gahkflix:edit", args=[draft.pk]),
        reverse("gahkflix:approvals"),
        reverse("gahkflix:request", args=[change.pk]),
        reverse("gahkflix:bin"),
        reverse("gahkflix:status", args=[draft.pk]),
        reverse("gahkflix:residents_lookup") + "?q=bo",
        reverse("gahkflix:tags_lookup") + "?q=re",
    ]:
        assert client.get(url).status_code == 200, url
    assert (
        client_for(admin2)
        .get(reverse("gahkflix:request_deletion", args=[make_video(admin, days_old=9).pk]))
        .status_code
        == 200
    )


# --- The worker ---------------------------------------------------------------------------------


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    sent: list[int] = []
    monkeypatch.setattr(tasks.process_video_file, "delay", sent.append)
    yield sent


def test_a_stalled_encode_is_sent_again_then_given_up(admin: Resident, dispatched: list[int]) -> None:
    video = make_video(admin, published=False)
    stalled = video.files.get()
    old = timezone.now() - timedelta(minutes=20)
    VideoFile.objects.filter(pk=stalled.pk).update(state=FileState.PROCESSING, heartbeat_at=old, attempts=1)
    tasks.recover_stalled()
    stalled.refresh_from_db()
    assert stalled.state == FileState.QUEUED and dispatched == [stalled.pk]

    VideoFile.objects.filter(pk=stalled.pk).update(state=FileState.PROCESSING, heartbeat_at=old, attempts=3)
    tasks.recover_stalled()
    stalled.refresh_from_db()
    assert stalled.state == FileState.FAILED


def test_a_claimed_file_is_not_processed_twice(admin: Resident) -> None:
    video = make_video(admin, published=False)
    VideoFile.objects.filter(video=video).update(state=FileState.PROCESSING)
    assert tasks.process_video_file(video.files.get().pk) is False


# --- ffmpeg helpers -----------------------------------------------------------------------------


def test_probe_reads_rotation_duration_and_date() -> None:
    output = """
  Duration: 00:01:02.50, start: 0.000000, bitrate: 15000 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), 1920x1080, 30 fps
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo
    Metadata:
      com.apple.quicktime.creationdate: 2025-12-24T19:30:00+0100
"""
    probe = encoding.parse_probe(output)
    assert (probe.width, probe.height) == (1080, 1920)
    assert probe.duration == 62.5
    assert probe.has_audio and probe.has_video
    assert probe.recorded_at is not None and probe.recorded_at.date() == date(2025, 12, 24)


def test_renditions_are_never_upscaled() -> None:
    assert [r.name for r in encoding.plan_renditions(1920, 1080)] == ["fullhd", "hd"]
    assert [r.box for r in encoding.plan_renditions(1080, 1920)] == [(1080, 1920), (720, 1280)]
    assert [r.name for r in encoding.plan_renditions(1280, 720)] == ["hd"]
    assert [r.name for r in encoding.plan_renditions(640, 480)] == ["hd"]


def _picture(size: tuple[int, int]) -> "Image.Image":
    """Grey, with a red band across the top tenth: what a crop to 16:9 would cut off a portrait."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, (128, 128, 128))
    ImageDraw.Draw(image).rectangle((0, 0, size[0], size[1] // 10), fill=(255, 0, 0))
    return image


def test_a_shape_close_to_16_by_9_is_cropped() -> None:
    for size, expected in (
        ((1800, 1080), (1800, 1012)),
        ((1440, 1080), (1440, 810)),
        ((2560, 1080), (1920, 1080)),
    ):
        picture = encoding.sixteen_nine(_picture(size), encoding.BANNER_MAX_WIDTH)
        assert picture.size == expected, size


def test_a_portrait_or_square_picture_is_kept_whole_on_a_blur() -> None:
    banner = encoding.sixteen_nine(_picture((1080, 1920)), encoding.BANNER_MAX_WIDTH)
    assert banner.size == (1920, 1080)
    # The picture, scaled to the banner's height, sits in the middle with its red top intact ...
    red, green, blue = banner.getpixel((960, 20))  # type: ignore[misc]
    assert red > 200 and green < 60 and blue < 60
    # ... beside a darkened blur of itself, not black bars.
    left = banner.getpixel((40, 540))
    assert left != (0, 0, 0) and left[0] < 128  # type: ignore[index]

    small_square = encoding.sixteen_nine(_picture((300, 300)), encoding.THUMBNAIL_SIZE[0])
    assert small_square.size == (533, 300)  # never enlarged: as tall as the picture itself


def test_frame_times_are_spread_across_the_film() -> None:
    import random

    times = encoding.frame_times(100.0, 10, random.Random(3))
    assert len(times) == 10
    assert all(5 <= t <= 95 for t in times)
    assert times == sorted(times)
    assert all(int((t - 5) // 9) == index for index, t in enumerate(times))
