"""Background ZIP jobs: progress, telling a slow job from a dead one, and where jobs run."""

import subprocess
import threading
import zipfile
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album import progress
from photo_album.models import (
    JOB_QUEUED_TOO_LONG,
    JOB_STALLED_AFTER,
    AlbumDownload,
    AlbumDownloadState,
    AlbumImport,
    AlbumImportState,
    DerivativeState,
    Media,
)
from photo_album.progress import JobProgress
from photo_album.services import MAX_DERIVATIVE_ATTEMPTS
from photo_album.storage import PhotoAlbumS3Storage, get_photo_album_storage
from photo_album.tasks import (
    DOWNLOAD_INTERRUPTED,
    FAILED_IMPORT_ARCHIVE_KEPT,
    IMPORT_INTERRUPTED,
    DerivativesNotBuilt,
    build_album_download,
    build_media_derivatives,
    fail_stalled_downloads,
    fail_stalled_imports,
    process_album_import,
)
from residents.models import Resident

from .media_samples import jpeg_bytes


def _import(resident: Resident, content: bytes = b"not read", **fields: object) -> AlbumImport:
    album_import = AlbumImport.objects.create(
        requested_by=resident, folder="2026", archive_name="fest.zip", **fields
    )
    album_import.archive.save("fest.zip", ContentFile(content), save=True)
    return album_import


def _zip_with_one_photo() -> bytes:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("one.jpg", jpeg_bytes())
    return payload.getvalue()


@pytest.mark.django_db
def test_an_import_whose_worker_never_returned_is_failed_and_its_zip_deleted_a_day_later(
    make_resident: Callable[..., Resident],
) -> None:
    """The Dropbox script asks for the status until ready or failed, so a row left BUILDING by a
    worker the OOM killer took kept it waiting on "still building" for good."""
    resident = make_resident()
    stalled = _import(
        resident,
        state=AlbumImportState.BUILDING,
        progressed_at=timezone.now() - JOB_STALLED_AFTER - timedelta(minutes=1),
    )
    working = _import(resident, state=AlbumImportState.BUILDING, progressed_at=timezone.now())
    AlbumImport.objects.filter(pk=working.pk).update(created_at=timezone.now() - timedelta(hours=3))
    archive_key = stalled.archive.name

    assert fail_stalled_imports.run() == 1

    stalled.refresh_from_db()
    working.refresh_from_db()
    assert stalled.state == AlbumImportState.FAILED
    assert stalled.error == IMPORT_INTERRUPTED
    assert stalled.completed_at is not None
    # Three hours in, but it reported progress a moment ago: slow, not dead.
    assert working.state == AlbumImportState.BUILDING
    assert working.archive
    # Failing it is a guess about a worker that may still be reading the ZIP, so it stays a while.
    assert stalled.archive
    assert get_photo_album_storage().exists(archive_key)

    AlbumImport.objects.filter(pk=stalled.pk).update(
        completed_at=timezone.now() - FAILED_IMPORT_ARCHIVE_KEPT - timedelta(minutes=1)
    )
    assert fail_stalled_imports.run() == 0

    stalled.refresh_from_db()
    assert not stalled.archive
    assert not get_photo_album_storage().exists(archive_key)
    assert AlbumImport.objects.get(pk=working.pk).archive


@pytest.mark.django_db
def test_a_download_failed_while_still_queued_is_built_when_its_message_arrives(
    make_resident: Callable[..., Resident],
) -> None:
    """With one media slot, a ZIP can honestly wait longer than JOB_QUEUED_TOO_LONG. The sweep
    failed it, and the message that finally arrived found no QUEUED row: no ZIP, ever."""
    download = AlbumDownload.objects.create(requested_by=make_resident(), media_ids=[], task_id="waited")
    AlbumDownload.objects.filter(pk=download.pk).update(
        created_at=timezone.now() - JOB_QUEUED_TOO_LONG - timedelta(minutes=1)
    )
    assert fail_stalled_downloads.run() == 1

    assert build_album_download.apply(args=(download.pk,), task_id="waited").get() is True

    download.refresh_from_db()
    assert download.state == AlbumDownloadState.READY
    assert download.error == ""


