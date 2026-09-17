from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from residents.models import Resident, Role


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
                "task": "core.tasks.send_admin_dummy_notification",
                "status": "SUCCESS",
                "finished_at": None,
                "worker": "celery@test",
                "retries": 0,
            }
        ],
    )
    client.force_login(administrator)

    response = client.get(reverse("siteadmin:worker_jobs"))

    assert response.status_code == 200
    assert "photo_album.tasks.build_media_derivatives" in response.content.decode()
    assert "core.tasks.send_admin_dummy_notification" in response.content.decode()
    assert "Process media" in response.content.decode()
