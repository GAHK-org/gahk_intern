"""Public-site admin — administrator-gated. The legacy privilege-escalation and open
mass-mailer are structurally gone; this provides the legitimate admin screens, chiefly assigning the
monthly embedsgruppe roles."""

import ast
import datetime
import functools
import io
import json
import pickle  # nosec B403: _ResultUnpickler rejects every global/class lookup.
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Never

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DatabaseError, connection, transaction
from django.db.models import Avg, F
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_celery_beat.models import PeriodicTask
from kombu.exceptions import DecodeError

from config.celery import app as celery_app
from core.models import WorkerJobLog
from core.worker_logs import SENT_AT_HEADER, parse_sent_at

from . import admin_access
from .models import AdminAccessGrant, Resident, Role, RoleAssignment, active_period
from .permissions import (
    PREVIEW_SESSION_KEY,
    current_resident,
    effective_roles,
    require_can_preview,
    role_required,
)


@role_required("administrator")
def home(request: HttpRequest) -> HttpResponse:
    # The pending list rides on the front page as well as its own screen: the mail in
    # residents.tasks is best-effort, and this is the one page every administrator opens.
    return render(request, "siteadmin/home.html", {"pending_grants": admin_access.pending()})


# ---- Worker jobs: what waits, what runs, what ran, and what Beat will send next ----
#
# Read from the tables Celery and kombu keep in our own Postgres (CELERY_BROKER_URL and
# CELERY_RESULT_BACKEND), plus the worker's own record of each job (core.worker_logs). Every read
# tolerates those tables not being there: with another broker or result backend the page is empty
# rather than broken.
#
# THE TWO CLOCKS. Both tables store `timestamp without time zone`, but not in the same zone. Celery
# writes `date_done` as UTC; kombu writes `timestamp` with the worker's datetime.now(), which is
# settings.TIME_ZONE because Django's settings export TZ into the process. Each query turns them
# into aware values with AT TIME ZONE, so nothing past the SQL sees a naive one. (Shown side by
# side as they were stored, a job finished two hours before it was sent.)

JOB_PAGE_SIZE = 25
# Jobs listed under each task type in the queue; the rest of the type's are counted.
QUEUED_JOBS_SHOWN = 5
# Sub-jobs listed on a job's page; a photo import has one per picture, so all are counted.
CHILD_JOBS_SHOWN = 200
# The activity chart: one column an hour.
ACTIVITY_HOURS = 24
UNKNOWN_TASK = "Ukendt opgave"

# celery_taskmeta.status -> its name on the page and its chip, in the order the filter lists them.
# PENDING is never stored; the page uses it for a job still in the queue.
JOB_STATES = {
    "SUCCESS": ("Gennemført", "chip"),
    "FAILURE": ("Fejlet", "chip-neg"),
    "STARTED": ("Kører", "chip-brass"),
    "RETRY": ("Prøves igen", "chip-brass"),
    "REVOKED": ("Annulleret", "chip-quiet"),
    "PENDING": ("I kø", "chip-quiet"),
}
JOB_PERIODS = {
    "": ("Alt gemt", None),
    "24h": ("Seneste 24 timer", datetime.timedelta(hours=24)),
    "7d": ("Seneste 7 dage", datetime.timedelta(days=7)),
    "30d": ("Seneste 30 dage", datetime.timedelta(days=30)),
}
# Sort key -> label and ORDER BY clause over _history's output columns. The clauses are interpolated
# into SQL, so they must stay literals here, and the request value only ever selects a key.
JOB_SORTS = {
    "newest": ("Nyeste først", "done_at DESC NULLS LAST, task_id"),
    "oldest": ("Ældste først", "done_at ASC NULLS LAST, task_id"),
    "slowest": ("Længst køretid", "runtime DESC NULLS LAST, done_at DESC NULLS LAST, task_id"),
}


