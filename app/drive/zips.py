"""Several files, or whole folders, downloaded as one zip of at most DRIVE_MAX_ZIP_GB.

The zip is built in the background on the "media" worker, as the photo album's are, and goes into the
bucket, from where it is downloaded as any file is: a 5 GB zip streamed through the app would hold a
web worker for as long as the download takes, and the proxy cuts a request that takes more than a
minute.

What goes in is decided when the zip is asked for (`plan`), from what the resident can read then:
subfolders they cannot open are left out without a trace, as are files in the bin. Fetching the zip
checks again that they can still read every file in it, so a zip is not a way to keep what a resident
has since lost access to. Only the one who asked can fetch it, for ZIP_KEEP_HOURS.

Names in a folder are unique ignoring case, so the paths in a zip never clash. Files are stored, not
compressed: most of what is uploaded is already compressed, and storing costs the worker nothing.

The build reports its progress, by bytes read against the zip's size (core/job_progress.py): the
worker-jobs page shows it, and the watchdog stops one that gets nowhere, so the time limit can be the
album ZIPs' twelve hours. Whether a build has died is read from the worker's own row for it
(WorkerJobLog, under the task id `task_id` gives it), as the album's are from their progress.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from zipstream import ZIP_STORED, ZipStream

from core.job_progress import LiveJob, current, stage, stopped_reason
from core.models import WorkerJobLog
from photo_album.models import JOB_QUEUED_TOO_LONG, JOB_STALLED_AFTER
from photo_album.storage import PhotoAlbumS3Storage
from photo_album.tasks import _ZipReader
from residents.models import Resident

from . import services
from .access import Level, Viewer
from .models import File, FileVersion, Folder, ZipDownload
from .operations import Selection

ZIP_PREFIX = "drive/zips/"
ZIP_KEEP_HOURS = 24
ZIPPING = stage(0.0, 1.0, "Pakker zip'en")


def max_zip_bytes() -> int:
    return int(settings.DRIVE_MAX_ZIP_GB * 1024**3)


@dataclass
class Plan:
    entries: list[dict[str, object]]
    size: int

    @property
    def file_count(self) -> int:
        return sum(1 for entry in self.entries if "key" in entry)


def _latest_keys(file_ids: list[int]) -> dict[int, str]:
    latest: dict[int, tuple[int, str]] = {}
    for file_id, number, key in FileVersion.objects.filter(file_id__in=file_ids).values_list(
        "file_id", "number", "key"
    ):
        if file_id not in latest or number > latest[file_id][0]:
            latest[file_id] = (number, key)
    return {file_id: key for file_id, (_, key) in latest.items()}


def plan(viewer: Viewer, selection: Selection) -> Plan:
    """Where each readable file goes in the zip: a ticked file at the top, a ticked folder as a
    folder with everything below it that the viewer can open."""
    files: list[tuple[str, File]] = [(file.name, file) for file in selection.files]
    folders: list[str] = []
    for top in selection.folders:
        if viewer.level(top) < Level.READ:
            continue
        below = sorted(
            Folder.objects.filter(path__startswith=top.path, deleted_at__isnull=True),
            key=lambda folder: len(folder.path),
        )
        paths: dict[int, str] = {}
        for folder in below:
            parent_path = "" if folder.pk == top.pk else paths.get(folder.parent_id or 0)
            if parent_path is None or viewer.level(folder) < Level.READ:
                continue  # it, and everything below it, is left out
            paths[folder.pk] = f"{parent_path}{folder.name}/"
        inside = list(File.objects.filter(folder_id__in=paths, deleted_at__isnull=True).order_by("name_key"))
        for file in inside:
            files.append((paths[file.folder_id] + file.name, file))
        with_files = {file.folder_id for file in inside}
        # A folder with nothing in it, and no folder below it, is kept as an empty folder.
        has_children = {folder.parent_id for folder in below if folder.pk in paths}
        folders += [path for pk, path in paths.items() if pk not in with_files and pk not in has_children]
    keys = _latest_keys([file.pk for _, file in files])
    entries: list[dict[str, object]] = [
        {"path": path, "key": keys[file.pk], "file": file.pk} for path, file in files if file.pk in keys
    ]
    entries += [{"path": path} for path in sorted(folders)]
    return Plan(entries, sum(file.size for _, file in files if file.pk in keys))


def request(viewer: Viewer, selection: Selection, *, name: str, actor: Resident) -> ZipDownload:
    found = plan(viewer, selection)
    if not found.file_count:
        raise ValidationError("Der er ingen filer at hente.")
    if found.size > max_zip_bytes():
        raise ValidationError(
            f"Det er for meget til én zip (over {settings.DRIVE_MAX_ZIP_GB:g} GB). Vælg færre ad gangen."
        )
    download = ZipDownload.objects.create(
        requested_by=actor,
        folder=selection.source,
        name=f"{name}.zip",
        entries=found.entries,
        size=found.size,
    )
    from .tasks import build_zip  # tasks imports this module

    transaction.on_commit(lambda: build_zip.apply_async((download.pk,), task_id=task_id(download)))
    return download


def task_id(download: ZipDownload) -> str:
    """The build's Celery task id, and so its WorkerJobLog's: one build per zip."""
    return f"drive-zip-{download.token}"


class _Progress:
    """Bytes read so far against the zip's size. Called from boto3's reading thread, so it holds the
    job taken in the task's own."""

    def __init__(self, total: int) -> None:
        self.total, self.done = total, 0
        self.job: LiveJob | None = current()
        ZIPPING(0.0, self.job)

    def __call__(self, read: int) -> None:
        self.done += read
        ZIPPING(self.done / self.total if self.total else 1.0, self.job)


