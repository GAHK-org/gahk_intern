import io
import posixpath
from collections.abc import Buffer, Iterator
from pathlib import Path

from celery import shared_task
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db.models import Q, QuerySet
from django.utils import timezone
from zipstream import ZIP_STORED, ZipStream

from .models import (
    ALBUM_DOWNLOAD_RETENTION,
    ALBUM_DOWNLOAD_STALE_AFTER,
    DERIVATIVE_CLAIM_TTL,
    AlbumDownload,
    AlbumDownloadState,
    DerivativeState,
    Media,
)
from .services import build_derivatives, pending_derivatives
from .storage import PhotoAlbumS3Storage, get_photo_album_storage

# The two jobs here that legitimately outrun the global CELERY_TASK_TIME_LIMIT of fifteen minutes:
# an H.264 encode of a phone clip, and a whole-album ZIP streamed into object storage. Under the
# global limit both were SIGKILLed mid-work — a transcode left the media PENDING with its attempt
# counter bumped, so three passes marked a perfectly good file FAILED, and a ZIP left its download
# row wedged in BUILDING (see ALBUM_DOWNLOAD_STALE_AFTER).
#
# The SOFT limit is the one that does the work. It raises SoftTimeLimitExceeded *inside* the task,
# which both tasks already handle — `build_album_download` catches it in its `except Exception` and
# records FAILED with a message, rather than vanishing. The hard limit is only the backstop for a
# task wedged somewhere uninterruptible, five minutes later.
MEDIA_TASK_TIME_LIMIT = 60 * 60
MEDIA_TASK_SOFT_TIME_LIMIT = MEDIA_TASK_TIME_LIMIT - 300


def _unclaimed(queryset: "QuerySet[Media]") -> "QuerySet[Media]":
    """Rows no worker currently holds — never claimed, or claimed by one that never came back."""
    return queryset.filter(
        Q(derivative_started_at__isnull=True)
        | Q(derivative_started_at__lt=timezone.now() - DERIVATIVE_CLAIM_TTL)
    )


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def build_media_derivatives(media_id: int) -> bool:
    """Build derivatives for one committed upload, if it still needs them and nobody else has it."""
    # CLAIM FIRST, AS ONE STATEMENT. `UPDATE ... WHERE` takes the row lock, so of two workers handed
    # the same id exactly one sees a rowcount of 1 and the other backs off — which a read-then-write
    # could not promise. Whoever loses returns False rather than starting a second ffmpeg encode of
    # the same file and a second increment of `derivative_attempts`.
    claimed = _unclaimed(Media.objects.filter(pk=media_id, derivative_state=DerivativeState.PENDING)).update(
        derivative_started_at=timezone.now()
    )
    if not claimed:
        return False
    try:
        media = Media.objects.get(pk=media_id)
    except Media.DoesNotExist:
        return False
    return build_derivatives(media)


@shared_task
def process_pending_media(limit: int = 10) -> int:
    """Backstop for messages lost while the broker is unavailable.

    Skips rows a worker already holds. The claim in `build_media_derivatives` is what actually makes
    that safe — this only avoids queueing work that would be thrown away on arrival.
    """
    dispatched = 0
    # Counted as we go, rather than re-running the query afterwards: the old
    # `min(pending_derivatives().count(), limit)` re-read the table AFTER dispatching and so
    # reported whatever was still pending at that instant — racing the worker it had just started,
    # and surfacing that racy number as the task result on the siteadmin jobs page.
    for media in _unclaimed(pending_derivatives())[:limit]:
        build_media_derivatives.delay(media.pk)
        dispatched += 1
    return dispatched


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


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
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
def fail_stalled_downloads() -> int:
    """Close out ZIP builds no worker is coming back to, so the page stops waiting on them.

    A worker killed outright — hard time limit, OOM, a redeploy mid-build — runs no exception
    handler, so the row it was working on keeps `state = BUILDING` and `completed_at = NULL`. Two
    things then never happen: `purge_expired_downloads` filters on `completed_at`, so the row is
    never collected; and photo_album/detail.html keeps drawing the "Download klargøres …" branch,
    whose five-second reload then runs for as long as the resident leaves that page open.

    Setting `completed_at` here is what hands the row back to `purge_expired_downloads`, so this
    ends the reload loop AND lets the row be collected on the ordinary seven-day schedule.
    """
    cutoff = timezone.now() - ALBUM_DOWNLOAD_STALE_AFTER
    return AlbumDownload.objects.filter(
        state__in=(AlbumDownloadState.QUEUED, AlbumDownloadState.BUILDING),
        created_at__lt=cutoff,
    ).update(
        state=AlbumDownloadState.FAILED,
        error="Download blev afbrudt undervejs.",
        completed_at=timezone.now(),
    )


@shared_task
def purge_expired_downloads() -> int:
    """Remove completed album ZIP jobs and their private archives after seven days."""
    cutoff = timezone.now() - ALBUM_DOWNLOAD_RETENTION
    downloads = AlbumDownload.objects.filter(completed_at__lt=cutoff)
    storage = get_photo_album_storage()
    count = 0
    for download in downloads.iterator():
        if download.archive_key:
            storage.delete(download.archive_key)
        download.delete()
        count += 1
    return count
