"""/healthz and /health (core/health.py), and the heartbeats /health reads (core/heartbeat.py)."""

import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from core import health, heartbeat
from core.models import ServiceHeartbeat

PROCESSES = ["default", "media", "beat"]


@pytest.fixture(autouse=True)
def _processes(settings: object) -> None:
    settings.HEALTH_PROCESSES = PROCESSES
    # A host production would never list: the probe must get through ALLOWED_HOSTS regardless.
    settings.ALLOWED_HOSTS = ["gahk.dk"]


@pytest.fixture
def bucket() -> mock.Mock:
    client = mock.Mock()
    with mock.patch.object(health, "_s3_client", return_value=client):
        yield client


def _beat(name: str, age: timedelta = timedelta()) -> None:
    ServiceHeartbeat.objects.update_or_create(name=name, defaults={"beat_at": timezone.now() - age})


def test_healthz_answers_without_touching_anything() -> None:
    # No django_db mark: a database query here would fail the test.
    with mock.patch.object(health, "health") as full_check:
        response = Client().get("/healthz", HTTP_HOST="127.0.0.1:8000")
    assert response.status_code == 200
    assert response.content == b"ok"
    full_check.assert_not_called()


@pytest.mark.django_db
def test_health_ok_when_everything_is_up(bucket: mock.Mock) -> None:
    for name in PROCESSES:
        _beat(name, timedelta(seconds=10))

    response = Client().get("/health", HTTP_HOST="127.0.0.1:8000")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "storage", *PROCESSES}
    assert body["checks"]["media"] == {"ok": True, "age_seconds": 10}
    assert response["Cache-Control"] == "no-store"
    bucket.head_bucket.assert_called_once()


@pytest.mark.django_db
def test_health_fails_on_a_stale_or_missing_heartbeat(bucket: mock.Mock) -> None:
    _beat("default")
    _beat("media", timedelta(seconds=heartbeat.HEARTBEAT_MAX_AGE + 1))
    # beat has never written a row

    response = Client().get("/health")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["default"]["ok"] is True
    assert checks["media"]["ok"] is False
    assert checks["beat"] == {"ok": False, "age_seconds": None}


@pytest.mark.django_db
def test_health_fails_when_the_bucket_is_unreachable(bucket: mock.Mock) -> None:
    for name in PROCESSES:
        _beat(name)
    bucket.head_bucket.side_effect = Exception(
        "Could not connect to the endpoint URL: https://secret.example"
    )

    response = Client().get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["storage"] == {"ok": False}
    assert b"secret.example" not in response.content


@pytest.mark.django_db
def test_health_fails_when_the_database_is_unreachable(bucket: mock.Mock) -> None:
    with mock.patch.object(health, "check_database", side_effect=Exception("connection refused")):
        response = Client().get("/health")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["database"] == {"ok": False}
    assert checks["storage"] == {"ok": True}
    assert all(checks[name] == {"ok": False, "age_seconds": None} for name in PROCESSES)


@pytest.fixture
def heartbeat_dir(tmp_path: Path) -> Iterator[Path]:
    with mock.patch.object(heartbeat, "HEARTBEAT_DIR", tmp_path):
        yield tmp_path


@pytest.mark.django_db
def test_the_heartbeat_thread_writes_a_row_and_stops(heartbeat_dir: Path) -> None:
    stop = threading.Event()
    stop.set()  # one beat, then return instead of waiting HEARTBEAT_INTERVAL

    # Both would drop the connection the test's transaction lives on.
    with (
        mock.patch.object(heartbeat.connection, "close"),
        mock.patch.object(heartbeat, "close_old_connections"),
    ):
        heartbeat._run("media", "media@3f2a", stop)

    row = ServiceHeartbeat.objects.get(name="media")
    assert row.hostname == "media@3f2a"
    assert timezone.now() - row.beat_at < timedelta(seconds=5)
    # What worker-media's Docker healthcheck reads.
    assert time.time() - (heartbeat_dir / "heartbeat-media").stat().st_mtime < 5


def test_a_failed_heartbeat_leaves_the_file_alone(heartbeat_dir: Path) -> None:
    stop = threading.Event()
    stop.set()

    with (
        mock.patch.object(heartbeat, "record", side_effect=Exception("connection refused")),
        mock.patch.object(heartbeat.connection, "close"),
        mock.patch.object(heartbeat, "close_old_connections"),
    ):
        heartbeat._run("media", "media@3f2a", stop)

    # The healthcheck must go stale with the row, not stay green on a database the worker can't reach.
    assert not (heartbeat_dir / "heartbeat-media").exists()


def test_a_worker_beats_under_its_node_name() -> None:
    with mock.patch.object(heartbeat, "start") as start:
        heartbeat._start_worker_heartbeat(sender=mock.Mock(hostname="default@3f2a"))
    start.assert_called_once_with("default", "default@3f2a")
