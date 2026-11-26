"""Keep the worker's own record of every Celery job — a `WorkerJobLog` row — for the worker-jobs
admin page, which reads status, result and traceback from Celery's result table but would otherwise
lose three things:

* WHEN A JOB WAS SENT. Nothing in Celery or kombu records it: kombu's `timestamp` column stays empty
  until a worker takes the message, and is then the worker's local clock at that moment. So every
  message is stamped with a `sent_at` header as it is published, in UTC. A retry keeps the first
  send's: Celery republishes with the request's own custom headers, and the stamp only fills a gap.
* WHAT SENT IT, AND WITH WHAT. The parent job and the arguments are in the broker message, which
  core.tasks sweeps after a day.
* WHAT IT LOGGED. Worker output otherwise lives only in the worker container's log, interleaved
  with every other job and gone with the container.

When a task starts, its row is written with the sent time, the parent and the arguments, so a job
whose worker dies under it still has one. While it runs, a handler collects the records it emits
(INFO and up, its own and those of the code it calls); the job's monitor (core/job_progress.py)
writes them to the row as they come, with how far the job has got, and when it finishes the row
gets the whole log, its state and its finish time.

The buffer is a ContextVar, set in `task_prerun` and taken in `task_postrun`, so records from
anything else running in the process — or from another thread — never land on a job's log.
"""

import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from celery.signals import before_task_publish, task_postrun, task_prerun
from django.utils import timezone

from . import job_memory, job_progress

# Enough for an import's list of left-out files; a task that logs more than this keeps its first
# lines and a count of the rest, rather than growing one row without bound.
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000

# The message header carrying the publish time. Read back from the broker by the worker-jobs page
# for a job still in the queue, and from `task.request` here once a worker has it.
SENT_AT_HEADER = "sent_at"

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
    # A copy: a logger created by another thread mid-walk would change the dict under the loop.
    loggers += [
        logger
        for logger in list(logging.Logger.manager.loggerDict.values())
        if isinstance(logger, logging.Logger) and not logger.propagate
    ]
    for logger in loggers:
        if _handler not in logger.handlers:
            logger.addHandler(_handler)


def parse_sent_at(value: object) -> datetime | None:
    """The `sent_at` header as an aware datetime, or None for a message sent without one."""
    if not isinstance(value, str):
        return None
    try:
        sent_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return sent_at if timezone.is_aware(sent_at) else None


def _request_sent_at(request: Any) -> datetime | None:  # noqa: ANN401
    """The header on a running task's request: an attribute in a worker, which spreads a message's
    headers over the request, and in `request.headers` for a task run with apply(headers=…)."""
    custom = getattr(request, "headers", None) or {}
    return parse_sent_at(getattr(request, SENT_AT_HEADER, None) or custom.get(SENT_AT_HEADER))


@before_task_publish.connect
def _stamp_sent_at(headers: dict[str, Any] | None = None, **_: Any) -> None:  # noqa: ANN401
    if headers is not None:
        headers.setdefault(SENT_AT_HEADER, timezone.now().isoformat())


@task_prerun.connect
def _start(task_id: str | None = None, task: Any = None, **_: Any) -> None:  # noqa: ANN401
    if task_id is None:
        return
    _install()
    capture = _Capture(timezone.now())
    _current.set(capture)
    request = getattr(task, "request", None)
    from .models import WorkerJobLog

    try:
        log, created = WorkerJobLog.objects.get_or_create(
            task_id=task_id,
            defaults={
                "task_name": getattr(task, "name", "") or "",
                "state": "STARTED",
                "started_at": capture.started_at,
                "sent_at": _request_sent_at(request),
                "parent_id": getattr(request, "parent_id", None) or "",
                "arguments": getattr(request, "argsrepr", None) or "",
                "keyword_arguments": getattr(request, "kwargsrepr", None) or "",
            },
        )
        # A retry runs under the same task id: the row keeps the first attempt's times.
        if not created:
            log.state = "STARTED"
            log.save(update_fields=["state"])
        earlier = [] if created else list(log.lines)
    except Exception:
        logging.getLogger(__name__).warning("Could not record the start of task %s", task_id, exc_info=True)
        earlier = []
    job_memory.reset()  # this job's peak, not the process's biggest job's
    job_progress.start(
        job_progress.LiveJob(task_id, capture.started_at, capture.lines, earlier),
        monitor=not getattr(request, "is_eager", False),
    )


@task_postrun.connect
def _finish(task_id: str | None = None, task: Any = None, state: str | None = None, **_: Any) -> None:  # noqa: ANN401
    capture = _current.get()
    _current.set(None)
    live = job_progress.finish(task_id) if task_id else None
    if task_id is None or capture is None:
        return
    if live is not None:  # the last reading of its own peak; its programs have ended
        live.note_memory(job_memory.own_peak(), None)
    from .models import WorkerJobLog

    try:
        log, _created = WorkerJobLog.objects.get_or_create(
            task_id=task_id,
            defaults={"task_name": getattr(task, "name", "") or "", "started_at": capture.started_at},
        )
        # A retry's lines follow the earlier attempt's. The monitor may have written some of this
        # run's already: they are written again from the start, not appended a second time.
        earlier = live.earlier_lines if live is not None else log.lines
        room = max(0, MAX_LINES - len(earlier))
        log.lines = [*earlier, *capture.lines[:room]]
        log.omitted += capture.omitted + max(0, len(capture.lines) - room)
        log.state = state or ""
        log.finished_at = timezone.now()
        if live is not None and live.fraction is not None:
            # A job that succeeded got all the way, whatever it reported last (GAHKFlix's last report
            # is 95 %, before its frames). One that failed keeps where it stopped, and doing what.
            done = state == "SUCCESS"
            log.progress = 1.0 if done else live.fraction
            log.progress_step = "" if done else live.step
            log.progressed_at = log.finished_at
        if live is not None:
            for name, value in job_progress.memory_fields(live).items():
                setattr(log, name, value)
        log.save()
    except Exception:
        logging.getLogger(__name__).warning("Could not store the log of task %s", task_id, exc_info=True)
