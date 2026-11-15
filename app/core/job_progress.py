"""How far a running worker job has got, its log so far, and the watchdog that stops one that stalls.

A job reports with `report_progress(fraction, step)`: a fraction of the whole job, 0 to 1, and what
it is doing, in Danish, for the worker-jobs page. That only sets two attributes, so it may be
called per chunk. A thread of the worker process's own — the monitor — writes them to the job's
`WorkerJobLog` row every FLUSH_INTERVAL, with the log lines collected since (core/worker_logs.py):
the page shows a running job's progress and its log as they come, and a job that is killed keeps
the lines it logged up to the last write. The thread has its own database connection, so the
writes are seen at once, even while the job itself is inside a transaction.

THE WATCHDOG. A job that reports progress must keep making it: at least MIN_GAIN of the whole every
WINDOW, or else finish within it when less than that is left. One that does not is stopped by the
monitor with SIGUSR1, which is how Celery's prefork pool delivers a soft time limit: the job gets
SoftTimeLimitExceeded where it is, and handles it as it handles the time limit — a running ffmpeg
is killed, the job fails with what it was doing. So a stuck job gives up its slot within the hour
instead of at its hard time limit, which can then be generous (twelve hours) for the honest ones.
A job that reports no progress is not watched; its hard time limit stays its only limit.

Measured against the progress an hour earlier, from the job's start (at 0). A job that has only
just begun reporting is therefore held to the same pace from its start, never given a fresh hour.
"""

import logging
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from django.db import close_old_connections, connection
from django.utils import timezone

logger = logging.getLogger(__name__)

FLUSH_INTERVAL = 10.0  # seconds between writes of a job's progress and log
TICK = 5.0  # how often the monitor looks
WINDOW = 60 * 60.0  # seconds
MIN_GAIN = 0.10  # of the whole job, per WINDOW


@dataclass
class LiveJob:
    """What the monitor knows of one running job. Created by worker_logs as the job starts."""

    task_id: str
    started_at: datetime
    lines: list[dict[str, str]]  # the capture's own list: appended to by the job's log handler
    earlier_lines: list[dict[str, str]] = field(default_factory=list)  # an earlier attempt's
    started: float = field(default_factory=time.monotonic)
    thread_ident: int = field(default_factory=threading.get_ident)
    fraction: float | None = None
    step: str = ""
    # (monotonic time, fraction) as the monitor saw it, from (start, 0) on, back to WINDOW ago.
    samples: deque[tuple[float, float]] = field(default_factory=deque)
    written: tuple[float | None, str, int] = (None, "", 0)  # fraction, step, lines at the last write
    written_at: float = 0.0
    stopped: str = ""  # why the watchdog stopped it, once it has

    def __post_init__(self) -> None:
        self.samples.append((self.started, 0.0))


_jobs: dict[str, LiveJob] = {}
_lock = threading.Lock()
_monitor: list[threading.Thread] = []  # this process's monitor thread, once started
_current = threading.local()


def start(job: LiveJob, *, monitor: bool) -> None:
    """Register a starting job. With `monitor`, make sure this process's monitor thread runs: not
    for a job run eagerly (tests, a call from a request), which has no worker around it."""
    _current.job = job
    with _lock:
        _jobs[job.task_id] = job
    if monitor:
        _ensure_monitor()


def finish(task_id: str) -> LiveJob | None:
    _current.job = None
    with _lock:
        return _jobs.pop(task_id, None)


def current() -> LiveJob | None:
    """The job running in this thread, to report to from another one (see `report_progress`)."""
    return getattr(_current, "job", None)


def report_progress(fraction: float, step: str = "", job: LiveJob | None = None) -> None:
    """How far the running job has got: `fraction` of the whole, and what it is doing now. Free to
    call as often as convenient; nothing is written here. Does nothing outside a worker job.

    From a thread the job started (boto3 reads an upload's stream in one of its own), pass the
    `job` that `current()` gave in the job's own thread: this thread has none of its own.
    """
    job = job or current()
    if job is None:
        return
    job.fraction = min(1.0, max(0.0, fraction))
    if step:
        job.step = step


def stopped_reason() -> str:
    """Why the watchdog stopped the running job, for its error: "" when it did not (the soft time
    limit raised the same exception)."""
    job: LiveJob | None = getattr(_current, "job", None)
    return job.stopped if job is not None else ""


