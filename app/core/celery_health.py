"""The worker heartbeat as a Celery bootstep — see core.health.

The worker's own timer, in the main process: it keeps beating while the pool's single slot runs an
hour-long encode, and stops if the worker itself hangs, which is what the healthcheck is for.
"""

import logging
from typing import Any

from celery import bootsteps

# Beat's two frequent tasks, which would otherwise fill the worker log with "received"/"succeeded".
QUIET_TASKS = frozenset({"core.tasks.heartbeat", "core.tasks.check_health"})
# Where Celery logs "Task … received" and "Task … succeeded" for every task.
TASK_LIFECYCLE_LOGGERS = ("celery.worker.strategy", "celery.app.trace")


class QuietHeartbeat(logging.Filter):
    """Drops the received/succeeded INFO lines of beat's frequent tasks (QUIET_TASKS): thousands of
    lines a day saying nothing. A failure is logged above INFO and still shows."""

    def filter(self, record: logging.LogRecord) -> bool:
        data = getattr(record, "data", None)
        quiet = isinstance(data, dict) and data.get("name") in QUIET_TASKS
        return not (quiet and record.levelno == logging.INFO)


def quiet_heartbeat_logs() -> None:
    for name in TASK_LIFECYCLE_LOGGERS:
        logging.getLogger(name).addFilter(QuietHeartbeat())


class HeartbeatStep(bootsteps.StartStopStep):
    requires = {"celery.worker.components:Timer"}

    def __init__(self, worker: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(worker, **kwargs)
        self._tref: Any = None

    def start(self, worker: Any) -> None:  # noqa: ANN401
        from .health import WORKER_HEARTBEAT_SECONDS, worker_heartbeat

        worker_heartbeat(worker.hostname)
        self._tref = worker.timer.call_repeatedly(
            WORKER_HEARTBEAT_SECONDS, worker_heartbeat, (worker.hostname,), priority=10
        )

    def stop(self, worker: Any) -> None:  # noqa: ANN401
        if self._tref is not None:
            self._tref.cancel()
