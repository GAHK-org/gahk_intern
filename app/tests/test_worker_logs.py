import logging
import zipfile
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO

import pytest
from celery import shared_task
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core import worker_logs
from core.models import WorkerJobLog
from core.tasks import purge_worker_job_logs
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
def test_old_job_logs_are_purged() -> None:
    now = timezone.now()
    WorkerJobLog.objects.create(task_id="old", started_at=now - timedelta(days=8))
    WorkerJobLog.objects.create(task_id="new", started_at=now - timedelta(days=6))

    assert purge_worker_job_logs.run() == 1
    assert list(WorkerJobLog.objects.values_list("task_id", flat=True)) == ["new"]
