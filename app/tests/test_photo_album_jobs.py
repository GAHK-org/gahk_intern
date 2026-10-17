"""Background ZIP jobs: progress, telling a slow job from a dead one, and where jobs run."""

import threading
from collections.abc import Callable
from datetime import timedelta

import pytest
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from photo_album import progress
from photo_album.models import (
    JOB_STALLED_AFTER,
    AlbumDownload,
    AlbumDownloadState,
    AlbumImport,
    AlbumImportState,
)
from photo_album.progress import JobProgress
from photo_album.storage import PhotoAlbumS3Storage, get_photo_album_storage
from photo_album.tasks import (
    DOWNLOAD_INTERRUPTED,
    IMPORT_INTERRUPTED,
    build_album_download,
    fail_stalled_imports,
    process_album_import,
)
from residents.models import Resident


def _import(resident: Resident, **fields: object) -> AlbumImport:
    album_import = AlbumImport.objects.create(
        requested_by=resident, folder="2026", archive_name="fest.zip", **fields
    )
    album_import.archive.save("fest.zip", ContentFile(b"not read"), save=True)
    return album_import


@pytest.mark.django_db
def test_an_import_whose_worker_never_returned_is_failed_and_its_zip_deleted(
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
    assert not stalled.archive
    assert not get_photo_album_storage().exists(archive_key)
    # Three hours in, but it reported progress a moment ago: slow, not dead.
    assert working.state == AlbumImportState.BUILDING
    assert working.archive


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
