"""The local copy of thumbnails and HD images in front of the bucket (photo_album.cache)."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from django.test import Client

from photo_album import cache
from photo_album.models import Album, Media, MediaStatus
from photo_album.services import build_derivatives, upload_media
from photo_album.storage import get_photo_album_storage
from residents.models import Resident

from .media_samples import image_upload


@pytest.fixture
def cache_dir(settings: object, tmp_path: Path) -> Path:
    settings.PHOTO_ALBUM_CACHE_DIR = str(tmp_path / "cache")  # type: ignore[attr-defined]
    return tmp_path / "cache"


def _stored(resident: Resident, **fields: object) -> Media:
    album = Album.objects.create(folder="2024", name="Fest")
    media = upload_media(album=album, uploaded_file=image_upload("x.jpg"), resident=resident, approved=True)
    assert build_derivatives(media)
    if fields:
        Media.objects.filter(pk=media.pk).update(**fields)
        media.refresh_from_db()
    return media


def _read(key: str) -> bytes:
    with get_photo_album_storage().open(key, "rb") as handle:
        return handle.read()


def _body(response: object) -> bytes:
    return b"".join(response.streaming_content) if response.streaming else response.content  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_a_thumbnail_is_copied_once_then_served_from_disk(
    client: Client, cache_dir: Path, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    media = _stored(resident)
    client.force_login(resident)
    stored = _read(media.thumbnail.name)

    first = client.get(media.thumbnail.url)
    copy = cache_dir / cache.THUMBNAIL / media.thumbnail.name
    assert first.status_code == 200 and first.content == stored
    assert first["Cache-Control"] == "private, max-age=86400"
    assert copy.read_bytes() == stored

    media.thumbnail.storage.delete(media.thumbnail.name)  # the bucket is no longer asked
    assert client.get(media.thumbnail.url).content == stored


@pytest.mark.django_db
def test_hd_images_are_cached_but_originals_are_not(
    client: Client, cache_dir: Path, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    media = _stored(resident)
    client.force_login(resident)

    assert client.get(media.high_definition.url).status_code == 200
    assert (cache_dir / cache.HIGH_DEFINITION / media.high_definition.name).is_file()
    original = client.get(media.original.url)
    assert original.status_code == 200 and _body(original) == _read(media.original.name)
    hd_copy = cache_dir / cache.HIGH_DEFINITION / media.high_definition.name
    assert [path for path in cache_dir.rglob("*") if path.is_file()] == [hd_copy]


@pytest.mark.django_db
def test_a_video_is_never_cached_in_hd(cache_dir: Path, make_resident: Callable[..., Resident]) -> None:
    """Its player seeks with Range requests, which only the bucket answers."""
    media = _stored(make_resident(), content_type="video/mp4")

    assert cache.cached_file(media, media.high_definition.name) is None
    assert cache.cached_file(media, media.thumbnail.name) is not None


@pytest.mark.django_db
def test_nothing_is_cached_for_a_reader_who_may_not_see_it(
    client: Client, cache_dir: Path, make_resident: Callable[..., Resident]
) -> None:
    media = _stored(make_resident(email="owner@gahk.dk"), status=MediaStatus.PENDING)
    client.force_login(make_resident(email="other@gahk.dk"))

    assert client.get(media.thumbnail.url).status_code == 403
    assert not cache_dir.exists()


@pytest.mark.django_db
def test_a_failed_copy_falls_back_to_the_bucket(
    client: Client, cache_dir: Path, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    resident = make_resident()
    media = _stored(resident)
    client.force_login(resident)

    def disk_full(*_args: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(cache, "_fetch", disk_full)
    response = client.get(media.thumbnail.url)
    assert response.status_code == 200 and _body(response) == _read(media.thumbnail.name)


@pytest.mark.django_db
def test_caching_is_off_without_a_directory(make_resident: Callable[..., Resident]) -> None:
    media = _stored(make_resident())
    assert cache.cached_file(media, media.thumbnail.name) is None


def test_eviction_drops_the_least_recently_served_first(tmp_path: Path) -> None:
    for age, name in enumerate(["newest", "middle", "oldest"]):
        path = tmp_path / "photo-album" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)
        os.utime(path, (1_000_000 - age, 1_000_000 - age))
    partial = tmp_path / ".partial-dead"
    partial.write_bytes(b"x" * 50)
    os.utime(partial, (0, 0))

    assert cache.evict(tmp_path, budget=250) == 100
    assert sorted(path.name for path in tmp_path.rglob("*") if path.is_file()) == ["middle", "newest"]
    assert cache.evict(tmp_path, budget=250) == 0
