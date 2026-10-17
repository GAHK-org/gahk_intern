"""The local disk copy of live Den Hurtige and opslagstavle photos (core.media.register_cacheable).

A live message's photo, and a recent or pinned opslag's, is served from the server's own disk. Once
archived (a Den Hurtige message past expires_at, an opslag older than NOTICE_IMAGE_CACHE_DAYS) it is
served the way it was before, by the storage branch of core.media.serve_media. With the plain
FileSystemStorage the suite runs on, that branch streams with a FileResponse, which is how these
tests tell the two apart; against the bucket the same branch is the presigned redirect.
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.http import FileResponse
from django.test import Client
from django.utils import timezone

from core.media import CACHED_COPY_CACHE_CONTROL
from den_hurtige.models import QuickComment, QuickPost
from opslagstavle.models import Notice, NoticeComment, NoticeImage
from residents.models import Resident

from .media_samples import jpeg_bytes


@pytest.fixture
def media(settings: object, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path / "media"  # type: ignore[attr-defined]
    return tmp_path / "media"


@pytest.fixture
def cache_dir(settings: object, tmp_path: Path, media: Path) -> Path:
    settings.MEDIA_CACHE_DIR = str(tmp_path / "cache")  # type: ignore[attr-defined]
    return tmp_path / "cache" / "attachments"


def _body(response: object) -> bytes:
    if isinstance(response, FileResponse):
        return b"".join(response.streaming_content)  # type: ignore[arg-type]
    return response.content  # type: ignore[attr-defined]


def _served_from_cache(client: Client, url: str) -> bool:
    response = client.get(url)
    assert response.status_code == 200
    return response.headers.get("Cache-Control") == CACHED_COPY_CACHE_CONTROL


def _post(author: Resident, *, expires_in: timedelta = timedelta(hours=1)) -> QuickPost:
    post = QuickPost.objects.create(author=author, content="Kaffe", expires_at=timezone.now() + expires_in)
    post.image.save("kaffe.jpg", ContentFile(jpeg_bytes()), save=True)
    return post


@pytest.mark.django_db
def test_a_live_messages_photo_is_served_from_the_local_copy(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    resident = make_resident()
    client.force_login(resident)
    post = _post(resident)

    response = client.get(post.image.url)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == CACHED_COPY_CACHE_CONTROL
    assert response.headers["Vary"] == "Cookie"
    assert response["Content-Type"] == "image/jpeg"
    assert _body(response) == post.image.read()
    assert (cache_dir / post.image.name).is_file()


@pytest.mark.django_db
def test_an_archived_messages_photo_goes_to_storage_and_is_not_copied(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    resident = make_resident()
    client.force_login(resident)
    post = _post(resident, expires_in=-timedelta(minutes=1))

    assert not _served_from_cache(client, post.image.url)
    assert not (cache_dir / post.image.name).exists()


@pytest.mark.django_db
def test_a_replys_photo_lives_as_long_as_its_message(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    resident = make_resident()
    client.force_login(resident)
    post = _post(resident)
    reply = QuickComment.objects.create(post=post, author=resident, content="Ja tak")
    reply.image.save("svar.jpg", ContentFile(jpeg_bytes()), save=True)

    assert _served_from_cache(client, reply.image.url)

    QuickPost.objects.filter(pk=post.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert not _served_from_cache(client, reply.image.url)


@pytest.mark.django_db
def test_login_is_still_required(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    post = _post(make_resident())

    response = client.get(post.image.url)

    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert not cache_dir.exists()


@pytest.mark.django_db(transaction=True)
def test_a_deleted_photo_is_dropped_from_the_local_copy(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    resident = make_resident()
    client.force_login(resident)
    post = _post(resident)
    name = post.image.name
    assert _served_from_cache(client, post.image.url)

    post.delete()

    assert not (cache_dir / name).exists()


@pytest.mark.django_db
def test_caching_is_off_without_a_directory(
    client: Client, make_resident: Callable[..., Resident], media: Path
) -> None:
    resident = make_resident()
    client.force_login(resident)
    post = _post(resident)

    assert not _served_from_cache(client, post.image.url)


# --- opslagstavlen: no archive, so an age cutoff --------------------------------------------------


def _notice_image(author: Resident, *, age: timedelta, pinned: bool = False) -> NoticeImage:
    notice = Notice.objects.create(author=author, body="Opslag", pinned_at=timezone.now() if pinned else None)
    Notice.objects.filter(pk=notice.pk).update(created_at=timezone.now() - age)  # auto_now_add
    image = NoticeImage(notice=notice, uploaded_by=author)
    image.file.save("plakat.jpg", ContentFile(jpeg_bytes()), save=True)
    return image


@pytest.mark.django_db
def test_a_recent_opslags_photo_is_cached_and_an_old_ones_is_not(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path, settings: object
) -> None:
    settings.NOTICE_IMAGE_CACHE_DAYS = 30  # type: ignore[attr-defined]
    resident = make_resident()
    client.force_login(resident)

    assert _served_from_cache(client, _notice_image(resident, age=timedelta(days=2)).file.url)
    assert not _served_from_cache(client, _notice_image(resident, age=timedelta(days=31)).file.url)


@pytest.mark.django_db
def test_a_pinned_opslag_is_read_like_a_new_one(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    resident = make_resident()
    client.force_login(resident)

    assert _served_from_cache(client, _notice_image(resident, age=timedelta(days=400), pinned=True).file.url)


@pytest.mark.django_db
def test_a_photo_in_a_post_being_written_is_cached(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    """Uploaded before the post is saved, so it has no notice yet; the editor's preview loads it."""
    resident = make_resident()
    client.force_login(resident)
    image = NoticeImage(uploaded_by=resident)
    image.file.save("udkast.jpg", ContentFile(jpeg_bytes()), save=True)

    assert _served_from_cache(client, image.file.url)


@pytest.mark.django_db
def test_a_comments_photo_goes_by_the_comments_own_age(
    client: Client, make_resident: Callable[..., Resident], cache_dir: Path
) -> None:
    """A new comment on an old opslag is new to read; the longer prefix's rule wins over opslag/'s."""
    resident = make_resident()
    client.force_login(resident)
    notice = Notice.objects.create(author=resident, body="Gammelt opslag")
    Notice.objects.filter(pk=notice.pk).update(created_at=timezone.now() - timedelta(days=400))
    comment = NoticeComment.objects.create(notice=notice, author=resident, body="Nyt svar")
    comment.image.save("svar.jpg", ContentFile(jpeg_bytes()), save=True)
    assert comment.image.name.startswith("opslag/kommentarer/")

    assert _served_from_cache(client, comment.image.url)

    NoticeComment.objects.filter(pk=comment.pk).update(created_at=timezone.now() - timedelta(days=31))
    assert not _served_from_cache(client, comment.image.url)
