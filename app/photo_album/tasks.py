import enum
import io
import logging
import posixpath
import subprocess  # nosec B404 — only to catch ffmpeg's CalledProcessError
from collections.abc import Buffer, Callable, Iterator
from dataclasses import asdict
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

from celery import Task, shared_task
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile, File
from django.core.management import call_command
from django.db.models import Q, QuerySet
from django.utils import timezone
from zipstream import ZIP_STORED, ZipStream

from . import direct_upload, merge, notify
from .dashboard import measure_storage
from .models import (
    ALBUM_DOWNLOAD_RETENTION,
    DERIVATIVE_CLAIM_TTL,
    JOB_QUEUED_TOO_LONG,
    JOB_STALLED_AFTER,
    Album,
    AlbumDownload,
    AlbumDownloadState,
    AlbumImport,
    AlbumImportState,
    AlbumMerge,
    AlbumMergeState,
    DerivativeState,
    DownloadVariant,
    Media,
)
from .progress import JobProgress
from .services import (
    MAX_DERIVATIVE_ATTEMPTS,
    Progress,
    backfill_hashes,
    backfill_sizes,
    build_derivatives,
    check_file_time_sources,
    ffmpeg_failure,
    import_zip_album,
    is_video_media,
    no_progress,
    pending_derivatives,
    reread_capture_dates,
    settle_all_dates,
    shrink_enlarged_video,
)
from .storage import PhotoAlbumS3Storage, get_photo_album_storage

logger = logging.getLogger(__name__)

# The two jobs here that legitimately outrun the global CELERY_TASK_TIME_LIMIT of fifteen minutes:
# an H.264 encode of a phone clip, and a whole-album ZIP streamed into object storage. Under the
# global limit both were SIGKILLed mid-work — a transcode left the media PENDING with its attempt
# counter bumped, so three passes marked a perfectly good file FAILED, and a ZIP left its download
# row wedged in BUILDING (see JOB_STALLED_AFTER).
#
# The SOFT limit is the one that does the work. It raises SoftTimeLimitExceeded *inside* the task,
# which both tasks already handle — `build_album_download` catches it in its `except Exception` and
# records FAILED with a message, rather than vanishing. The hard limit is only the backstop for a
# task wedged somewhere uninterruptible, five minutes later.
#
# Four hours: a long video on this box's few cores honestly takes more than the hour this used to
# be. It holds the one media slot for as long as it runs, so ZIP jobs queue behind it meanwhile.
# DERIVATIVE_CLAIM_TTL must stay equal to it.
MEDIA_TASK_TIME_LIMIT = 4 * 60 * 60
MEDIA_TASK_SOFT_TIME_LIMIT = MEDIA_TASK_TIME_LIMIT - 300

# Album ZIPs, both ways, may take as long: one Dropbox album can be a whole year. What
# tells a slow one from a dead one is its progress (JOB_STALLED_AFTER), not its age, so the limit
# here is only the ceiling for a job that keeps reporting yet never finishes.
ZIP_TASK_TIME_LIMIT = 4 * 60 * 60
ZIP_TASK_SOFT_TIME_LIMIT = ZIP_TASK_TIME_LIMIT - 300

# How long a failed import's ZIP is kept. Not deleted when the sweep fails the job: that is a guess
# about a worker, which may still be reading the file, or whose message may still be queued.
FAILED_IMPORT_ARCHIVE_KEPT = timedelta(days=1)

DOWNLOAD_INTERRUPTED = "Download blev afbrudt undervejs."
IMPORT_INTERRUPTED = "Importen blev afbrudt undervejs. Intet er importeret."
MERGE_INTERRUPTED = "Fletningen blev afbrudt undervejs. Intet er flettet."


class Claim(enum.Enum):
    """What `_claim_job` made of a job's row."""

    CLAIMED = "claimed"  # it is this task's to do
    NOTHING = "nothing"  # another task has it, or nobody needs doing
    LOST = "lost"  # this very message's first run died; just closed out as failed


