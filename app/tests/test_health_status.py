"""Heartbeats for the workers and beat, the beat container's healthcheck, and the admin status page
(core.health). `/healthz` itself is in test_healthz.py."""

import datetime
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from core import health
from core.celery_health import HeartbeatStep
from core.models import ServiceHeartbeat
from core.tasks import heartbeat
from residents.models import Role


@pytest.fixture
def alive_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "alive"
    monkeypatch.setattr(health, "WORKER_ALIVE_FILE", path)
    # It would close the test's transaction-wrapped connection (autocommit is off inside it).
    monkeypatch.setattr(health, "close_old_connections", lambda: None)
    return path


def _by_name(checks: list[health.Check]) -> dict[str, health.Check]:
    return {c.name: c for c in checks}


def _beat_at(name: str, ago: datetime.timedelta) -> None:
    ServiceHeartbeat.objects.update_or_create(name=name, defaults={"beat_at": timezone.now() - ago})


@pytest.mark.django_db
def test_a_worker_heartbeat_touches_the_file_and_records_its_node(alive_file: Path) -> None:
    health.worker_heartbeat("media@3f2a91c0")

    assert alive_file.exists()
    assert ServiceHeartbeat.objects.filter(name="worker:media").exists()


@pytest.mark.django_db
def test_the_worker_file_is_touched_even_when_the_database_write_fails(alive_file: Path) -> None:
    with mock.patch.object(health, "record_heartbeat", side_effect=RuntimeError("db down")):
        health.worker_heartbeat("default@abc")
    assert alive_file.exists()


@pytest.mark.django_db
def test_the_bootstep_beats_at_start_and_then_on_the_workers_timer(alive_file: Path) -> None:
    worker = SimpleNamespace(hostname="default@abc", timer=mock.Mock())
    step = HeartbeatStep(worker)

    step.start(worker)

    assert ServiceHeartbeat.objects.filter(name="worker:default").exists()
    interval, callback, args = worker.timer.call_repeatedly.call_args.args
    assert (interval, callback, args) == (
        health.WORKER_HEARTBEAT_SECONDS,
        health.worker_heartbeat,
        ("default@abc",),
    )
    step.stop(worker)
    worker.timer.call_repeatedly.return_value.cancel.assert_called_once()


@pytest.mark.django_db
def test_the_beat_task_records_the_beat_heartbeat() -> None:
    heartbeat()
    assert ServiceHeartbeat.objects.filter(name="beat").exists()


@pytest.mark.django_db
def test_status_is_ok_with_fresh_heartbeats() -> None:
    for name in ("worker:default", "worker:media", "beat"):
        _beat_at(name, datetime.timedelta(seconds=20))

    checks = _by_name(health.system_status())

    assert checks["Worker (default)"].status == health.OK
    assert checks["Worker (media)"].status == health.OK
    assert checks["Beat (planlagte opgaver)"].status == health.OK


@pytest.mark.django_db
def test_status_fails_a_stale_or_missing_heartbeat() -> None:
    _beat_at("worker:default", health.WORKER_STALE_AFTER + datetime.timedelta(seconds=30))

    checks = _by_name(health.system_status())

    assert checks["Worker (default)"].status == health.FAIL
    assert checks["Worker (media)"].status == health.FAIL
    assert checks["Worker (media)"].detail == "Har aldrig meldt sig."
    assert checks["Beat (planlagte opgaver)"].status == health.FAIL


@pytest.mark.django_db
def test_the_database_fails_above_70_percent_of_its_limit(settings: object) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("pg_database_size is Postgres only")
    settings.DB_SIZE_LIMIT_GB = None  # type: ignore[attr-defined]
    assert health.check_database().status == health.OK

    settings.DB_SIZE_LIMIT_GB = 0.000001  # type: ignore[attr-defined]  # ~1 KB: any database is over
    assert health.check_database().status == health.FAIL

    settings.DB_SIZE_LIMIT_GB = 10_000  # type: ignore[attr-defined]
    assert health.check_database().status == health.OK


def test_s3_is_not_checked_without_a_bucket() -> None:
    with mock.patch.object(health, "default_storage", SimpleNamespace()):
        assert health.check_s3().status == health.OK


def test_s3_fails_when_the_bucket_does_not_answer() -> None:
    client = mock.Mock()
    client.head_bucket.side_effect = RuntimeError("403 Forbidden")
    storage = SimpleNamespace(
        bucket_name="gahk", connection=SimpleNamespace(meta=SimpleNamespace(client=client))
    )
    with mock.patch.object(health, "default_storage", storage):
        check = health.check_s3()
    assert check.status == health.FAIL
    assert "403 Forbidden" in check.detail


def _heartbeat_task(last_run_at: datetime.datetime | None) -> None:
    every_minute, _ = IntervalSchedule.objects.get_or_create(every=60, period=IntervalSchedule.SECONDS)
    PeriodicTask.objects.update_or_create(
        name="heartbeat",
        defaults={"task": "core.tasks.heartbeat", "interval": every_minute, "last_run_at": last_run_at},
    )


@pytest.mark.django_db
def test_the_beat_healthcheck_fails_until_beat_has_sent_the_heartbeat() -> None:
    with pytest.raises(CommandError):
        call_command("healthcheck", "beat")
    _heartbeat_task(None)
    with pytest.raises(CommandError):
        call_command("healthcheck", "beat")


@pytest.mark.django_db
def test_the_beat_healthcheck_follows_when_beat_last_sent_it() -> None:
    _heartbeat_task(timezone.now() - datetime.timedelta(minutes=2))
    call_command("healthcheck", "beat")

    _heartbeat_task(timezone.now() - datetime.timedelta(minutes=30))
    with pytest.raises(CommandError):
        call_command("healthcheck", "beat")


@pytest.mark.django_db
def test_the_status_page_is_for_administrators(make_resident: Callable) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    plain = make_resident(email="plain@gahk.dk")
    c = Client()

    c.force_login(plain)
    assert c.get("/admin/status").status_code == 403

    c.force_login(admin)
    response = c.get("/admin/status")
    assert response.status_code == 200
    assert "Driftsstatus" in response.content.decode()
    assert {c.name for c in response.context["checks"]} >= {"Database", "S3", "Beat (planlagte opgaver)"}