def _read(sql: str, params: Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]:
    """Rows from Celery's or kombu's tables — or none when they are not there. The savepoint keeps
    a failed read from aborting whatever transaction the request is in."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    except DatabaseError:
        return []


def _format_duration(duration: datetime.timedelta | None) -> str:
    """'45 ms', '3,2 sek.', '2 min. 3 sek.' or '1 t. 5 min.'; '' when there is no duration."""
    if duration is None:
        return ""
    milliseconds = max(0, duration // datetime.timedelta(milliseconds=1))
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    if milliseconds < 60_000:
        return f"{milliseconds / 1000:.1f} sek.".replace(".", ",", 1)
    minutes, seconds = divmod(milliseconds // 1000, 60)
    if minutes < 60:
        return f"{minutes} min. {seconds} sek."
    hours, minutes = divmod(minutes, 60)
    return f"{hours} t. {minutes} min."


def _between(start: datetime.datetime | None, end: datetime.datetime | None) -> str:
    return _format_duration(end - start) if start and end else ""


@functools.cache
def _task_registry() -> Mapping[str, Any]:
    """Celery's registry with every app's tasks module imported, as a worker has it. A web process
    otherwise imports a tasks module only once something there sends one of its tasks, and a task
    it has not met would be measured against the global time limit instead of its own."""
    celery_app.autodiscover_tasks(force=True)
    return celery_app.tasks


def _time_limit(task_name: str) -> datetime.timedelta:
    """How long a job of this task may run before the worker kills it: its own hard limit, or the
    global one."""
    task = _task_registry().get(task_name)
    return datetime.timedelta(seconds=getattr(task, "time_limit", None) or settings.CELERY_TASK_TIME_LIMIT)


class _TaskName:
    """'photo_album.tasks.build_media_derivatives' shows as `build_media_derivatives`, under
    `photo_album`."""

    task: str

    @property
    def name(self) -> str:
        return self.task.rpartition(".")[2]

    @property
    def app(self) -> str:
        return self.task.partition(".")[0] if "." in self.task else ""


def _state_label(status: str) -> str:
    return JOB_STATES[status][0] if status in JOB_STATES else status or "Ukendt"


def _state_class(status: str) -> str:
    return JOB_STATES[status][1] if status in JOB_STATES else "chip-quiet"


@dataclass
class Job(_TaskName):
    """One job as the page shows it, put together from Celery's result row, the broker message and
    the worker's record — whichever of them still has each part."""

    id: str
    task: str = UNKNOWN_TASK
    status: str = ""
    sent_at: datetime.datetime | None = None
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None
    queue: str = ""
    worker: str = ""
    retries: int = 0
    arguments: str = ""
    keyword_arguments: str = ""
    parent_id: str = ""
    root_id: str = ""
    origin: str = ""
    eta: str = ""
    expires: str = ""
    task_context: str = ""
    result: str | None = None
    traceback: str = ""
    # Set on a running job: how long it has run, and how long it may.
    elapsed: datetime.timedelta | None = None
    time_limit: datetime.timedelta | None = None

    @property
    def state_label(self) -> str:
        return _state_label(self.status)

    @property
    def state_class(self) -> str:
        return _state_class(self.status)

    @property
    def worker_name(self) -> str:
        """`media` for the node `media@3f2a9c…`: the container id says nothing to anybody."""
        return self.worker.partition("@")[0]

    @property
    def waited(self) -> str:
        """From sent to started: the time in the queue."""
        return _between(self.sent_at, self.started_at)

    @property
    def ran(self) -> str:
        return _between(self.started_at, self.finished_at)

    @property
    def running_for(self) -> str:
        return _format_duration(self.elapsed)

    @property
    def time_limit_label(self) -> str:
        return _format_duration(self.time_limit)

    @property
    def over_time_limit(self) -> bool:
        """A job still STARTED past its hard time limit is not running: the worker would have
        killed it. Its worker died under it and nothing marked it."""
        return bool(self.elapsed and self.time_limit and self.elapsed > self.time_limit)

    @property
    def limit_used(self) -> int:
        """How much of its time limit a running job has used, in percent (at most 100)."""
        if not (self.elapsed and self.time_limit):
            return 0
        return min(100, int(self.elapsed / self.time_limit * 100))

    def run(self, now: datetime.datetime) -> None:
        """Mark it as running since `started_at`, measured against its time limit."""
        if self.started_at:
            self.elapsed = now - self.started_at
        self.time_limit = _time_limit(self.task)

    def take_record(self, record: WorkerJobLog) -> None:
        """Fill in from the worker's record what the result row and the message did not have."""
        if self.task == UNKNOWN_TASK and record.task_name:
            self.task = record.task_name
        self.status = self.status or record.state
        self.sent_at = record.sent_at or self.sent_at
        self.started_at = record.started_at or self.started_at
        self.finished_at = self.finished_at or record.finished_at
        self.parent_id = record.parent_id or self.parent_id
        self.arguments = record.arguments or self.arguments
        self.keyword_arguments = record.keyword_arguments or self.keyword_arguments


class _ResultUnpickler(pickle.Unpickler):
    """Read legacy primitive result values without allowing pickle code execution."""

    def find_class(self, module: str, name: str) -> Never:
        raise pickle.UnpicklingError("global values are not permitted in task results")


def _display_task_result(result: object) -> str:
    """Return a display-safe Celery result, including results stored before JSON serialization."""
    if isinstance(result, memoryview):
        result = result.tobytes()
    try:
        value = celery_app.backend.decode(result)
    except (DecodeError, TypeError, UnicodeDecodeError, ValueError):
        if not isinstance(result, (bytes, bytearray)):
            return str(result)
        try:
            value = _ResultUnpickler(io.BytesIO(result)).load()
        except (EOFError, pickle.UnpicklingError, ValueError):
            return "Resultatet kunne ikke afkodes sikkert."
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def _task_argument_id(arguments: str) -> int | None:
    """Return the first positional task argument when it is a plain integer ID."""
    try:
        parsed = ast.literal_eval(arguments)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    if isinstance(parsed, (tuple, list)) and parsed:
        first_argument = parsed[0]
        if isinstance(first_argument, int) and not isinstance(first_argument, bool):
            return first_argument
    return None


def _add_photo_album_context(jobs: list[Job]) -> None:
    """Attach concise resource names for photo-album tasks with two batched database queries."""
    media_task = "photo_album.tasks.build_media_derivatives"
    import_task = "photo_album.tasks.process_album_import"
    argument_ids = [_task_argument_id(job.arguments) for job in jobs]
    media_ids: set[int] = {
        argument_id
        for job, argument_id in zip(jobs, argument_ids, strict=True)
        if job.task == media_task and argument_id is not None
    }
    import_ids: set[int] = {
        argument_id
        for job, argument_id in zip(jobs, argument_ids, strict=True)
        if job.task == import_task and argument_id is not None
    }
    if not media_ids and not import_ids:
        return

    from photo_album.models import Album, AlbumImport, Media

    media_by_id = {
        media.pk: media
        for media in Media.objects.filter(pk__in=media_ids)
        .select_related("album")
        .only("pk", "title", "album__id", "album__folder", "album__name")
    }
    imports_by_id = {
        album_import.pk: album_import
        for album_import in AlbumImport.objects.filter(pk__in=import_ids).only(
            "pk", "folder", "archive_name", "album_ids"
        )
    }
    album_ids = {
        album_id
        for album_import in imports_by_id.values()
        for album_id in album_import.album_ids
        if isinstance(album_id, int) and not isinstance(album_id, bool)
    }
    albums_by_id = {
        album.pk: album for album in Album.objects.filter(pk__in=album_ids).only("pk", "folder", "name")
    }

    for job, argument_id in zip(jobs, argument_ids, strict=True):
        if argument_id is not None and job.task == media_task and (media := media_by_id.get(argument_id)):
            job.task_context = f"Medie: {media.title} (album: {media.album})"
        elif (
            argument_id is not None
            and job.task == import_task
            and (album_import := imports_by_id.get(argument_id))
        ):
            imported_albums = [
                str(albums_by_id[album_id]) for album_id in album_import.album_ids if album_id in albums_by_id
            ]
            context = f"ZIP: {album_import.archive_name} (mappe: {album_import.folder})"
            if imported_albums:
                context += f"; albummer: {', '.join(imported_albums)}"
            job.task_context = context


