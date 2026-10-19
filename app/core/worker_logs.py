"""Keep what a Celery task logged, so the worker-jobs admin page can show it next to the job.

Worker output otherwise lives only in the worker container's log, interleaved with every other job
and gone with the container. While a task runs, a handler collects the records it emits (INFO and
up, its own and those of the code it calls); when it finishes they are stored as one
`WorkerJobLog` row keyed on the task id. A task that logged nothing stores nothing, so the many
small jobs that just do their work add no rows.

The buffer is a ContextVar, set in `task_prerun` and taken in `task_postrun`, so records from
anything else running in the process — or from another thread — never land on a job's log.
"""

import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from celery.signals import task_postrun, task_prerun
from django.utils import timezone

# Enough for an import's list of left-out files; a task that logs more than this keeps its first
# lines and a count of the rest, rather than growing one row without bound.
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000

# The worker's own bookkeeping ("Task … received", "… succeeded in 0.3s") — the job page already
# shows all of that from the result backend — and SQL debug output.
_IGNORED_PREFIXES = (
    "celery.app",
    "celery.worker",
    "celery.beat",
    "celery.pool",
    "kombu",
    "amqp",
    "django.db",
)


class _Capture:
    def __init__(self, started_at: datetime) -> None:
        self.started_at = started_at
        self.lines: list[dict[str, str]] = []
        self.omitted = 0


_current: ContextVar[_Capture | None] = ContextVar("worker_job_log", default=None)


class JobLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        capture = _current.get()
        if capture is None or record.name.startswith(_IGNORED_PREFIXES):
            return
        if len(capture.lines) >= MAX_LINES:
            capture.omitted += 1
            return
        try:
            message = self.format(record)
        except Exception:
            message = str(record.msg)
        capture.lines.append(
            {
                "at": timezone.localtime(datetime.fromtimestamp(record.created, tz=UTC)).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message[:MAX_LINE_LENGTH],
            }
        )


_handler = JobLogHandler()


def _install() -> None:
    """Attach the handler where records end up: the root logger, plus every logger configured not
    to propagate to it (core's own, Celery's task loggers)."""
    loggers = [logging.getLogger()]
    loggers += [
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger) and not logger.propagate
    ]
    for logger in loggers:
        if _handler not in logger.handlers:
            logger.addHandler(_handler)


@task_prerun.connect
def _start(task_id: str | None = None, **_: Any) -> None:  # noqa: ANN401
    if task_id is None:
        return
    _install()
    _current.set(_Capture(timezone.now()))


@task_postrun.connect
def _finish(task_id: str | None = None, task: Any = None, state: str | None = None, **_: Any) -> None:  # noqa: ANN401
    capture = _current.get()
    _current.set(None)
    if task_id is None or capture is None or not capture.lines:
        return
    from .models import WorkerJobLog

    try:
        log, _created = WorkerJobLog.objects.get_or_create(
            task_id=task_id,
            defaults={"task_name": getattr(task, "name", "") or "", "started_at": capture.started_at},
        )
        # A retry runs under the same task id: its lines follow the earlier attempt's.
        room = max(0, MAX_LINES - len(log.lines))
        log.lines = [*log.lines, *capture.lines[:room]]
        log.omitted += capture.omitted + max(0, len(capture.lines) - room)
        log.state = state or ""
        log.finished_at = timezone.now()
        log.save()
    except Exception:
        logging.getLogger(__name__).warning("Could not store the log of task %s", task_id, exc_info=True)