def _chunks(key: str, progress: _Progress) -> Iterator[bytes]:
    with services.storage().open(key, "rb") as source:
        while chunk := source.read(1024 * 1024):
            progress(len(chunk))
            yield chunk


def _failure(exc: Exception) -> str:
    """The build's error. A stopped build's exception says nothing of its own."""
    if isinstance(exc, SoftTimeLimitExceeded):
        return "Stoppet, fordi jobbet ikke kom videre." if stopped_reason() else "Stoppet ved tidsgrænsen."
    return str(exc)[:255] or type(exc).__name__


def build(download_id: int) -> bool:
    claimed = ZipDownload.objects.filter(pk=download_id, state=ZipDownload.State.QUEUED).update(
        state=ZipDownload.State.BUILDING
    )
    if not claimed:
        return False
    download = ZipDownload.objects.get(pk=download_id)
    key = f"{ZIP_PREFIX}{download.token}.zip"
    archive = ZipStream(compress_type=ZIP_STORED)
    progress = _Progress(download.size)
    for entry in download.entries:
        if "key" in entry:
            archive.add(_chunks(str(entry["key"]), progress), arcname=str(entry["path"]))
        else:
            archive.mkdir(str(entry["path"]))
    store = services.storage()
    try:
        if isinstance(store, PhotoAlbumS3Storage):
            store.bucket.upload_fileobj(
                _ZipReader(archive), key, ExtraArgs={"ContentType": "application/zip"}
            )
        else:
            store.save(key, ContentFile(b"".join(archive)))
    except Exception as exc:
        ZipDownload.objects.filter(pk=download.pk).update(
            state=ZipDownload.State.FAILED, error=_failure(exc), completed_at=timezone.now()
        )
        raise  # the job fails, with its traceback on the worker-jobs page
    ZipDownload.objects.filter(pk=download.pk).update(
        state=ZipDownload.State.READY, archive_key=key, completed_at=timezone.now()
    )
    return True


def stalled(download: ZipDownload) -> bool:
    """No worker is coming back to it. Queued: not taken in JOB_QUEUED_TOO_LONG, since the media
    queue runs one job at a time and a zip may honestly wait behind a twelve-hour encode. Building:
    its job ended without finishing it (a worker killed outright runs no handler), or has shown no
    sign of life - progress, or else its start - for JOB_STALLED_AFTER."""
    now = timezone.now()
    if download.state == ZipDownload.State.QUEUED:
        return download.created_at < now - JOB_QUEUED_TOO_LONG
    if download.state != ZipDownload.State.BUILDING:
        return False
    job = WorkerJobLog.objects.filter(task_id=task_id(download)).first()
    if job is None:  # its start went unrecorded: only the zip's own age to go by
        return download.created_at < now - JOB_STALLED_AFTER
    return job.finished_at is not None or (job.progressed_at or job.started_at) < now - JOB_STALLED_AFTER


def still_readable(viewer: Viewer, download: ZipDownload) -> bool:
    """The resident can still read every file in the zip that is still there."""
    ids = [int(entry["file"]) for entry in download.entries if "file" in entry]
    return all(
        viewer.file_level(file) >= Level.READ
        for file in File.objects.filter(pk__in=ids).select_related("folder")
    )


def expire() -> int:
    """Daily: zips older than ZIP_KEEP_HOURS go, with their bytes."""
    old = list(ZipDownload.objects.filter(created_at__lt=timezone.now() - timedelta(hours=ZIP_KEEP_HOURS)))
    store = services.storage()
    for download in old:
        if download.archive_key:
            store.delete(download.archive_key)
    ZipDownload.objects.filter(pk__in=[download.pk for download in old]).delete()
    return len(old)