def _take_messages(jobs: list[Job]) -> None:
    """Fill in from the broker what jobs without a worker's record lack: the sent time, the
    arguments, the parent. A job that failed or was annulled before a worker started it has no
    record, nor has one from before records were kept; the broker keeps its messages a day.

    LIKE on the raw text keeps the JSON parsing to the few rows that mention one of the ids.
    """
    missing = {job.id: job for job in jobs if job.sent_at is None}
    if not missing:
        return
    rows = _read(
        """
        SELECT DISTINCT ON (h->>'id') h->>'id', h->>%(sent_at)s, h->>'argsrepr', h->>'kwargsrepr', h->>'parent_id'
        FROM kombu_message AS m
        CROSS JOIN LATERAL (SELECT m.payload::jsonb->'headers' AS h) AS parsed
        WHERE m.payload LIKE ANY(%(mentions)s) AND h->>'id' = ANY(%(ids)s)
        ORDER BY h->>'id', m.id DESC
        """,
        {
            "sent_at": SENT_AT_HEADER,
            "mentions": [f"%{task_id}%" for task_id in missing],
            "ids": list(missing),
        },
    )
    for task_id, sent_at, arguments, keyword_arguments, parent_id in rows:
        job = missing[task_id]
        job.sent_at = parse_sent_at(sent_at)
        job.arguments = job.arguments or arguments or ""
        job.keyword_arguments = job.keyword_arguments or keyword_arguments or ""
        job.parent_id = job.parent_id or parent_id or ""


def _records(task_ids: list[str]) -> dict[str, WorkerJobLog]:
    """The worker's records of these jobs, without their logs."""
    return {
        record.task_id: record for record in WorkerJobLog.objects.filter(task_id__in=task_ids).defer("lines")
    }


# ---- the queue ----


@dataclass
class QueueGroup(_TaskName):
    """The messages of one task type waiting in one queue, the first QUEUED_JOBS_SHOWN of them listed."""

    queue: str
    task: str
    count: int
    jobs: list[Job] = field(default_factory=list)

    @property
    def oldest_sent_at(self) -> datetime.datetime | None:
        return self.jobs[0].sent_at if self.jobs else None

    @property
    def more(self) -> int:
        return self.count - len(self.jobs)


@dataclass
class Queue:
    groups: list[QueueGroup]

    @property
    def total(self) -> int:
        return sum(group.count for group in self.groups)

    @property
    def oldest_sent_at(self) -> datetime.datetime | None:
        return min((group.oldest_sent_at for group in self.groups if group.oldest_sent_at), default=None)


def _queue() -> Queue:
    """The messages no worker has taken yet, by queue and task type, in the order they will be taken.

    kombu takes the oldest visible message first. Its `timestamp` is still empty here — it is set
    when a worker takes the message — so the sent time is the header core.worker_logs stamps.
    """
    rows = _read(
        """
        SELECT task_id, queue, task, sent_at, argsrepr, kwargsrepr, eta, in_group FROM (
            SELECT
                m.id,
                q.name AS queue,
                h->>'id' AS task_id,
                h->>'task' AS task,
                h->>%(sent_at)s AS sent_at,
                h->>'argsrepr' AS argsrepr,
                h->>'kwargsrepr' AS kwargsrepr,
                h->>'eta' AS eta,
                row_number() OVER (PARTITION BY q.name, h->>'task' ORDER BY m.id) AS place,
                count(*) OVER (PARTITION BY q.name, h->>'task') AS in_group
            FROM kombu_message AS m
            JOIN kombu_queue AS q ON q.id = m.queue_id
            CROSS JOIN LATERAL (SELECT m.payload::jsonb->'headers' AS h) AS parsed
            WHERE m.visible
        ) AS waiting
        WHERE place <= %(shown)s
        ORDER BY id
        """,
        {"sent_at": SENT_AT_HEADER, "shown": QUEUED_JOBS_SHOWN},
    )
    groups: dict[tuple[str, str], QueueGroup] = {}
    jobs = []
    for task_id, queue, task, sent_at, arguments, keyword_arguments, eta, in_group in rows:
        job = Job(
            id=task_id or "",
            task=task or UNKNOWN_TASK,
            status="PENDING",
            queue=queue,
            sent_at=parse_sent_at(sent_at),
            arguments=arguments or "",
            keyword_arguments=keyword_arguments or "",
            eta=eta or "",
        )
        key = (job.queue, job.task)
        if key not in groups:
            groups[key] = QueueGroup(queue=job.queue, task=job.task, count=in_group)
        groups[key].jobs.append(job)
        jobs.append(job)
    _add_photo_album_context(jobs)
    return Queue(groups=list(groups.values()))


