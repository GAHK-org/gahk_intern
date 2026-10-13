import importlib
import logging
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from celery import shared_task
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.backends.utils import CursorWrapper
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from config.celery import app as celery_app
from core import worker_logs
from core.models import WorkerJobLog
from core.tasks import WORKER_JOB_RETENTION, purge_worker_job_logs, purge_worker_job_results
from photo_album.models import AlbumImport
from photo_album.tasks import process_album_import
from residents.models import Resident, Role

from .media_samples import jpeg_bytes

logger = logging.getLogger("photo_album.test_worker_logs")


@shared_task
def _chatty(count: int) -> int:
    for index in range(count):
        logger.info("line %d", index)
    return count


@shared_task
def _quiet() -> int:
    return 0


def _zip(*members: tuple[str, bytes]) -> SimpleUploadedFile:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for member, content in members:
            archive.writestr(member, content)
    return SimpleUploadedFile("Fest.zip", payload.getvalue(), content_type="application/zip")


@pytest.mark.django_db
def test_an_import_job_keeps_the_list_of_left_out_files(make_resident: Callable[..., Resident]) -> None:
    album_import = AlbumImport.objects.create(
        requested_by=make_resident(),
        folder="2026",
        archive_name="Fest.zip",
        archive=_zip(
            ("Fest/a.jpg", jpeg_bytes()), ("Fest/.DS_Store", b"finder"), ("__MACOSX/Fest/._a.jpg", b"x")
        ),
    )

    result = process_album_import.apply(args=[album_import.pk])

    log = WorkerJobLog.objects.get(task_id=result.id)
    assert log.task_name == "photo_album.tasks.process_album_import"
    assert log.state == "SUCCESS"
    messages = [line["message"] for line in log.lines]
    assert messages[0].startswith("Importing Fest.zip into folder 2026")
    assert any(message.startswith("Left out: ") and ".DS_Store" in message for message in messages)
    assert any(message.startswith("Left out: ") and "._a.jpg" in message for message in messages)
    assert "Imported 1 media item(s) into 1 album(s): Fest. 2 file(s) left out." in messages


@pytest.mark.django_db
def test_a_refused_import_logs_every_reason(make_resident: Callable[..., Resident]) -> None:
    album_import = AlbumImport.objects.create(
        requested_by=make_resident(),
        folder="2026",
        archive_name="Fest.zip",
        archive=_zip(("Fest/a.jpg", b"not a picture"), ("Fest/notes.txt", b"hello")),
    )

    result = process_album_import.apply(args=[album_import.pk])

    lines = WorkerJobLog.objects.get(task_id=result.id).lines
    refused = [line for line in lines if line["message"].startswith("Refused: ")]
    assert len(refused) == len(AlbumImport.objects.get().errors) >= 1
    assert {line["level"] for line in refused} == {"WARNING"}


@pytest.mark.django_db
def test_a_task_that_logs_nothing_stores_nothing_and_logging_outside_a_task_is_not_kept() -> None:
    logger.info("not in a task")
    _quiet.apply()
    assert not WorkerJobLog.objects.exists()

    result = _chatty.apply(args=[1])
    assert [line["message"] for line in WorkerJobLog.objects.get(task_id=result.id).lines] == ["line 0"]


@pytest.mark.django_db
def test_a_long_log_keeps_its_first_lines_and_counts_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_logs, "MAX_LINES", 3)

    result = _chatty.apply(args=[5])

    log = WorkerJobLog.objects.get(task_id=result.id)
    assert [line["message"] for line in log.lines] == ["line 0", "line 1", "line 2"]
    assert log.omitted == 2


@pytest.mark.django_db
def test_worker_job_page_shows_the_log_even_after_the_result_is_gone(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("residents.views_admin._job_details", lambda task_id: None)
    WorkerJobLog.objects.create(
        task_id="task-id",
        task_name="photo_album.tasks.process_album_import",
        state="SUCCESS",
        started_at=timezone.now(),
        finished_at=timezone.now(),
        lines=[
            {
                "at": "2026-10-13T12:34:56+02:00",
                "level": "INFO",
                "logger": "photo_album.tasks",
                "message": "Left out: Fest/.DS_Store",
            }
        ],
        omitted=4,
    )
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))

    content = client.get(reverse("siteadmin:worker_job_detail", args=["task-id"])).content.decode()

    assert "process_album_import" in content
    assert "12:34:56" in content
    assert "Left out: Fest/.DS_Store" in content
    assert "4 linjer mere" in content
    assert client.get(reverse("siteadmin:worker_job_detail", args=["other"])).status_code == 404


@pytest.mark.django_db
def test_only_the_logs_of_old_successful_jobs_are_purged() -> None:
    now = timezone.now()
    for task_id, state, age in [
        ("old", "SUCCESS", 31),
        ("new", "SUCCESS", 29),
        ("old-failure", "FAILURE", 365),
        ("old-unfinished", "", 365),
    ]:
        WorkerJobLog.objects.create(task_id=task_id, state=state, started_at=now - timedelta(days=age))

    assert purge_worker_job_logs.run() == 1
    assert sorted(WorkerJobLog.objects.values_list("task_id", flat=True)) == [
        "new",
        "old-failure",
        "old-unfinished",
    ]


def _store_result(cursor: CursorWrapper, task_id: str, status: str, age: timedelta) -> None:
    done = datetime.now(UTC).replace(tzinfo=None) - age  # naive UTC, as Celery stores it
    cursor.execute(
        "INSERT INTO celery_taskmeta (task_id, status, date_done) VALUES (%s, %s, %s)",
        [task_id, status, done],
    )


@pytest.mark.django_db
def test_only_old_successful_jobs_are_purged_from_the_result_backend() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE celery_taskmeta (id serial PRIMARY KEY, task_id varchar(155) UNIQUE,"
            " status varchar(50), date_done timestamp without time zone)"
        )
        _store_result(cursor, "old", "SUCCESS", WORKER_JOB_RETENTION + timedelta(hours=1))
        _store_result(cursor, "new", "SUCCESS", WORKER_JOB_RETENTION - timedelta(hours=1))
        _store_result(cursor, "old-failure", "FAILURE", timedelta(days=365))
        _store_result(cursor, "old-started", "STARTED", timedelta(days=365))

    assert purge_worker_job_results.run() == 1

    with connection.cursor() as cursor:
        cursor.execute("SELECT task_id FROM celery_taskmeta ORDER BY task_id")
        assert [row[0] for row in cursor.fetchall()] == ["new", "old-failure", "old-started"]


@pytest.mark.django_db
def test_the_result_sweep_is_a_noop_without_a_database_result_backend() -> None:
    assert purge_worker_job_results.run() == 0


@pytest.mark.django_db
def test_celerys_own_cleanup_which_deletes_failures_too_is_off() -> None:
    from django.apps import apps
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    migration = importlib.import_module("core.migrations.0012_remove_celery_backend_cleanup")
    crontab = CrontabSchedule.objects.create(minute="0", hour="4")
    PeriodicTask.objects.create(name="celery.backend_cleanup", task="celery.backend_cleanup", crontab=crontab)
    PeriodicTask.objects.create(name="other", task="core.tasks.purge_worker_job_logs", crontab=crontab)

    migration.remove_backend_cleanup(apps, None)  # type: ignore[arg-type]

    assert list(PeriodicTask.objects.values_list("name", flat=True)) == ["other"]
    assert celery_app.conf.result_expires is None, "Beat would install celery.backend_cleanup again"
