import pickle
from collections.abc import Callable
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from photo_album.models import Album, AlbumImport, Media
from residents.models import Resident, Role
from residents.views_admin import (
    _add_photo_album_context,
    _display_task_result,
    _format_runtime,
    _past_job_count,
    _past_jobs,
    _task_argument_id,
    _task_runtime,
)


def test_celery_requeues_work_when_a_worker_is_lost() -> None:
    assert settings.CELERY_TASK_ACKS_LATE is True
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True


def test_legacy_pickled_task_result_is_displayed_safely() -> None:
    assert _display_task_result(pickle.dumps({"downloaded": 2, "ready": True})) == (
        '{\n  "downloaded": 2,\n  "ready": true\n}'
    )


def test_task_runtime_is_displayed_in_minutes_seconds_and_milliseconds() -> None:
    assert _format_runtime(timedelta(minutes=2, seconds=3, milliseconds=45)) == "2 min. 3 sek. 45 ms"


def test_task_runtime_normalizes_celery_and_broker_timezones() -> None:
    finished_at = datetime(2026, 9, 20, 15, 59, 34, 346184)
    submitted_at = datetime(2026, 9, 20, 17, 59, 30, 311044)

    assert _task_runtime(finished_at, submitted_at) == "0 min. 4 sek. 35 ms"


def test_task_context_reads_only_integer_positional_ids() -> None:
    assert _task_argument_id({"arguments": "(42,)"}) == 42
    assert _task_argument_id({"arguments": "('42',)"}) is None


@pytest.mark.django_db
def test_photo_album_tasks_show_the_affected_media_and_import(
    make_resident: Callable[..., Resident],
) -> None:
    resident = make_resident()
    album = Album.objects.create(folder="2026", name="Sommerfest")
    media = Media.objects.create(album=album, title="sommer.jpg", requested_by=resident)
    album_import = AlbumImport.objects.create(
        requested_by=resident,
        folder="2026",
        archive_name="sommerfest.zip",
        album_ids=[album.pk],
    )
    jobs: list[dict[str, object]] = [
        {"task": "photo_album.tasks.build_media_derivatives", "arguments": f"({media.pk},)"},
        {"task": "photo_album.tasks.process_album_import", "arguments": f"({album_import.pk},)"},
    ]

    _add_photo_album_context(jobs)

    assert jobs[0]["task_context"] == "Medie: sommer.jpg (album: 2026: Sommerfest)"
    assert jobs[1]["task_context"] == "ZIP: sommerfest.zip (mappe: 2026); albummer: 2026: Sommerfest"


def test_past_jobs_limits_results_before_looking_up_broker_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    database_connection = MagicMock()
    database_connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr("residents.views_admin.connection", database_connection)

    assert _past_jobs("FAILURE", "finished_asc") == []

    sql, parameters = cursor.execute.call_args.args
    assert "WITH recent_results AS MATERIALIZED" in sql
    assert "WHERE (%s = '' OR status = %s)" in sql
    assert "CASE WHEN %s = 'finished_asc' THEN date_done END ASC" in sql
    assert "FROM recent_results AS result" in sql
    assert parameters == ["FAILURE", "FAILURE", "finished_asc", "finished_asc"]
    assert sql.index("WITH recent_results") < sql.index("LEFT JOIN LATERAL")


def test_past_job_count_honours_the_status_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = (42,)
    database_connection = MagicMock()
    database_connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr("residents.views_admin.connection", database_connection)

    assert _past_job_count("SUCCESS") == 42
    assert cursor.execute.call_args.args == (
        "SELECT COUNT(*) FROM celery_taskmeta WHERE (%s = '' OR status = %s)",
        ["SUCCESS", "SUCCESS"],
    )


@pytest.mark.django_db
def test_worker_jobs_requires_administrator(client: Client, make_resident: Callable[..., Resident]) -> None:
    client.force_login(make_resident())

    assert client.get(reverse("siteadmin:worker_jobs")).status_code == 403


@pytest.mark.django_db
def test_worker_jobs_shows_job_data_and_active_schedules(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    interval, _ = IntervalSchedule.objects.get_or_create(every=10, period=IntervalSchedule.MINUTES)
    PeriodicTask.objects.create(
        name="Process media", task="photo_album.tasks.process_pending_media", interval=interval
    )
    monkeypatch.setattr(
        "residents.views_admin._queued_jobs",
        lambda *_: [
            {
                "id": "queued-task-id",
                "task": "photo_album.tasks.build_media_derivatives",
                "queue": "celery",
                "timestamp": None,
                "eta": None,
            }
        ],
    )
    monkeypatch.setattr(
        "residents.views_admin._past_jobs",
        lambda *_: [
            {
                "id": "completed-task-id",
                "task": "core.tasks.send_admin_dummy_notification",
                "status": "SUCCESS",
                "finished_at": None,
                "worker": "celery@test",
                "retries": 0,
                "queue": "celery",
                "submitted_at": None,
                "runtime": None,
                "task_context": "Medie: sommer.jpg (album: 2026: Sommerfest)",
                "arguments": "(42,)",
                "keyword_arguments": "{}",
            }
        ],
    )
    monkeypatch.setattr("residents.views_admin._past_job_count", lambda *_: 321)
    client.force_login(administrator)

    response = client.get(reverse("siteadmin:worker_jobs"))

    assert response.status_code == 200
    assert "photo_album.tasks.build_media_derivatives" in response.content.decode()
    assert "core.tasks.send_admin_dummy_notification" in response.content.decode()
    assert "(42,)" in response.content.decode()
    assert "Medie: sommer.jpg" in response.content.decode()
    assert "viser 1 af 321" in response.content.decode()
    assert 'value="finished_desc" selected' in response.content.decode()
    assert "Process media" in response.content.decode()
    assert reverse("siteadmin:worker_job_detail", args=["completed-task-id"]) in response.content.decode()


@pytest.mark.django_db
def test_worker_job_detail_shows_result_metadata(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    administrator = make_resident(roles=(Role.ADMINISTRATOR,))
    monkeypatch.setattr(
        "residents.views_admin._job_details",
        lambda task_id: {
            "id": task_id,
            "task": "photo_album.tasks.build_album_download",
            "status": "FAILURE",
            "submitted_at": None,
            "finished_at": None,
            "runtime": "0:00:03",
            "result": "download failed",
            "traceback": "Traceback (most recent call last):\\nExampleError",
            "logs_available": False,
        },
    )
    client.force_login(administrator)

    response = client.get(reverse("siteadmin:worker_job_detail", args=["task-id"]))

    assert response.status_code == 200
    assert "build_album_download" in response.content.decode()
    assert "0:00:03" in response.content.decode()
    assert "ExampleError" in response.content.decode()
    assert "Worker-logge gemmes ikke" in response.content.decode()


@pytest.mark.django_db
def test_worker_job_detail_returns_not_found_for_unknown_job(
    client: Client, make_resident: Callable[..., Resident], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("residents.views_admin._job_details", lambda task_id: None)
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))

    assert client.get(reverse("siteadmin:worker_job_detail", args=["missing-task-id"])).status_code == 404
