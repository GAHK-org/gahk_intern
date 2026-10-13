import pickle
from collections.abc import Callable

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from residents.models import Resident, Role
from residents.views_admin import _display_task_result


def test_celery_requeues_work_when_a_worker_is_lost() -> None:
    assert settings.CELERY_TASK_ACKS_LATE is True
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True


def test_legacy_pickled_task_result_is_displayed_safely() -> None:
    assert _display_task_result(pickle.dumps({"downloaded": 2, "ready": True})) == (
        '{\n  "downloaded": 2,\n  "ready": true\n}'
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
        lambda: [
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
        lambda: [
            {
                "id": "completed-task-id",
                "task": "core.tasks.send_admin_dummy_notification",
                "status": "SUCCESS",
                "finished_at": None,
                "worker": "celery@test",
                "retries": 0,
                "queue": "celery",
                "submitted_at": None,
                "arguments": "(42,)",
                "keyword_arguments": "{}",
            }
        ],
    )
    client.force_login(administrator)

    response = client.get(reverse("siteadmin:worker_jobs"))

    assert response.status_code == 200
    assert "photo_album.tasks.build_media_derivatives" in response.content.decode()
    assert "core.tasks.send_admin_dummy_notification" in response.content.decode()
    assert "(42,)" in response.content.decode()
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