# ---- what runs ----


def _running(now: datetime.datetime) -> list[Job]:
    """Every job Celery has as STARTED, oldest first — including any whose worker died under it."""
    rows = _read(
        """
        SELECT task_id, name, date_done AT TIME ZONE 'UTC', worker, queue, retries
        FROM celery_taskmeta
        WHERE status = 'STARTED'
        ORDER BY date_done, task_id
        """
    )
    records = _records([row[0] for row in rows])
    jobs = []
    for task_id, name, started_at, worker, queue, retries in rows:
        job = Job(
            id=task_id,
            task=name or UNKNOWN_TASK,
            status="STARTED",
            # With track_started, Celery stores STARTED with date_done = when it started.
            started_at=started_at,
            worker=worker or "",
            queue=queue or "",
            retries=retries or 0,
        )
        if record := records.get(job.id):
            job.take_record(record)
        job.run(now)
        jobs.append(job)
    _take_messages(jobs)
    _add_photo_album_context(jobs)
    return jobs


@dataclass
class DaySummary:
    """The last 24 hours, for the tiles at the top."""

    succeeded: int = 0
    failed: int = 0
    last_failure_at: datetime.datetime | None = None
    average_runtime: datetime.timedelta | None = None

    @property
    def average(self) -> str:
        return _format_duration(self.average_runtime)


def _day_summary(now: datetime.datetime) -> DaySummary:
    since = now - datetime.timedelta(hours=24)
    summary = DaySummary()
    rows = _read(
        """
        SELECT status, count(*), max(date_done) AT TIME ZONE 'UTC'
        FROM celery_taskmeta
        WHERE date_done >= %(since)s AND status IN ('SUCCESS', 'FAILURE')
        GROUP BY status
        """,
        # Naive UTC, like the column, so the comparison can use its index.
        {"since": since.astimezone(datetime.UTC).replace(tzinfo=None)},
    )
    for status, count, last_at in rows:
        if status == "SUCCESS":
            summary.succeeded = count
        else:
            summary.failed, summary.last_failure_at = count, last_at
    summary.average_runtime = WorkerJobLog.objects.filter(
        state="SUCCESS", finished_at__gte=since, started_at__isnull=False
    ).aggregate(average=Avg(F("finished_at") - F("started_at")))["average"]
    return summary


def _live_context(now: datetime.datetime, queue: Queue | None = None) -> dict[str, object]:
    """What the top of the page shows and refreshes on its own: the tiles, what runs and the queue."""
    running = _running(now)
    return {
        "now": now,
        "queue": queue or _queue(),
        "running": [job for job in running if not job.over_time_limit],
        "stale": [job for job in running if job.over_time_limit],
        "day": _day_summary(now),
    }


# ---- what ran ----


@dataclass
class Bucket:
    """One hour of the activity chart, with its bars' heights as percentages of the chart."""

    hour: datetime.datetime
    succeeded: int
    failed: int
    succeeded_height: float = 0
    failed_height: float = 0

    @property
    def label(self) -> str:
        """The hour on the axis, every sixth (local time)."""
        local = timezone.localtime(self.hour)
        return f"{local:%H}" if local.hour % 6 == 0 else ""

    @property
    def span(self) -> str:
        local = timezone.localtime(self.hour)
        return f"{local:%H}:00–{local + datetime.timedelta(hours=1):%H}:00"


@dataclass
class Activity:
    buckets: list[Bucket]
    scale: int

    @property
    def succeeded(self) -> int:
        return sum(bucket.succeeded for bucket in self.buckets)

    @property
    def failed(self) -> int:
        return sum(bucket.failed for bucket in self.buckets)


def _nice_ceiling(value: int) -> int:
    """The axis' top: the smallest of 1, 2, 5, 10, 20, 50, … that is at least `value`."""
    step = 1
    while True:
        for factor in (1, 2, 5):
            if step * factor >= value:
                return step * factor
        step *= 10


def _activity(now: datetime.datetime) -> Activity:
    """Finished jobs per hour over the last ACTIVITY_HOURS hours, the current one included.

    Bucketed by the UTC hour: Copenhagen is a whole number of hours from UTC, so they are its hours too.
    """
    current = now.astimezone(datetime.UTC).replace(minute=0, second=0, microsecond=0)
    first = current - datetime.timedelta(hours=ACTIVITY_HOURS - 1)
    rows = _read(
        """
        SELECT date_trunc('hour', date_done) AT TIME ZONE 'UTC', status, count(*)
        FROM celery_taskmeta
        WHERE date_done >= %(since)s AND status IN ('SUCCESS', 'FAILURE')
        GROUP BY 1, 2
        """,
        {"since": first.replace(tzinfo=None)},
    )
    counts = {(hour, status): count for hour, status, count in rows}
    buckets = [
        Bucket(hour=hour, succeeded=counts.get((hour, "SUCCESS"), 0), failed=counts.get((hour, "FAILURE"), 0))
        for hour in (first + datetime.timedelta(hours=offset) for offset in range(ACTIVITY_HOURS))
    ]
    scale = _nice_ceiling(max((bucket.succeeded + bucket.failed for bucket in buckets), default=0))
    for bucket in buckets:
        bucket.succeeded_height = bucket.succeeded / scale * 100
        bucket.failed_height = bucket.failed / scale * 100
    return Activity(buckets=buckets, scale=scale)