@pytest.mark.django_db
def test_an_import_failed_while_still_queued_is_imported_when_its_message_arrives(
    make_resident: Callable[..., Resident],
) -> None:
    album_import = _import(make_resident(), _zip_with_one_photo(), task_id="waited")
    AlbumImport.objects.filter(pk=album_import.pk).update(
        created_at=timezone.now() - JOB_QUEUED_TOO_LONG - timedelta(minutes=1)
    )
    assert fail_stalled_imports.run() == 1

    assert process_album_import.apply(args=(album_import.pk,), task_id="waited").get() is True

    album_import.refresh_from_db()
    assert album_import.state == AlbumImportState.READY
    assert album_import.error == ""
    assert len(album_import.album_ids) == 1


@pytest.mark.django_db
def test_a_job_that_died_while_building_is_not_revived(make_resident: Callable[..., Resident]) -> None:
    """Only a job that never started is revived. One that recorded progress was running when its
    worker died, and running it again would most likely just repeat whatever killed it."""
    download = AlbumDownload.objects.create(
        requested_by=make_resident(),
        media_ids=[],
        task_id="died",
        state=AlbumDownloadState.FAILED,
        error=DOWNLOAD_INTERRUPTED,
        progressed_at=timezone.now() - timedelta(hours=1),
    )

    assert build_album_download.apply(args=(download.pk,), task_id="died").get() is False

    download.refresh_from_db()
    assert download.state == AlbumDownloadState.FAILED


@pytest.mark.django_db
def test_a_redelivered_import_whose_first_run_died_is_failed_not_run_again(
    make_resident: Callable[..., Resident],
) -> None:
    """With acks_late and reject_on_worker_lost, Celery hands the message out again after the
    worker is killed. It used to find the row BUILDING, return False — a "successful" job on the
    worker-jobs page — and leave the row BUILDING for good."""
    resident = make_resident()
    album_import = _import(resident, state=AlbumImportState.BUILDING, task_id="first-run")
    elsewhere = _import(resident, state=AlbumImportState.BUILDING, task_id="someone-else")

    assert process_album_import.apply(args=(album_import.pk,), task_id="first-run").get() is False
    # A message that is not this row's own does not get to decide that the row's worker is gone.
    assert process_album_import.apply(args=(elsewhere.pk,), task_id="a-duplicate").get() is False

    album_import.refresh_from_db()
    elsewhere.refresh_from_db()
    assert album_import.state == AlbumImportState.FAILED
    assert album_import.error == IMPORT_INTERRUPTED
    assert not album_import.archive
    assert elsewhere.state == AlbumImportState.BUILDING


@pytest.mark.django_db
def test_a_redelivered_download_whose_first_run_died_is_failed(
    make_resident: Callable[..., Resident],
) -> None:
    download = AlbumDownload.objects.create(
        requested_by=make_resident(), media_ids=[], state=AlbumDownloadState.BUILDING, task_id="first-run"
    )

    assert build_album_download.apply(args=(download.pk,), task_id="first-run").get() is False

    download.refresh_from_db()
    assert download.state == AlbumDownloadState.FAILED
    assert download.error == DOWNLOAD_INTERRUPTED


@pytest.mark.django_db
def test_starting_a_job_records_its_task_and_progress_clock(make_resident: Callable[..., Resident]) -> None:
    """The task id is what lets a redelivery recognise its own dead first run; progressed_at is
    what the reaper measures from, so it starts when the work does."""
    download = AlbumDownload.objects.create(requested_by=make_resident(), media_ids=[])

    assert build_album_download.apply(args=(download.pk,), task_id="the-task").get() is True

    download.refresh_from_db()
    assert download.state == AlbumDownloadState.READY
    assert download.task_id == "the-task"
    assert download.progressed_at is not None


