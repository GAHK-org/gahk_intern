import io
import posixpath
from collections.abc import Buffer, Iterator
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone
from zipstream import ZIP_STORED, ZipStream

from .models import AlbumDownload, AlbumDownloadState, DerivativeState, Media
from .services import build_derivatives, pending_derivatives
from .storage import PhotoAlbumS3Storage, get_photo_album_storage


@shared_task
def build_media_derivatives(media_id: int) -> bool:
    """Build derivatives for one committed upload, if it still needs them."""
    try:
        media = Media.objects.get(pk=media_id, derivative_state=DerivativeState.PENDING)
    except Media.DoesNotExist:
        return False
    return build_derivatives(media)


@shared_task
def process_pending_media(limit: int = 10) -> int:
    """Backstop for messages lost while the broker is unavailable."""
    for media in pending_derivatives()[:limit]:
        build_media_derivatives.delay(media.pk)
    return min(pending_derivatives().count(), limit)


@shared_task
def purge_expired_media() -> None:
    """Permanently remove photo-album media past its retention period."""
    call_command("purge_photo_album")


def _original_chunks(media: Media) -> Iterator[bytes]:
    with media.original.open("rb") as original:
        while chunk := original.read(1024 * 1024):
            yield chunk


class _ZipReader(io.RawIOBase):
    """Adapt ZipStream's iterator to boto3's non-seekable upload interface."""

    def __init__(self, archive: ZipStream) -> None:
        self._chunks = iter(archive)
        self._remainder = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer)
        while len(self._remainder) < len(view):
            try:
                self._remainder += next(self._chunks)
            except StopIteration:
                break
        size = min(len(view), len(self._remainder))
        view[:size] = self._remainder[:size]
        self._remainder = self._remainder[size:]
        return size


def _archive(download: AlbumDownload) -> ZipStream:
    archive = ZipStream(compress_type=ZIP_STORED)
    names: set[str] = set()
    media_items = Media.objects.filter(pk__in=download.media_ids, deleted_at__isnull=True).only(
        "pk", "original"
    )
    for media in media_items.order_by("pk"):
        original_name = Path(media.original.name or f"media-{media.pk}").name
        filename = original_name
        if filename in names:
            stem, suffix = posixpath.splitext(original_name)
            filename = f"{stem}-{media.pk}{suffix}"
        names.add(filename)
        archive.add(_original_chunks(media), arcname=filename)
    return archive


@shared_task
def build_album_download(download_id: int) -> bool:
    """Build an album ZIP into object storage, without occupying a web worker."""
    try:
        download = AlbumDownload.objects.get(pk=download_id, state=AlbumDownloadState.QUEUED)
    except AlbumDownload.DoesNotExist:
        return False
    download.state = AlbumDownloadState.BUILDING
    download.save(update_fields=["state"])
    key = f"photo-album-zips/{download.token}.zip"
    try:
        archive = _archive(download)
        storage = get_photo_album_storage()
        if isinstance(storage, PhotoAlbumS3Storage):
            storage.bucket.upload_fileobj(
                _ZipReader(archive), key, ExtraArgs={"ContentType": "application/zip"}
            )
        else:
            storage.save(key, ContentFile(b"".join(archive)))
    except Exception as exc:
        download.state = AlbumDownloadState.FAILED
        download.error = str(exc)[:255]
        download.completed_at = timezone.now()
        download.save(update_fields=["state", "error", "completed_at"])
        raise
    download.state = AlbumDownloadState.READY
    download.archive_key = key
    download.completed_at = timezone.now()
    download.save(update_fields=["state", "archive_key", "completed_at"])
    return True


@shared_task
def purge_expired_downloads() -> int:
    """Remove completed album ZIP jobs and their private archives after seven days."""
    cutoff = timezone.now() - timedelta(days=7)
    downloads = AlbumDownload.objects.filter(completed_at__lt=cutoff)
    storage = get_photo_album_storage()
    count = 0
    for download in downloads.iterator():
        if download.archive_key:
            storage.delete(download.archive_key)
        download.delete()
        count += 1
    return count