@dataclass
class TaskType(_TaskName):
    """Everything kept of one task: its results by status, plus what of it waits now."""

    task: str
    counts: dict[str, int] = field(default_factory=dict)
    last_at: datetime.datetime | None = None
    last_status: str = ""
    queued: int = 0
    average_runtime: datetime.timedelta | None = None

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def succeeded(self) -> int:
        return self.counts.get("SUCCESS", 0)

    @property
    def failed(self) -> int:
        return self.counts.get("FAILURE", 0)

    @property
    def running(self) -> int:
        return self.counts.get("STARTED", 0)

    @property
    def other(self) -> int:
        return self.total - self.succeeded - self.failed - self.running

    @property
    def last_state_label(self) -> str:
        return _state_label(self.last_status)

    @property
    def last_state_class(self) -> str:
        return _state_class(self.last_status)

    @property
    def average(self) -> str:
        return _format_duration(self.average_runtime)


def _task_types(queue: Queue) -> list[TaskType]:
    """Every task with a result kept or a message waiting, by name."""
    types: dict[str, TaskType] = {}
    for name, status, count, last_at in _read(
        """
        SELECT name, status, count(*), max(date_done) AT TIME ZONE 'UTC'
        FROM celery_taskmeta
        GROUP BY name, status
        """
    ):
        task = name or UNKNOWN_TASK
        task_type = types.setdefault(task, TaskType(task=task))
        task_type.counts[status] = count
        if last_at and (task_type.last_at is None or last_at > task_type.last_at):
            task_type.last_at, task_type.last_status = last_at, status
    for group in queue.groups:
        types.setdefault(group.task, TaskType(task=group.task)).queued += group.count
    runtimes = (
        WorkerJobLog.objects.filter(state="SUCCESS", task_name__in=types, started_at__isnull=False)
        .exclude(finished_at=None)
        .values("task_name")
        .annotate(average=Avg(F("finished_at") - F("started_at")))
    )
    for row in runtimes:
        types[row["task_name"]].average_runtime = row["average"]
    return sorted(types.values(), key=lambda task_type: task_type.task)


@dataclass(frozen=True)
class JobFilters:
    """The history's filters, as given in the query string and checked against what is offered."""

    task: str = ""
    status: str = ""
    period: str = ""
    sort: str = "newest"
    include_children: bool = False

    @classmethod
    def from_query(cls, query: QueryDict) -> "JobFilters":
        status = query.get("status", "")
        period = query.get("period", "")
        sort = query.get("sort", "newest")
        return cls(
            task=query.get("type", "")[:255],
            status=status if status in JOB_STATES else "",
            period=period if period in JOB_PERIODS else "",
            sort=sort if sort in JOB_SORTS else "newest",
            include_children=query.get("children") == "1",
        )

    def where(self, *, with_status: bool = True) -> tuple[str, dict[str, object]]:
        """The WHERE clause over _HISTORY_FROM and its parameters. Only literals go into the clause."""
        clauses = []
        params: dict[str, object] = {}
        if with_status and self.status:
            clauses.append("result.status = %(status)s")
            params["status"] = self.status
        if self.task:
            clauses.append("result.name = %(task)s")
            params["task"] = self.task
        if since := JOB_PERIODS[self.period][1]:
            # Naive UTC, like the column, so the comparison can use its index.
            clauses.append("result.date_done >= %(since)s")
            params["since"] = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - since
        if not self.include_children:
            clauses.append("coalesce(job.parent_id, '') = ''")
        return " AND ".join(clauses) or "TRUE", params


# Every job with a result, and the worker's record of it (core_workerjoblog is WorkerJobLog's table)
# where there is one. A job sent by another job carries its `parent_id` in that record; one without
# a record (it ran before records were kept) reads as top-level.
_HISTORY_FROM = """
    FROM celery_taskmeta AS result
    LEFT JOIN core_workerjoblog AS job ON job.task_id = result.task_id
"""