@pytest.mark.django_db(transaction=True)
def test_progress_made_inside_a_transaction_is_visible_outside_it(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import stores everything in one transaction. Progress written inside it would only become
    visible on commit — so an import dying half-way would look as if it had never progressed."""
    monkeypatch.setattr(progress, "PROGRESS_WRITE_INTERVAL", timedelta(0))
    album_import = _import(make_resident(), state=AlbumImportState.BUILDING)
    seen: list[object] = []

    def look_from_another_connection() -> None:
        try:
            seen.append(AlbumImport.objects.get(pk=album_import.pk).progress)
        finally:
            connection.close()

    with transaction.atomic():
        JobProgress(AlbumImport.objects.filter(pk=album_import.pk))("storing", 3, 10)
        reader = threading.Thread(target=look_from_another_connection)
        reader.start()
        reader.join()

    assert seen == [{"step": "storing", "done": 3, "total": 10}]


@pytest.mark.django_db
def test_progress_is_written_at_most_every_interval(make_resident: Callable[..., Resident]) -> None:
    album_import = _import(make_resident(), state=AlbumImportState.BUILDING)
    report = JobProgress(AlbumImport.objects.filter(pk=album_import.pk))

    report("checking", 1, 10)  # well inside the first interval

    album_import.refresh_from_db()
    assert album_import.progress == {}
    assert album_import.progressed_at is None


@pytest.mark.django_db
def test_import_status_reports_progress_while_building(
    client: Client, make_resident: Callable[..., Resident]
) -> None:
    resident = make_resident()
    album_import = _import(
        resident, state=AlbumImportState.BUILDING, progress={"step": "storing", "done": 120, "total": 400}
    )
    client.force_login(resident)

    status = client.get(reverse("photo_album:import_zip_status", args=[album_import.token])).json()

    assert status["progress"] == {"step": "storing", "done": 120, "total": 400}


def test_s3_files_opened_for_reading_spill_to_disk() -> None:
    """django-storages defaults max_memory_size to 0, which SpooledTemporaryFile reads as never
    rolling over: every open() of a stored file read the WHOLE object into memory, and a large
    album ZIP had the import worker OOM-killed."""
    storage = PhotoAlbumS3Storage(**settings.PHOTO_ALBUM_S3_OPTIONS)

    assert 0 < storage.max_memory_size <= 64 * 1024 * 1024


def test_heavy_jobs_are_routed_to_the_media_queue() -> None:
    """A route naming a task that does not exist routes nothing, silently — and the job then runs
    on the default queue beside the quick ones."""
    from config.celery import app

    app.loader.import_default_modules()
    for name, route in settings.CELERY_TASK_ROUTES.items():
        assert name in app.tasks, name
        assert route == {"queue": "media"}
    assert "photo_album.tasks.build_media_derivatives" in settings.CELERY_TASK_ROUTES
    assert "photo_album.tasks.process_album_import" in settings.CELERY_TASK_ROUTES


@pytest.mark.django_db
def test_a_photo_is_queued_for_the_quick_worker_and_a_video_for_the_media_worker(
    make_resident: Callable[..., Resident],
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    """A thumbnail is half a second of Pillow, and the uploader is waiting to see it: behind an
    hour-long encode on the one media slot it could wait hours."""
    from photo_album import tasks
    from photo_album.models import Album, Media
    from photo_album.services import upload_media

    from .media_samples import image_upload, video_upload

    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks.build_media_derivatives,
        "apply_async",
        lambda args, queue: queued.append((Media.objects.get(pk=args[0]).filename, queue)),
    )
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Fest")

    with django_capture_on_commit_callbacks(execute=True):
        upload_media(album=album, uploaded_file=image_upload("photo.jpg"), resident=resident)
        upload_media(album=album, uploaded_file=video_upload("clip.mp4"), resident=resident)

    assert queued == [("photo.jpg", "thumbnails"), ("clip.mp4", "media")]


# --- Media derivatives -----------------------------------------------------------------------------


def _broken_video(make_resident: Callable[..., Resident], **fields: object) -> Media:
    from photo_album.models import Album
    from photo_album.services import upload_media

    album = Album.objects.create(folder="2004", name=f"Fremkaldning {Album.objects.count()}")
    corrupt = SimpleUploadedFile("clip.mpg", b"not a video", content_type="video/mpeg")
    resident = make_resident(email=f"uploader-{album.pk}@gahk.dk")
    media = upload_media(album=album, uploaded_file=corrupt, resident=resident)
    if fields:
        Media.objects.filter(pk=media.pk).update(**fields)
        media.refresh_from_db()
    return media


@pytest.mark.django_db
def test_a_video_that_cannot_be_converted_is_a_failed_job_naming_the_file(
    make_resident: Callable[..., Resident],
) -> None:
    """It used to return False — a successful job — so a video sat on "Mediet behandles" with nothing
    on the jobs page to say why."""
    media = _broken_video(make_resident)

    with pytest.raises(DerivativesNotBuilt, match=r"clip\.mpg \(11\xa0bytes\).*attempt 1 of 3"):
        build_media_derivatives.apply(args=(media.pk,), task_id="first")

    media.refresh_from_db()
    assert media.derivative_state == DerivativeState.PENDING
    assert media.derivative_attempts == 1


@pytest.mark.django_db
def test_an_attempt_counts_even_when_the_worker_dies_during_it(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker killed mid-encode saves nothing afterwards, so a video that killed its worker every
    time was never counted, never given up on, and stayed "Mediet behandles" forever."""
    from photo_album import services

    media = _broken_video(make_resident)

    def killed(*_: object) -> None:
        raise SystemExit  # stands in for the kill: nothing after this line runs

    monkeypatch.setattr(services, "video_variants", killed)
    with pytest.raises(SystemExit):
        build_media_derivatives.apply(args=(media.pk,), task_id="first")

    media.refresh_from_db()
    assert media.derivative_attempts == 1


@pytest.mark.django_db
def test_a_video_out_of_attempts_is_marked_failed_without_another_try(
    make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    from photo_album import services

    media = _broken_video(make_resident, derivative_attempts=MAX_DERIVATIVE_ATTEMPTS)
    monkeypatch.setattr(services, "video_variants", lambda *_: pytest.fail("tried again"))

    with pytest.raises(DerivativesNotBuilt, match="gave up after 3 attempts"):
        build_media_derivatives.apply(args=(media.pk,), task_id="last")

    media.refresh_from_db()
    assert media.derivative_state == DerivativeState.FAILED


@pytest.mark.django_db
def test_a_redelivered_conversion_whose_first_run_died_fails_and_releases_the_row(
    make_resident: Callable[..., Resident],
) -> None:
    """With acks_late and reject_on_worker_lost, the message comes back after its worker is killed,
    finds the row still claimed and used to return False: a "successful" job that hid the kill."""
    claimed_at = timezone.now()
    media = _broken_video(
        make_resident, derivative_started_at=claimed_at, derivative_task_id="first", derivative_attempts=1
    )
    other = _broken_video(make_resident, derivative_started_at=claimed_at, derivative_task_id="someone-else")

    with pytest.raises(DerivativesNotBuilt, match="was lost"):
        build_media_derivatives.apply(args=(media.pk,), task_id="first")
    # Another worker's claim is theirs: this message has nothing to do.
    assert build_media_derivatives.apply(args=(other.pk,), task_id="duplicate").get() is False

    media.refresh_from_db()
    other.refresh_from_db()
    # Released for the nightly process_pending_media; the dead run's attempt stays counted.
    assert media.derivative_started_at is None
    assert media.derivative_attempts == 1
    assert other.derivative_started_at == claimed_at


def test_a_failed_ffmpeg_says_why() -> None:
    from photo_album.services import ffmpeg_failure

    killed = subprocess.CalledProcessError(-9, ["ffmpeg"], stderr="")
    refused = subprocess.CalledProcessError(
        1, ["ffmpeg"], stderr="[mpeg @ 0x1] start time not set\n\nInvalid data found when processing input\n"
    )

    assert ffmpeg_failure(killed) == "ffmpeg was stopped by signal 9 — most likely the out-of-memory killer"
    assert ffmpeg_failure(refused) == (
        "ffmpeg exited with 1: [mpeg @ 0x1] start time not set | Invalid data found when processing input"
    )


def test_a_conversion_claim_lasts_as_long_as_a_conversion_may_run() -> None:
    """Shorter, and a second worker could take the row while the first is still encoding it."""
    from photo_album.models import DERIVATIVE_CLAIM_TTL
    from photo_album.tasks import MEDIA_TASK_TIME_LIMIT

    assert DERIVATIVE_CLAIM_TTL == timedelta(seconds=MEDIA_TASK_TIME_LIMIT)


@pytest.mark.django_db
def test_the_size_of_an_original_is_recorded_on_upload_and_backfilled(
    make_resident: Callable[..., Resident],
) -> None:
    from photo_album.services import backfill_sizes

    media = _broken_video(make_resident)
    assert media.size == len(b"not a video")
    Media.objects.filter(pk=media.pk).update(size=None)

    result = backfill_sizes()

    media.refresh_from_db()
    assert (result.sized, result.remaining, media.size) == (1, 0, len(b"not a video"))