def _claim_job(
    rows: "QuerySet[Any]",
    states: type[AlbumDownloadState] | type[AlbumImportState] | type[AlbumMergeState],
    task_id: str | None,
    *,
    interrupted: str,
) -> Claim:
    """Move a QUEUED download, import or merge to BUILDING for this task.

    A row `fail_stalled_*` failed while this very message was still waiting in the queue is claimed
    too: with one media slot a job can honestly wait longer than JOB_QUEUED_TOO_LONG. It is told
    from a job that died while building by never having recorded progress — claiming sets it.

    One thing a redelivery can tell. A message handed out again because its worker was lost
    (CELERY_TASK_REJECT_ON_WORKER_LOST) finds its row BUILDING under its OWN task id: that is this
    very message's first run, and nobody is coming back to it. It is closed out as failed on the
    spot rather than by `fail_stalled_*` a quarter of an hour later — and not run again, which would
    most likely just repeat whatever killed the worker.
    """
    now = timezone.now()
    waiting = Q(state=states.QUEUED)
    if task_id:
        waiting |= Q(state=states.FAILED, error=interrupted, task_id=task_id, progressed_at__isnull=True)
    if rows.filter(waiting).update(
        state=states.BUILDING,
        task_id=task_id or "",
        progress={},
        progressed_at=now,
        error="",
        completed_at=None,
    ):
        return Claim.CLAIMED
    if task_id and rows.filter(state=states.BUILDING, task_id=task_id).update(
        state=states.FAILED, error=interrupted, completed_at=now
    ):
        logger.warning("The worker running this job was lost (killed, out of memory?); marked as failed.")
        return Claim.LOST
    return Claim.NOTHING


def _stalled(
    rows: "QuerySet[Any]",
    states: type[AlbumDownloadState] | type[AlbumImportState] | type[AlbumMergeState],
) -> "QuerySet[Any]":
    """The jobs no worker is coming back to: no progress for JOB_STALLED_AFTER, or never started."""
    now = timezone.now()
    cutoff = now - JOB_STALLED_AFTER
    return rows.filter(
        Q(state=states.BUILDING, progressed_at__lt=cutoff)
        # A row from before progress was recorded, or one claimed without it.
        | Q(state=states.BUILDING, progressed_at__isnull=True, created_at__lt=cutoff)
        | Q(state=states.QUEUED, created_at__lt=now - JOB_QUEUED_TOO_LONG)
    )


def _unclaimed(queryset: "QuerySet[Media]") -> "QuerySet[Media]":
    """Rows no worker currently holds — never claimed, or claimed by one that never came back."""
    return queryset.filter(
        Q(derivative_started_at__isnull=True)
        | Q(derivative_started_at__lt=timezone.now() - DERIVATIVE_CLAIM_TTL)
    )


class DerivativesNotBuilt(Exception):
    """A media item's derivatives could not be built. Raised so the job shows as failed, with why."""


