import os
from typing import Any

from celery import Celery
from celery.signals import worker_process_init

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("gahk_intern")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@worker_process_init.connect
def _own_broker_connections(**_: Any) -> None:  # noqa: ANN401
    """Give a newly forked worker process database connections of its own for the broker.

    kombu's SQLAlchemy transport keeps its engines in a class-level cache (`Channel._engines`), and a
    pool process forked after the main one has used the broker — every replacement for a child lost
    to the OOM killer or a time limit — inherits them, open connections and all. Two processes then
    talk over one Postgres connection, and psycopg's prepared statements collide: "prepared statement
    "_pg3_3" already exists" as a job was queued. `dispose(close=False)` gives this process a fresh
    pool and leaves the parent's connections open for the parent.
    """
    try:
        from kombu.transport.sqlalchemy import Channel
    except ImportError:  # pragma: no cover — no SQLAlchemy, so no such transport either
        return
    for engine, _session in list(Channel._engines.values()):
        engine.dispose(close=False)
