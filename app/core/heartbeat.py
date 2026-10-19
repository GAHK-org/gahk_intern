"""Heartbeats from the Celery processes, so /health can tell whether workers and beat are running.

The broker is kombu's SQLAlchemy transport, which has no fanout — so `celery inspect ping` and
worker events, the usual ways to ask a worker whether it is there, never get an answer. Instead
each process writes a `ServiceHeartbeat` row from a thread of its own every HEARTBEAT_INTERVAL.

A thread, not a periodic task: a task would sit in the queue behind whatever the worker is doing,
and `worker-media` legitimately spends up to CELERY_TASK_TIME_LIMIT (15 min) on one video encode,
so a queued heartbeat would report a perfectly healthy worker as dead. The thread runs in the
worker's main process, beside the pool, and keeps beating however long a task takes.

Each beat also touches a file, which is what the worker's and beat's Docker healthchecks read
(docker-compose.prod.yml): Coolify then shows each container as /health sees it, without booting
Django and querying the database every 30 seconds in each container.
"""

import logging
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from celery.signals import beat_init, worker_ready, worker_shutdown
from django.db import close_old_connections, connection
from django.utils import timezone

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30  # seconds
# /health calls a process down once its row is this old: several missed beats, not one slow write.
HEARTBEAT_MAX_AGE = 120  # seconds
# /tmp in the image. Each healthcheck in docker-compose.prod.yml spells out this path and
# HEARTBEAT_MAX_AGE, so a change to either goes there too.
HEARTBEAT_DIR = Path(tempfile.gettempdir())

_stop = threading.Event()


def record(name: str, hostname: str = "") -> None:
    from .models import ServiceHeartbeat

    ServiceHeartbeat.objects.update_or_create(
        name=name, defaults={"hostname": hostname, "beat_at": timezone.now()}
    )


def _run(name: str, hostname: str, stop: threading.Event) -> None:
    while True:
        try:
            close_old_connections()
            record(name, hostname)
            # Only once the row is written, so the file is no fresher than what /health reads.
            (HEARTBEAT_DIR / f"heartbeat-{name}").touch()
        except Exception:
            # A database blip must not end the thread: the next beat retries, and until then the
            # row just ages, which is what /health should see.
            logger.exception("Could not record the %s heartbeat", name)
        finally:
            # The thread's own connection; holding it open between beats would pin one per process.
            connection.close()
        if stop.wait(HEARTBEAT_INTERVAL):
            return


def start(name: str, hostname: str) -> threading.Thread:
    thread = threading.Thread(
        target=_run, args=(name, hostname, _stop), name=f"heartbeat-{name}", daemon=True
    )
    thread.start()
    return thread


@worker_ready.connect
def _start_worker_heartbeat(sender: Any = None, **kwargs: Any) -> None:  # noqa: ANN401 — Celery's signal signature
    # "default@3f2a…" → "default": the -n in docker-compose.prod.yml, stable across redeploys.
    hostname = getattr(sender, "hostname", None) or socket.gethostname()
    start(hostname.split("@", 1)[0], hostname)


@beat_init.connect
def _start_beat_heartbeat(**kwargs: Any) -> None:  # noqa: ANN401 — Celery's signal signature
    start("beat", socket.gethostname())


@worker_shutdown.connect
def _stop_heartbeat(**kwargs: Any) -> None:  # noqa: ANN401 — Celery's signal signature
    _stop.set()