@shared_task(bind=True, time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def build_media_derivatives(self: Task, media_id: int) -> bool:
    """Build derivatives for one committed upload, if it still needs them and nobody else has it.

    Returns False only when there is nothing for it to do: the row is gone, ready, or another worker
    holds it. A file that could not be converted — or whose worker died converting it — RAISES, so
    the job is listed as failed rather than as a success that returned False, which is how a video
    could sit on "Mediet behandles" with nothing on the jobs page to say why.
    """
    task_id = self.request.id or ""
    # CLAIM FIRST, AS ONE STATEMENT. `UPDATE ... WHERE` takes the row lock, so of two workers handed
    # the same id exactly one sees a rowcount of 1 and the other backs off — which a read-then-write
    # could not promise. Whoever loses returns False rather than starting a second ffmpeg encode of
    # the same file and a second increment of `derivative_attempts`.
    claimed = _unclaimed(Media.objects.filter(pk=media_id, derivative_state=DerivativeState.PENDING)).update(
        derivative_started_at=timezone.now(), derivative_task_id=task_id
    )
    if not claimed:
        # Redelivered after its worker was killed (CELERY_TASK_REJECT_ON_WORKER_LOST): the row is
        # still claimed under this very message's id, and nobody is coming back to it. Release it
        # for the nightly `process_pending_media` — the dead run already counted its attempt.
        if task_id and Media.objects.filter(
            pk=media_id,
            derivative_state=DerivativeState.PENDING,
            derivative_task_id=task_id,
            derivative_started_at__isnull=False,
        ).update(derivative_started_at=None):
            raise DerivativesNotBuilt(
                f"The worker converting media {media_id} was lost (killed — out of memory, or a "
                "redeploy). It is tried again by the nightly process_pending_media."
            )
        return False
    try:
        media = Media.objects.get(pk=media_id)
    except Media.DoesNotExist:
        return False
    logger.info("Converting %s, in %s.", media.display_name_and_size, media.album)
    if build_derivatives(media):
        return True
    if media.derivative_state == DerivativeState.FAILED:
        raise DerivativesNotBuilt(
            f"{media.display_name_and_size} could not be converted; gave up after {media.derivative_attempts} attempts."
        )
    raise DerivativesNotBuilt(
        f"{media.display_name_and_size} could not be converted (attempt {media.derivative_attempts} of "
        f"{MAX_DERIVATIVE_ATTEMPTS}); tried again by the nightly process_pending_media."
    )


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def reencode_enlarged_video(media_id: int) -> str:
    """Re-encode one video's viewer copy at the video's own size, if an earlier conversion enlarged
    it to 1600 px. Queued by `manage.py reencode_enlarged_photo_album_videos`."""
    try:
        media = Media.objects.select_related("album").get(pk=media_id, derivative_state=DerivativeState.READY)
    except Media.DoesNotExist:
        return f"Media {media_id} is gone or not ready; left as it is."
    logger.info("Checking %s, in %s.", media.display_name_and_size, media.album)
    try:
        outcome = shrink_enlarged_video(media)
    except subprocess.CalledProcessError as exc:
        raise DerivativesNotBuilt(
            f"{media.display_name_and_size} could not be re-encoded ({ffmpeg_failure(exc)}); its old copy is kept."
        ) from exc
    logger.info("%s", outcome)
    return outcome


def queue_derivatives(media: Media) -> None:
    """Queue `build_media_derivatives` for one item: a video's encode on "media", a photo on
    "thumbnails".

    A photo is half a second of Pillow and the uploader is waiting to see it, so it must not queue
    behind encodes and ZIP jobs. Its own queue, rather than "celery", is what keeps the quick jobs
    quick in turn: `worker` takes from "celery" and "thumbnails" by turns, so a mail sent during an
    import of 400 photos waits for one thumbnail, not for all of them.
    """
    queue = "media" if is_video_media(media) else "thumbnails"
    build_media_derivatives.apply_async((media.pk,), queue=queue)


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
        queue_derivatives(media)
        dispatched += 1
    return dispatched


# How long one night's hash backfill may read originals for. Old rows are hashed in portions, so
# the first nights after a deploy never hold the worker for long; once every row has its hash, a
# run is one indexed query that finds nothing.
HASH_BACKFILL_BUDGET = timedelta(minutes=20)


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def backfill_media_hashes() -> int:
    """Nightly: hash originals stored before hashes were, then record the size of those stored
    before sizes were (a HEAD each), in what is left of the budget. Returns how many were hashed."""
    until = timezone.now() + HASH_BACKFILL_BUDGET
    result = backfill_hashes(until=until)
    logger.info(
        "Hashed %d media item(s); %d could not be read; %d still without a hash.",
        result.hashed,
        len(result.unreadable),
        result.remaining,
    )
    sizes = backfill_sizes(until=until)
    logger.info(
        "Recorded the size of %d media item(s); %d could not be read; %d still without a size.",
        sizes.sized,
        sizes.unreadable,
        sizes.remaining,
    )
    return result.hashed


# How long one night may read originals again for a capture date, for the same reason.
DATE_REREAD_BUDGET = timedelta(minutes=20)
# And sidecars, to tell where the file times stored before that was recorded came from. A sidecar
# is a few hundred bytes, so the archive's thousands are read in one night.
FILE_TIME_CHECK_BUDGET = timedelta(minutes=10)


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def settle_media_dates() -> int:
    """Nightly: read originals stored before XMP and IPTC were read for a capture date, and the
    sidecars of files whose time's source is not known yet, then give every item without a
    believable date an approximate one (services.settle_dates). Returns how many were re-dated.

    Every album is settled, not only those read tonight: that is what dates the items stored before
    approximations existed. Once they are, it changes nothing, at two queries per album tree.
    """
    reread = reread_capture_dates(until=timezone.now() + DATE_REREAD_BUDGET)
    sources = check_file_time_sources(until=timezone.now() + FILE_TIME_CHECK_BUDGET)
    changed = settle_all_dates()
    logger.info(
        "Read %d original(s) again and found %d capture date(s); %d could not be read; %d left. "
        "Read %d sidecar(s) for where a file time is from, %d of them its source; %d could not be "
        "read; %d left. %d item(s) re-dated.",
        reread.checked,
        reread.found,
        len(reread.unreadable),
        reread.remaining,
        sources.checked,
        sources.from_sidecar,
        len(sources.unreadable),
        sources.remaining,
        changed,
    )
    return changed


@shared_task(time_limit=MEDIA_TASK_TIME_LIMIT, soft_time_limit=MEDIA_TASK_SOFT_TIME_LIMIT)
def measure_photo_album_storage() -> int:
    """Nightly, and when the dashboard asks: what the album takes up in the bucket. Returns the bytes."""
    report = measure_storage()
    logger.info(
        "Measured the photo album's storage: %d byte(s) in %d object(s), in %.1f s.",
        report.total_bytes,
        sum(entry.get("objects", 0) for entry in report.totals.values()),
        report.seconds,
    )
    return report.total_bytes


@shared_task
def purge_expired_media() -> None:
    """Permanently remove photo-album media past its retention period."""
    call_command("purge_photo_album")


def _chunks(file_field: File, still_working: Callable[[], None] = lambda: None) -> Iterator[bytes]:
    with file_field.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            still_working()
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


def _archive(download: AlbumDownload, progress: Progress = no_progress) -> ZipStream:
    archive = ZipStream(compress_type=ZIP_STORED)
    entries, renamed = archive_entries(download)
    for index, (arcname, _, source) in enumerate(entries):
        still_working = partial(progress, "zipping", index, len(entries))
        archive.add(_chunks(source, still_working), arcname=arcname)
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


@shared_task(bind=True, time_limit=ZIP_TASK_TIME_LIMIT, soft_time_limit=ZIP_TASK_SOFT_TIME_LIMIT)
def build_album_download(self: Task, download_id: int) -> bool:
    """Build an album ZIP into object storage, without occupying a web worker."""
    rows = AlbumDownload.objects.filter(pk=download_id)
    if (
        _claim_job(rows, AlbumDownloadState, self.request.id, interrupted=DOWNLOAD_INTERRUPTED)
        is not Claim.CLAIMED
    ):
        return False
    download = rows.get()
    key = f"photo-album-zips/{download.token}.zip"
    logger.info(
        "Building %s (%s) with %d item(s) for %s.",
        download.filename,
        download.variant,
        len(download.media_ids),
        download.requested_by,
    )
    try:
        archive = _archive(download, JobProgress(rows))
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
    # Unconditionally READY, even over a FAILED that `fail_stalled_downloads` wrote meanwhile: the
    # ZIP is there, so that was the wrong guess.
    download.state = AlbumDownloadState.READY
    download.archive_key = key
    download.error = ""
    download.completed_at = timezone.now()
    download.save(update_fields=["state", "archive_key", "error", "completed_at"])
    return True


@shared_task
def fail_stalled_downloads() -> int:
    """Close out ZIP builds no worker is coming back to, so the page stops waiting on them.

    A worker killed outright — OOM, hard time limit, a redeploy mid-build — runs no exception
    handler, so the row it was working on keeps `state = BUILDING` and `completed_at = NULL`. Two
    things then never happen: `purge_expired_downloads` filters on `completed_at`, so the row is
    never collected; and photo_album/detail.html keeps drawing the "Download klargøres …" branch,
    whose five-second reload then runs for as long as the resident leaves that page open.

    Setting `completed_at` here is what hands the row back to `purge_expired_downloads`, so this
    ends the reload loop AND lets the row be collected on the ordinary seven-day schedule.
    """
    return _stalled(AlbumDownload.objects.all(), AlbumDownloadState).update(
        state=AlbumDownloadState.FAILED,
        error=DOWNLOAD_INTERRUPTED,
        completed_at=timezone.now(),
    )


def _discard_archives(imports: "QuerySet[AlbumImport]") -> None:
    """Delete the uploaded ZIPs of finished imports; the task's own `finally` never ran for these."""
    for album_import in imports.exclude(archive=""):
        album_import.archive.delete(save=False)
        album_import.save(update_fields=["archive"])


def _tell_import_failed(album_import: AlbumImport, reason: str) -> None:
    """The notification, never in the way of what the task is doing: a failure to send it is
    logged, not raised over the import's own outcome."""
    try:
        notify.import_failed(album_import, reason)
    except Exception:
        logger.warning(
            "Could not tell %s that import %s failed",
            album_import.requested_by_id,
            album_import.pk,
            exc_info=True,
        )


# How long a finished upload may wait for its import before it counts as abandoned: past the 24 h a
# ticket is good for (direct_upload.TTL), so no upload that could still be submitted is taken.
ORPHANED_IMPORT_AFTER = timedelta(days=2)


@shared_task
def purge_orphaned_import_archives() -> int:
    """Daily: delete uploaded import ZIPs no import refers to (direct_upload.purge_orphaned_imports).

    An import's own ZIP is deleted when it finishes (process_album_import) or a day after it fails
    (fail_stalled_imports); this is for the ones that never became an import at all.
    """
    referenced = set(AlbumImport.objects.exclude(archive="").values_list("archive", flat=True))
    deleted = direct_upload.purge_orphaned_imports(
        referenced, older_than=timezone.now() - ORPHANED_IMPORT_AFTER
    )
    for key in deleted:
        logger.info("Deleted %s, which no import refers to.", key)
    return len(deleted)


@shared_task
def fail_stalled_imports() -> int:
    """Close out ZIP imports no worker is coming back to, and delete the ZIPs they left behind.

    The same hole as `fail_stalled_downloads`, from the other side: the Dropbox script asks for an
    import's status until it is ready or failed, and a row left BUILDING kept it asking "still
    building" for good. Nothing of the import itself is left to clean up — it is stored in one
    transaction, which died with the worker.

    The ZIPs go FAILED_IMPORT_ARCHIVE_KEPT later, not with the jobs failed here: a worker still
    downloading one from the bucket would lose it, and a job still queued could not be revived.
    """
    # Which were running and stopped, and which never started — told apart for the notification.
    started = dict(_stalled(AlbumImport.objects.all(), AlbumImportState).values_list("pk", "progressed_at"))
    failed = _stalled(AlbumImport.objects.filter(pk__in=started), AlbumImportState).update(
        state=AlbumImportState.FAILED, error=IMPORT_INTERRUPTED, completed_at=timezone.now()
    )
    for album_import in AlbumImport.objects.filter(
        pk__in=started, state=AlbumImportState.FAILED, error=IMPORT_INTERRUPTED
    ).select_related("requested_by"):
        _tell_import_failed(
            album_import, notify.INTERRUPTED if started[album_import.pk] else notify.NEVER_STARTED
        )
    _discard_archives(
        AlbumImport.objects.filter(
            state=AlbumImportState.FAILED, completed_at__lt=timezone.now() - FAILED_IMPORT_ARCHIVE_KEPT
        )
    )
    return failed


@shared_task(bind=True, time_limit=ZIP_TASK_TIME_LIMIT, soft_time_limit=ZIP_TASK_SOFT_TIME_LIMIT)
def process_album_import(self: Task, import_id: int) -> bool:
    """Unpack a stored ZIP outside the web request that received it."""
    rows = AlbumImport.objects.filter(pk=import_id)
    claim = _claim_job(rows, AlbumImportState, self.request.id, interrupted=IMPORT_INTERRUPTED)
    if claim is Claim.LOST:
        # This message's own dead first run, just closed out; nothing will run it now.
        _discard_archives(rows)
        _tell_import_failed(rows.get(), notify.INTERRUPTED)
    if claim is not Claim.CLAIMED:
        return False
    album_import = rows.get()
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
                root_name=album_import.album_name,
                progress=JobProgress(rows),
            )
        # READY even over a FAILED that `fail_stalled_imports` wrote meanwhile: the albums are in.
        album_import.state = AlbumImportState.READY
        album_import.error = ""
        album_import.album_ids = [album.pk for album in result.albums]
        album_import.skipped = result.skipped
        album_import.merged = result.merged
        album_import.completed_at = timezone.now()
        album_import.save(update_fields=["state", "album_ids", "skipped", "merged", "error", "completed_at"])
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
        _tell_import_failed(album_import, notify.refused(exc.messages))
        return False
    except Exception as exc:
        album_import.state = AlbumImportState.FAILED
        album_import.error = str(exc)[:255]
        album_import.completed_at = timezone.now()
        album_import.save(update_fields=["state", "error", "completed_at"])
        _tell_import_failed(album_import, notify.CRASHED)
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


