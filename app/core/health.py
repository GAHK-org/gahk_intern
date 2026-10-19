"""Health: `/healthz` for the web container, heartbeats for the workers and beat, and the fuller
status the admin page shows.

`/healthz` stays deliberately narrow — "this process answers and reaches the database" — because
Docker and Coolify act on it: a full disk or an S3 outage failing it would only stall a rolling
deploy or restart `web`, which fixes neither. Those are on the status page (`system_status`).

`/healthz` is a middleware first in MIDDLEWARE rather than a view, so it answers before Django
validates the Host header: a checker inside the container sends whatever host it likes, and an
ALLOWED_HOSTS 400 would read as an unhealthy app.
"""

import datetime
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import close_old_connections, connection
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

HEALTHZ_PATH = "/healthz"


def healthz() -> HttpResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return HttpResponse("db unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


class HealthzMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path == HEALTHZ_PATH and request.method in ("GET", "HEAD"):
            return healthz()
        return self.get_response(request)


# --- Heartbeats -----------------------------------------------------------------------------------
#
# `celery inspect ping` is not an option: the broker is kombu's SQLAlchemy transport, which has no
# broadcast, so remote control never reaches a worker.

# Touched by each worker's timer; the container healthcheck reads its age (docker-compose.prod.yml).
# Only a liveness marker in the container's own /tmp: nothing is read from it but its mtime.
WORKER_ALIVE_FILE = Path(os.environ.get("CELERY_ALIVE_FILE", "/tmp/celery-worker-alive"))  # noqa: S108  # nosec B108
WORKER_HEARTBEAT_SECONDS = 30
WORKER_STALE_AFTER = datetime.timedelta(minutes=2)
# Beat schedules `core.tasks.heartbeat` every minute and the default worker runs it, so this row is
# beat AND the default queue keeping up. That worker runs one job at a time, so the heartbeat waits
# behind whatever is running: the threshold must outlast the longest job (CELERY_TASK_TIME_LIMIT,
# 15 min), or a long mail-out reads as a failure. Beat itself stopping is its container
# healthcheck's job (manage.py healthcheck beat), which queueing does not affect.
DEFAULT_QUEUE_STALE_AFTER = datetime.timedelta(minutes=20)
DEFAULT_QUEUE_CHECK = "Standardkø (beat → worker)"
# The workers the status page expects to hear from: the node names in docker-compose.prod.yml.
EXPECTED_WORKERS = ("default", "media")


def record_heartbeat(name: str) -> None:
    from .models import ServiceHeartbeat

    ServiceHeartbeat.objects.update_or_create(name=name, defaults={"beat_at": timezone.now()})


def worker_heartbeat(hostname: str) -> None:
    """One worker beat: the file for its container healthcheck, the row for the status page.

    Keyed on the node name ("default" of "default@<container id>"), so a redeploy's new container
    takes over the row rather than leaving the old one behind looking dead."""
    WORKER_ALIVE_FILE.touch()
    try:
        close_old_connections()  # this runs in the worker's timer thread, across DB restarts
        record_heartbeat(f"worker:{hostname.split('@', maxsplit=1)[0]}")
    except Exception:  # the file is what the healthcheck reads; a missed row costs a stale status
        logger.warning("Could not record the worker heartbeat.", exc_info=True)


# --- The status page ------------------------------------------------------------------------------

OK, WARN, FAIL = "ok", "advarsel", "fejl"

# Fail the database check above this share of DB_SIZE_LIMIT_GB.
DB_SIZE_FAIL_FRACTION = 0.7
S3_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # OK / WARN / FAIL
    detail: str


def _gb(size: int) -> str:
    return f"{size / 1024**3:.2f} GB"


def _age(moment: datetime.datetime) -> str:
    seconds = int((timezone.now() - moment).total_seconds())
    if seconds < 120:
        return f"{seconds} sek. siden"
    if seconds < 7200:
        return f"{seconds // 60} min. siden"
    return f"{seconds // 3600} t. siden"


def check_database() -> Check:
    if connection.vendor != "postgresql":
        return Check("Database", WARN, f"Størrelse kun for Postgres (dette er {connection.vendor}).")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            size = int(cursor.fetchone()[0])
    except Exception as exc:
        return Check("Database", FAIL, f"Kan ikke nås: {exc}")
    limit_gb = settings.DB_SIZE_LIMIT_GB
    if not limit_gb:
        return Check("Database", OK, f"{_gb(size)} brugt (sæt DB_SIZE_LIMIT_GB for en grænse).")
    share = size / (limit_gb * 1024**3)
    detail = f"{_gb(size)} af {limit_gb:g} GB ({share:.0%})."
    return Check("Database", FAIL if share > DB_SIZE_FAIL_FRACTION else OK, detail)


def check_s3() -> Check:
    bucket = getattr(default_storage, "bucket_name", None)
    if not bucket:
        return Check("S3", OK, "Ikke i brug her (lokal fillagring).")

    def head() -> None:
        default_storage.connection.meta.client.head_bucket(Bucket=bucket)  # type: ignore[attr-defined]

    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(head).result(timeout=S3_TIMEOUT_SECONDS)
    except FutureTimeout:
        return Check("S3", FAIL, f"Bucket {bucket} svarer ikke inden for {S3_TIMEOUT_SECONDS} sek.")
    except Exception as exc:
        return Check("S3", FAIL, f"Bucket {bucket}: {exc}")
    finally:
        pool.shutdown(wait=False)
    return Check("S3", OK, f"Bucket {bucket} svarer ({(time.monotonic() - started) * 1000:.0f} ms).")


def _heartbeat_check(label: str, name: str, stale_after: datetime.timedelta) -> Check:
    from .models import ServiceHeartbeat

    row = ServiceHeartbeat.objects.filter(name=name).first()
    if row is None:
        return Check(label, FAIL, "Har aldrig meldt sig.")
    status = OK if timezone.now() - row.beat_at <= stale_after else FAIL
    return Check(label, status, f"Senest {_age(row.beat_at)}.")


def system_status() -> list[Check]:
    return [
        check_database(),
        check_s3(),
        *(
            _heartbeat_check(f"Worker ({node})", f"worker:{node}", WORKER_STALE_AFTER)
            for node in EXPECTED_WORKERS
        ),
        _heartbeat_check(DEFAULT_QUEUE_CHECK, "beat", DEFAULT_QUEUE_STALE_AFTER),
    ]