def _history(filters: JobFilters, now: datetime.datetime, *, limit: int, offset: int) -> list[Job]:
    """One page of jobs, filtered, sorted and paged in SQL so every option reaches the whole table
    and a fan-out of sub-jobs cannot crowd the parent jobs out of a fixed window."""
    where, params = filters.where()
    rows = _read(
        f"""
        SELECT
            result.task_id,
            result.status,
            result.name,
            result.worker,
            result.queue,
            result.retries,
            result.date_done AT TIME ZONE 'UTC' AS done_at,
            CASE WHEN result.status <> 'STARTED'
                THEN (result.date_done AT TIME ZONE 'UTC') - job.started_at END AS runtime,
            job.sent_at,
            job.started_at,
            job.parent_id,
            job.arguments,
            job.keyword_arguments
        {_HISTORY_FROM}
        WHERE {where}
        ORDER BY {JOB_SORTS[filters.sort][1]}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**params, "limit": limit, "offset": offset},
    )
    jobs = []
    for (
        task_id,
        status,
        name,
        worker,
        queue,
        retries,
        done_at,
        _runtime,
        sent_at,
        started_at,
        parent_id,
        arguments,
        keyword_arguments,
    ) in rows:
        job = Job(
            id=task_id,
            task=name or UNKNOWN_TASK,
            status=status,
            sent_at=sent_at,
            started_at=started_at,
            worker=worker or "",
            queue=queue or "",
            retries=retries or 0,
            parent_id=parent_id or "",
            arguments=arguments or "",
            keyword_arguments=keyword_arguments or "",
        )
        if job.status == "STARTED":
            job.started_at = job.started_at or done_at
            job.run(now)
        else:
            job.finished_at = done_at
        jobs.append(job)
    _take_messages(jobs)
    _add_photo_album_context(jobs)
    return jobs


def _history_counts(filters: JobFilters) -> dict[str, int]:
    """How many jobs each status has under the other filters: the status chips, and the paging."""
    where, params = filters.where(with_status=False)
    rows = _read(
        f"SELECT result.status, count(*) {_HISTORY_FROM} WHERE {where} GROUP BY result.status", params
    )
    return dict(rows)


# ---- Beat ----

_EVERY_MINUTES = re.compile(r"(\d+)-59/(\d+)")
_HOURS = re.compile(r"\d+(,\d+)*")
_INTERVALS = {
    "days": ("Hver dag", "Hver {}. dag"),
    "hours": ("Hver time", "Hver {}. time"),
    "minutes": ("Hvert minut", "Hvert {}. minut"),
    "seconds": ("Hvert sekund", "Hvert {}. sekund"),
}


def _plan(task: PeriodicTask) -> str:
    """The schedule in words where it is one of the usual shapes, else as cron fields."""
    if crontab := task.crontab:
        rest = (crontab.month_of_year, crontab.day_of_week)
        if crontab.minute.isdigit() and _HOURS.fullmatch(crontab.hour) and rest == ("*", "*"):
            times = [f"{int(hour):02d}:{int(crontab.minute):02d}" for hour in crontab.hour.split(",")]
            at = "kl. " + (" og ".join([", ".join(times[:-1]), times[-1]]) if len(times) > 1 else times[0])
            if crontab.day_of_month == "*":
                return f"Hver dag {at}"
            if crontab.day_of_month.isdigit():
                return f"Den {crontab.day_of_month}. i måneden {at}"
        every = _EVERY_MINUTES.fullmatch(crontab.minute)
        if every and (crontab.hour, crontab.day_of_month, *rest) == ("*", "*", "*", "*"):
            return f"Hvert {every[2]}. minut, fra :{int(every[1]):02d}"
        return " ".join(
            (crontab.minute, crontab.hour, crontab.day_of_month, crontab.month_of_year, crontab.day_of_week)
        )
    if interval := task.interval:
        if interval.period not in _INTERVALS:
            return str(interval)
        once, many = _INTERVALS[interval.period]
        return once if interval.every == 1 else many.format(interval.every)
    return str(task.schedule)


# Beat stores last_run_at when it syncs, every few minutes, so a next run a little in the past is
# Beat being behind on its bookkeeping, not Beat being down.
OVERDUE_AFTER = datetime.timedelta(minutes=5)


@dataclass
class Schedule(_TaskName):
    title: str
    task: str
    plan: str
    last_run_at: datetime.datetime | None
    next_run_at: datetime.datetime | None
    total_run_count: int
    overdue: bool
    last: TaskType | None


def _schedules(types: list[TaskType], now: datetime.datetime) -> list[Schedule]:
    """Beat's enabled entries, each with when it runs next and how its task last ended."""
    by_task = {task_type.task: task_type for task_type in types}
    schedules = []
    for task in PeriodicTask.objects.filter(enabled=True).select_related(
        "interval", "crontab", "solar", "clocked"
    ):
        try:
            next_run_at = now + task.schedule.remaining_estimate(task.last_run_at or now)
        except (AttributeError, TypeError, ValueError):
            next_run_at = None
        schedules.append(
            Schedule(
                title=task.name,
                task=task.task,
                plan=_plan(task),
                last_run_at=task.last_run_at,
                next_run_at=next_run_at,
                total_run_count=task.total_run_count,
                overdue=bool(next_run_at and next_run_at < now - OVERDUE_AFTER),
                last=by_task.get(task.task),
            )
        )
    far = now + datetime.timedelta(days=3650)
    return sorted(schedules, key=lambda schedule: (schedule.next_run_at or far, schedule.title))


# ---- one job ----


def _job_details(task_id: str, now: datetime.datetime) -> tuple[Job, WorkerJobLog | None] | None:
    """One job from all three places — the result, the newest delivery of its message (a retry is
    republished under the same id), and the worker's record — or None when none has it."""
    result_rows = _read(
        """
        SELECT status, date_done AT TIME ZONE 'UTC', result, traceback, name, worker, retries, queue
        FROM celery_taskmeta
        WHERE task_id = %(id)s
        """,
        {"id": task_id},
    )
    # position() on the raw text discards almost every row before any payload is parsed as JSON.
    message_rows = _read(
        """
        SELECT q.name, m.visible, m.timestamp AT TIME ZONE %(zone)s, m.payload::jsonb->'headers'
        FROM kombu_message AS m
        JOIN kombu_queue AS q ON q.id = m.queue_id
        WHERE position(%(id)s in m.payload) > 0 AND m.payload::jsonb->'headers'->>'id' = %(id)s
        ORDER BY m.id DESC
        LIMIT 1
        """,
        {"id": task_id, "zone": settings.TIME_ZONE},
    )
    record = WorkerJobLog.objects.filter(task_id=task_id).first()
    if not result_rows and not message_rows and record is None:
        return None

    job = Job(id=task_id)
    if message_rows:
        queue, visible, taken_at, raw_headers = message_rows[0]
        # Django loads jsonb as text on every connection, for JSONField to decode itself.
        headers = json.loads(raw_headers) if raw_headers else {}
        job.task = headers.get("task") or job.task
        job.queue = queue
        job.status = "PENDING" if visible else ""
        job.sent_at = parse_sent_at(headers.get(SENT_AT_HEADER))
        # When a worker took the message: as near to its start as is known without a record.
        job.started_at = taken_at
        job.arguments = headers.get("argsrepr") or ""
        job.keyword_arguments = headers.get("kwargsrepr") or ""
        job.parent_id = headers.get("parent_id") or ""
        job.root_id = headers.get("root_id") or ""
        job.origin = headers.get("origin") or ""
        job.eta = headers.get("eta") or ""
        job.expires = headers.get("expires") or ""
    if record is not None:
        job.take_record(record)
    if result_rows:
        status, done_at, value, traceback, name, worker, retries, queue = result_rows[0]
        job.status = status
        job.task = name or job.task
        job.worker = worker or ""
        job.retries = retries or 0
        job.queue = queue or job.queue
        job.result = _display_task_result(value) if value is not None else None
        job.traceback = traceback or ""
        if job.status == "STARTED":
            # Celery's start time beats the message's taken time; the worker's own beats both.
            if record is None:
                job.started_at = done_at or job.started_at
            job.finished_at = None
        else:
            job.finished_at = done_at
    if job.status == "STARTED":
        job.run(now)
    _add_photo_album_context([job])
    return job, record


def _child_jobs(parent_id: str) -> tuple[list[Job], dict[str, int]]:
    """The jobs this one sent — those a worker has had, from their records, then those still in
    the queue — and how many of them are in each state."""
    children = [
        Job(
            id=task_id,
            task=name or UNKNOWN_TASK,
            status=status or "",
            sent_at=sent_at,
            started_at=started_at,
            finished_at=done_at,
            arguments=arguments or "",
        )
        for task_id, name, status, sent_at, started_at, done_at, arguments in _read(
            """
            SELECT
                job.task_id,
                job.task_name,
                coalesce(result.status, job.state),
                job.sent_at,
                job.started_at,
                CASE WHEN result.status <> 'STARTED' THEN result.date_done AT TIME ZONE 'UTC' END,
                job.arguments
            FROM core_workerjoblog AS job
            LEFT JOIN celery_taskmeta AS result ON result.task_id = job.task_id
            WHERE job.parent_id = %(parent)s
            ORDER BY job.started_at, job.task_id
            """,
            {"parent": parent_id},
        )
    ]
    children += [
        Job(
            id=task_id,
            task=name or UNKNOWN_TASK,
            status="PENDING",
            sent_at=parse_sent_at(sent_at),
            arguments=arguments or "",
        )
        for task_id, name, sent_at, arguments in _read(
            """
            SELECT h->>'id', h->>'task', h->>%(sent_at)s, h->>'argsrepr'
            FROM kombu_message AS m
            CROSS JOIN LATERAL (SELECT m.payload::jsonb->'headers' AS h) AS parsed
            WHERE m.visible AND position(%(parent)s in m.payload) > 0 AND h->>'parent_id' = %(parent)s
            ORDER BY m.id
            """,
            {"parent": parent_id, "sent_at": SENT_AT_HEADER},
        )
    ]
    counts: dict[str, int] = {}
    for child in children:
        counts[child.status] = counts.get(child.status, 0) + 1
    shown = children[:CHILD_JOBS_SHOWN]
    _add_photo_album_context(shown)
    return shown, counts


def _state_counts(counts: Mapping[str, int], selected: str = "") -> list[tuple[str, str, int]]:
    """(status, label, count) in the order of JOB_STATES, then any status it does not know; a
    status with no jobs only when it is the one selected."""
    order = [*JOB_STATES, *sorted(set(counts) - set(JOB_STATES))]
    return [
        (status, _state_label(status), counts.get(status, 0))
        for status in order
        if counts.get(status) or status == selected
    ]


@role_required("administrator")
def worker_jobs(request: HttpRequest) -> HttpResponse:
    now = timezone.now()
    filters = JobFilters.from_query(request.GET)
    queue = _queue()
    types = _task_types(queue)
    counts = _history_counts(filters)
    total = counts.get(filters.status, 0) if filters.status else sum(counts.values())
    page = Paginator(range(total), JOB_PAGE_SIZE).get_page(request.GET.get("page"))
    jobs = _history(filters, now, limit=JOB_PAGE_SIZE, offset=(page.number - 1) * JOB_PAGE_SIZE)
    return render(
        request,
        "siteadmin/worker_jobs.html",
        {
            **_live_context(now, queue),
            "activity": _activity(now),
            "task_types": types,
            "type_names": [task_type.task for task_type in types],
            "schedules": _schedules(types, now),
            "filters": filters,
            "status_counts": _state_counts(counts, filters.status),
            "history_total": sum(counts.values()),
            "page": page,
            "jobs": jobs,
            "periods": [(key, label) for key, (label, _since) in JOB_PERIODS.items()],
            "sorts": [(key, label) for key, (label, _order) in JOB_SORTS.items()],
        },
    )


def worker_jobs_live(request: HttpRequest) -> HttpResponse:
    """The top of the worker-jobs page, for its own poll.

    Not decorated, like den_hurtige's feed poll: role_required redirects to the login page or
    raises, and htmx would swap the login page or a 403 body into the dashboard. A 204 makes htmx
    leave it as it is, and hands an unauthorised caller nothing either way.
    """
    if not request.user.is_authenticated or Role.ADMINISTRATOR not in effective_roles(request):
        return HttpResponse(status=204)
    return render(request, "siteadmin/_worker_jobs_live.html", _live_context(timezone.now()))


@role_required("administrator")
def worker_job_detail(request: HttpRequest, task_id: str) -> HttpResponse:
    now = timezone.now()
    details = _job_details(task_id, now)
    if details is None:
        raise Http404("worker job not found")
    job, record = details
    children, child_counts = _child_jobs(task_id)
    return render(
        request,
        "siteadmin/worker_job_detail.html",
        {
            "job": job,
            "job_log": record,
            "children": children,
            "child_total": sum(child_counts.values()),
            "child_counts": _state_counts(child_counts),
        },
    )


@role_required("administrator")
def roles(request: HttpRequest) -> HttpResponse:
    year, month = active_period()
    if request.method == "POST":
        raw_rid = request.POST.get("resident") or ""
        role = request.POST.get("role")
        action = request.POST.get("action")
        # The id must be one of the residents this screen lists — someone living here in the active
        # month — not any row in the table: otherwise a hand-edited POST could hand a role (even
        # `administrator`) to a moved-out or never-resident account. isdigit() also turns a junk id
        # into a refusal instead of a 500.
        rid = int(raw_rid) if raw_rid.isdigit() else 0
        listed = Resident.objects.filter(id=rid, residencies__year=year, residencies__month=month).exists()
        if not listed:
            messages.error(request, "Beboeren bor ikke på kollegiet i den viste måned.")
        elif role in Role.values:
            if action == "add":
                RoleAssignment.objects.get_or_create(resident_id=rid, role=role, year=year, month=month)
                Resident.objects.filter(id=rid).update(is_staff=True)
                # Stamp the granter so they cannot also be the approver (residents.admin_access).
                # The grant itself is opened by the signal, which covers the other three doors.
                # A refused grant is reopened here too: granting the role again is a new request.
                if role == Role.ADMINISTRATOR:
                    granter = current_resident(request)
                    AdminAccessGrant.objects.filter(
                        resident_id=rid, status=AdminAccessGrant.Status.PENDING, requested_by=None
                    ).update(requested_by=granter)
                    admin_access.reopen_denied(rid, granter)
            elif action == "remove":
                RoleAssignment.objects.filter(resident_id=rid, role=role, year=year, month=month).delete()
        return redirect("siteadmin:roles")

    residents = (
        Resident.objects.filter(residencies__year=year, residencies__month=month)
        .distinct()
        .order_by("first_name", "last_name")
    )
    role_map: dict[int, list[str]] = {}
    for ra in RoleAssignment.objects.filter(year=year, month=month):
        role_map.setdefault(ra.resident_id, []).append(ra.role)
    rows = [(r, role_map.get(r.id, [])) for r in residents]
    return render(
        request,
        "siteadmin/roles.html",
        {"rows": rows, "all_roles": Role.choices, "period": f"{year}-{month:02d}"},
    )


# ---- Role preview ("view site as role") — gated on the REAL admin role (require_can_preview) so an
# admin previewing "beboer" can still end the preview. ----
@require_can_preview
def preview(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "siteadmin/preview.html",
        {"all_roles": Role.choices, "current": request.session.get(PREVIEW_SESSION_KEY)},
    )


@require_POST
@require_can_preview
def preview_set(request: HttpRequest) -> HttpResponseRedirect:
    mode = request.POST.get("mode")
    if mode == "clear":
        request.session.pop(PREVIEW_SESSION_KEY, None)
    elif mode == "resident":
        request.session[PREVIEW_SESSION_KEY] = []
    elif mode == "admin":
        request.session[PREVIEW_SESSION_KEY] = [Role.ADMINISTRATOR]
    else:  # mode == "role"
        role = request.POST.get("role")
        request.session[PREVIEW_SESSION_KEY] = [role] if role in Role.values else []
    return redirect("dashboard")


# ---- DEV-ONLY simulated clock: fast-forward the month locally to test round rollover.
# Hard-gated on settings.DEBUG — 404 in prod, so it can never be reached there. ----
@require_POST
@login_required
def dev_clock_set(request: HttpRequest) -> HttpResponseRedirect:
    if not settings.DEBUG:
        raise Http404
    from core.clock import current_date
    from core.models import DevClock

    action = request.POST.get("action")
    clock = DevClock.get()
    if action == "reset":
        clock.simulated_date = None
        clock.save(update_fields=["simulated_date"])
    elif action == "advance":
        base = current_date()  # the current effective date (override or real)
        year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
        clock.simulated_date = datetime.date(year, month, 1)
        clock.save(update_fields=["simulated_date"])
    elif action == "set":
        try:
            clock.simulated_date = datetime.date.fromisoformat(request.POST["date"])
            clock.save(update_fields=["simulated_date"])
        except (KeyError, ValueError):
            pass
    ref = request.META.get("HTTP_REFERER", "")
    if ref and url_has_allowed_host_and_scheme(ref, {request.get_host()}, require_https=request.is_secure()):
        return redirect(ref)
    return redirect("dashboard")


@role_required("administrator")
def admin_access_view(request: HttpRequest) -> HttpResponse:
    """Accept or refuse a new administrator's Django-admin access (the two-person rule).

    Every administrator may open this; `can_decide` is what actually authorises each button, and it
    is re-checked on POST rather than trusted from the rendered page.
    """
    me = current_resident(request)
    if request.method == "POST":
        grant = AdminAccessGrant.objects.filter(pk=request.POST.get("grant") or 0).first()
        if grant is None or grant.status != AdminAccessGrant.Status.PENDING:
            messages.error(request, "Anmodningen findes ikke længere.")
        elif not admin_access.can_decide(me, grant):
            messages.error(request, "Du kan ikke afgøre denne anmodning.")
        else:
            approve = request.POST.get("action") == "approve"
            admin_access.decide(grant, me, approve=approve)
            messages.success(
                request,
                f"{grant.resident.full_name} er {'godkendt' if approve else 'afvist'}.",
            )
        return redirect("siteadmin:admin_access")

    rows = [(g, admin_access.can_decide(me, g)) for g in admin_access.pending()]
    decided = (
        AdminAccessGrant.objects.exclude(status=AdminAccessGrant.Status.PENDING)
        .select_related("resident", "decided_by")
        .order_by("-decided_at")[:20]
    )
    return render(request, "siteadmin/admin_access.html", {"rows": rows, "decided": decided})