def stage(start: float, end: float, step: str) -> "Stage":
    """Part of a job, from `start` to `end` of the whole: report a fraction of the part itself."""
    return Stage(start, end, step)


@dataclass(frozen=True)
class Stage:
    start: float
    end: float
    step: str

    def __call__(self, fraction: float, job: LiveJob | None = None) -> None:
        report_progress(self.start + (self.end - self.start) * min(1.0, max(0.0, fraction)), self.step, job)


# --- The monitor --------------------------------------------------------------------------------


def _ensure_monitor() -> None:
    with _lock:
        # A forked pool process inherits the list, but not the thread in it.
        if _monitor and _monitor[0].is_alive():
            return
        thread = threading.Thread(target=_run, name="job-progress", daemon=True)
        _monitor[:] = [thread]
        thread.start()


def _run() -> None:
    while True:
        time.sleep(TICK)
        close_old_connections()  # this thread's own connection, kept between rounds like a request's
        try:
            tick(time.monotonic())
        except Exception:
            logger.warning("The job monitor failed a round.", exc_info=True)
            connection.close()


def tick(now: float) -> None:
    """One look at every running job: sample, write what changed, stop what stalled."""
    with _lock:
        jobs = list(_jobs.values())
    for job in jobs:
        if job.fraction is not None:
            _sample(job, now)
            if not job.stopped and (reason := stalled(job, now)):
                _stop(job, reason)
                write(job, now)
                continue
        if now - job.written_at >= FLUSH_INTERVAL and job.written != _state(job):
            write(job, now)


def _sample(job: LiveJob, now: float) -> None:
    job.samples.append((now, job.fraction or 0.0))
    # Keep one sample at or before the window's start: the one the gain is measured from.
    while len(job.samples) > 1 and job.samples[1][0] <= now - WINDOW:
        job.samples.popleft()


def stalled(job: LiveJob, now: float) -> str:
    """Why the job counts as stalled now, or "" if it does not."""
    if job.fraction is None or now - job.started < WINDOW:
        return ""
    since, then = job.samples[0]
    needed = min(MIN_GAIN, 1.0 - then)
    if job.fraction - then >= needed - 1e-9:
        return ""
    return (
        f"Stopped by the watchdog: {then:.0%} to {job.fraction:.0%} in the last "
        f"{(now - since) / 60:.0f} minutes, and a job must gain {MIN_GAIN:.0%} an hour"
        f"{' (or finish)' if needed < MIN_GAIN else ''}. Last step: {job.step or 'none reported'}."
    )


def _stop(job: LiveJob, reason: str) -> None:
    job.stopped = reason
    logger.error("Job %s: %s", job.task_id, reason)
    job.lines.append(
        {"at": timezone.localtime().isoformat(), "level": "ERROR", "logger": __name__, "message": reason}
    )
    # SIGUSR1 raises SoftTimeLimitExceeded in the job — but only where the prefork pool installed
    # that handler, and only in the thread signals go to: the job must be the process's main thread.
    handler = signal.getsignal(signal.SIGUSR1)
    if job.thread_ident == threading.main_thread().ident and callable(handler):
        os.kill(os.getpid(), signal.SIGUSR1)
    else:
        logger.warning("Job %s cannot be stopped from here; its hard time limit will.", job.task_id)


def _state(job: LiveJob) -> tuple[float | None, str, int]:
    return (job.fraction, job.step, len(job.lines))


def write(job: LiveJob, now: float) -> None:
    """Write the job's progress and its log so far to its row, unless the job has finished."""
    from .models import WorkerJobLog
    from .worker_logs import MAX_LINES

    state = _state(job)
    lines = list(job.lines)  # a copy: the job may log while this runs
    room = max(0, MAX_LINES - len(job.earlier_lines))
    fields: dict[str, object] = {"lines": [*job.earlier_lines, *lines[:room]]}
    if job.fraction is not None:
        fields |= {"progress": job.fraction, "progress_step": job.step, "progressed_at": timezone.now()}
    try:
        # Not after its end: the job's own final write (worker_logs._finish) is the last word.
        WorkerJobLog.objects.filter(task_id=job.task_id, finished_at__isnull=True).update(**fields)
    except Exception:
        logger.warning("Could not write the progress of job %s.", job.task_id, exc_info=True)
        return
    job.written, job.written_at = state, now
