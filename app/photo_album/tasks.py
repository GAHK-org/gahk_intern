import io
import logging
import posixpath
from collections.abc import Buffer, Iterator
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.core.management import call_command
from django.db.models import Q, QuerySet
from django.utils import timezone
from zipstream import ZIP_STORED, ZipStream

from .models import (
    ALBUM_DOWNLOAD_RETENTION,
    ALBUM_DOWNLOAD_STALE_AFTER,
    DERIVATIVE_CLAIM_TTL,
    Album,
    AlbumDownload,
    AlbumDownloadState,
    AlbumImport,
    AlbumImportState,
    DerivativeState,
    DownloadVariant,
    Media,
)
from .services import backfill_hashes, build_derivatives, import_zip_album, pending_derivatives
from .storage import PhotoAlbumS3Storage, get_photo_album_storage

logger = logging.getLogger(__name__)

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


# How long one night's hash backfill may read originals for. Old rows are hashed in portions, so
# the first nights after a deploy never hold the worker for long; once every row has its hash, a
# run is one indexed query that finds nothing.
HASH_BACKFILL_BUDGET = timedelta(minutes=20)


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def backfill_media_hashes() -> int:
    """Nightly: hash originals stored before hashes were. Returns how many were hashed."""
    result = backfill_hashes(until=timezone.now() + HASH_BACKFILL_BUDGET)
    logger.info(
        "Hashed %d media item(s); %d could not be read; %d still without a hash.",
        result.hashed,
        len(result.unreadable),
        result.remaining,
    )
    return result.hashed


@shared_task
def purge_expired_media() -> None:
    """Permanently remove photo-album media past its retention period."""
    call_command("purge_photo_album")


def _chunks(file_field: File) -> Iterator[bytes]:
    with file_field.open("rb") as source:
        while chunk := source.read(1024 * 1024):
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


# The leading underscore sorts it first in most file browsers, ahead of the folders and pictures.
RENAMED_NOTICE_NAME = "_OMDØBTE_FILER.txt"


def archive_entries(download: AlbumDownload) -> tuple[list[tuple[str, Media, File]], list[tuple[str, str]]]:
    """Where each file goes in the ZIP, and which files had to be renamed to get there.

    The folders mirror the album tree: below the downloaded album when there is one, or from the
    folder (year) down for a selection, which can span any number of albums. Files keep their
    original names. Several items in one album may share a name — two phones' IMG_0001.JPG — and so
    may an original and its HD copy's new extension; every member of such a group, compared without
    case as Windows and macOS would, is numbered `IMG_0001_01.JPG`, `IMG_0001_02.JPG`, … and listed
    in _OMDØBTE_FILER.txt.
    """
    media_items = list(
        Media.objects.filter(pk__in=download.media_ids, deleted_at__isnull=True)
        .select_related("album")
        .order_by("sort_at", "pk")
    )
    album_ids = {
        int(part) for media in media_items for part in media.album.path.strip("/").split("/") if part
    }
    names = dict(Album.objects.filter(pk__in=album_ids).values_list("pk", "name"))
    base_ids: list[int] = []
    if download.album is not None:
        base_ids = [int(part) for part in download.album.path.strip("/").split("/") if part]

    planned: list[tuple[str, str, Media, File]] = []  # (folder path, file name, media, file)
    for media in media_items:
        ids = [int(part) for part in media.album.path.strip("/").split("/") if part]
        if base_ids and ids[: len(base_ids)] == base_ids:
            folders = [names.get(pk, str(pk)) for pk in ids[len(base_ids) :]]
        else:
            folders = [media.album.folder, *(names.get(pk, str(pk)) for pk in ids)]
        filename = media.display_name or f"media-{media.pk}"
        source: File = media.original
        if (
            download.variant == DownloadVariant.HIGH_DEFINITION
            and media.has_derivatives
            and media.high_definition
        ):
            source = media.high_definition
            filename = f"{Path(filename).stem}{Path(media.high_definition.name or '').suffix or '.jpg'}"
        planned.append(("/".join(folders), filename, media, source))

    groups: dict[str, list[int]] = {}
    for index, (folder, filename, _, _) in enumerate(planned):
        groups.setdefault(posixpath.join(folder, filename).lower(), []).append(index)
    used = {path for path, members in groups.items() if len(members) == 1}
    renamed: list[tuple[str, str]] = []
    final_names: dict[int, str] = {}
    for members in groups.values():
        if len(members) == 1:
            folder, filename, _, _ = planned[members[0]]
            final_names[members[0]] = posixpath.join(folder, filename)
            continue
        number = 0
        for index in members:
            folder, filename, _, _ = planned[index]
            stem, suffix = posixpath.splitext(filename)
            while True:
                number += 1
                candidate = posixpath.join(folder, f"{stem}_{number:02d}{suffix}")
                if candidate.lower() not in used:
                    break
            used.add(candidate.lower())
            final_names[index] = candidate
            renamed.append((posixpath.join(folder, filename), candidate))
    entries = [(final_names[index], media, source) for index, (_, _, media, source) in enumerate(planned)]
    return entries, renamed