@shared_task(bind=True, time_limit=ZIP_TASK_TIME_LIMIT, soft_time_limit=ZIP_TASK_SOFT_TIME_LIMIT)
def process_album_merge(self: Task, merge_id: int) -> bool:
    """Merge one album into another outside the request that asked for it (photo_album.merge).

    As long as an import may take: two albums of a whole year, imported twice, is every original
    read twice to compare them.
    """
    rows = AlbumMerge.objects.filter(pk=merge_id)
    if _claim_job(rows, AlbumMergeState, self.request.id, interrupted=MERGE_INTERRUPTED) is not Claim.CLAIMED:
        return False
    job = rows.select_related("target", "source").get()
    logger.info("Merging %s into %s for %s.", job.source_name, job.target, job.requested_by)
    try:
        if job.source is None:
            raise ValidationError(f"{job.source_name} findes ikke længere.")
        result = merge.merge_albums(job.target, job.source, progress=JobProgress(rows))
    except ValidationError as exc:
        # Refused, not crashed: an album was locked or moved meanwhile, and nothing was merged.
        logger.warning("Refused: %s", " ".join(exc.messages))
        rows.update(
            state=AlbumMergeState.FAILED, error=" ".join(exc.messages)[:255], completed_at=timezone.now()
        )
        return False
    except Exception:
        rows.update(
            state=AlbumMergeState.FAILED,
            error="Fletningen mislykkedes. Intet er flettet.",
            completed_at=timezone.now(),
        )
        raise
    # By update, not save: the merge deleted the source album, which the row in memory still names.
    rows.update(state=AlbumMergeState.READY, result=asdict(result), error="", completed_at=timezone.now())
    logger.info("Merged %s into %s: %s.", job.source_name, job.target, merge.describe(asdict(result)))
    return True


@shared_task
def fail_stalled_merges() -> int:
    """Close out album merges no worker is coming back to. A merge changes the albums in one
    transaction, which died with the worker, so there is nothing of it to undo."""
    return _stalled(AlbumMerge.objects.all(), AlbumMergeState).update(
        state=AlbumMergeState.FAILED, error=MERGE_INTERRUPTED, completed_at=timezone.now()
    )