def _archive(download: AlbumDownload) -> ZipStream:
    archive = ZipStream(compress_type=ZIP_STORED)
    entries, renamed = archive_entries(download)
    for arcname, _, source in entries:
        archive.add(_chunks(source), arcname=arcname)
    if renamed:
        lines = [
            "Nogle filer havde samme navn i samme mappe og er derfor blevet nummereret:",
            "",
            *(f"{original}  ->  {new}" for original, new in renamed),
            "",
        ]
        archive.add("\r\n".join(lines).encode("utf-8"), arcname=RENAMED_NOTICE_NAME)
        logger.info("%d file(s) renamed to avoid a clash; listed in %s.", len(renamed), RENAMED_NOTICE_NAME)
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
    logger.info(
        "Building %s (%s) with %d item(s) for %s.",
        download.filename,
        download.variant,
        len(download.media_ids),
        download.requested_by,
    )
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


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def process_album_import(import_id: int) -> bool:
    """Unpack a stored ZIP outside the web request that received it."""
    try:
        album_import = AlbumImport.objects.get(pk=import_id, state=AlbumImportState.QUEUED)
    except AlbumImport.DoesNotExist:
        return False
    album_import.state = AlbumImportState.BUILDING
    album_import.save(update_fields=["state"])
    logger.info(
        "Importing %s into folder %s for %s%s%s.",
        album_import.archive_name,
        album_import.folder,
        album_import.requested_by,
        " (locked when done)" if album_import.lock_when_done else "",
        " (merged into an album of the same name)" if album_import.merge_into_existing else "",
    )
    try:
        with album_import.archive.open("rb") as archive:
            result = import_zip_album(
                archive=File(archive, name=album_import.archive_name),
                folder=album_import.folder,
                resident=album_import.requested_by,
                lock=album_import.lock_when_done,
                merge=album_import.merge_into_existing,
            )
        album_import.state = AlbumImportState.READY
        album_import.album_ids = [album.pk for album in result.albums]
        album_import.skipped = result.skipped
        album_import.merged = result.merged
        album_import.completed_at = timezone.now()
        album_import.save(update_fields=["state", "album_ids", "skipped", "merged", "completed_at"])
        for line in result.skipped:
            logger.info("Left out: %s", line)
        logger.info(
            "%s %d media item(s) into %d album(s): %s. %d file(s) left out.",
            "Merged" if result.merged else "Imported",
            result.added,
            len(result.albums),
            ", ".join(album.name for album in result.albums),
            len(result.skipped),
        )
        return True
    except ValidationError as exc:
        # Refused, not crashed: the archive had problems the resident can fix, and nothing of it
        # was stored. Every reason is kept so the status page can list them all at once.
        for message in exc.messages:
            logger.warning("Refused: %s", message)
        logger.warning("The ZIP was refused (%d reason(s)); nothing was imported.", len(exc.messages))
        album_import.state = AlbumImportState.FAILED
        album_import.errors = exc.messages
        album_import.error = "ZIP-filen blev afvist. Intet er importeret."
        album_import.completed_at = timezone.now()
        album_import.save(update_fields=["state", "errors", "error", "completed_at"])
        return False
    except Exception as exc:
        album_import.state = AlbumImportState.FAILED
        album_import.error = str(exc)[:255]
        album_import.completed_at = timezone.now()
        album_import.save(update_fields=["state", "error", "completed_at"])
        raise
    finally:
        if album_import.archive:
            album_import.archive.delete(save=False)
            album_import.save(update_fields=["archive"])


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
