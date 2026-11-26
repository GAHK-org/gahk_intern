"""Demo worker jobs for `manage.py seed_demo`: the worker-jobs admin page in every state it shows.

The page reads Celery's result rows (celery_taskmeta) and the worker's own record of each job
(WorkerJobLog). A seeded database has neither — no worker has run yet — and a worker on a Mac never
measures memory (core/job_memory.py reads /proc). Over the last three days, this seeds:

  * running now: a GAHKFlix encode hours in, with its progress and its peak memory so far, ffmpeg
    the largest program; and a photo's thumbnail started a second ago, not measured yet;
  * one whose worker died under it: still running past its time limit, in red, until the hourly
    close_lost_jobs closes it;
  * one that ran the worker out of memory, near worker-media's 1.5 GB, closed as lost, and the
    quarter-hourly recover_stalled sending it again as a sub-job that then succeeded;
  * failed with an error (the mail server refused), and stopped by the watchdog for getting nowhere;
  * finished: encodes, an album ZIP import with a thumbnail sub-job per picture, a ZIP for download,
    thumbnails of an upload, and the hourly and nightly Beat jobs, for the tiles, the chart and the
    task types;
  * the oldest jobs without memory figures ("–"), as jobs from before it was measured have.

Not seeded: the queue. Every local setup runs a worker on all three queues (docker-compose.yml),
which would take a seeded message within seconds, and run it. Nor Beat's schedules: Beat writes
them itself as it starts.

The rows are written directly, in the shapes Celery and core.worker_logs write them: running the
tasks would encode, import and mail for real. They name seeded films, photos and albums, so each job
says what it worked on. Their task ids are fixed, so seeding again replaces them.
"""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from uuid import NAMESPACE_URL, uuid5

from django.db import connection
from django.utils import timezone

from config.celery import app as celery_app
from residents.models import Resident

from .models import WorkerJobLog
from .tasks import LOST_JOB_TRACEBACK

MiB = 1024 * 1024
# Celery's node names as production has them (`-n default@%h`, `-n media@%h`); the page shows the
# part before the "@".
DEFAULT_WORKER = "default@4d8a0e6b1c52"
MEDIA_WORKER = "media@7c1e4b2f9a03"
# Jobs that ended longer ago than this have no memory figures, as jobs from before it was measured.
MEASURED_FOR = timedelta(days=2)
_NAMESPACE = uuid5(NAMESPACE_URL, "https://intern.gahk.dk/admin/worker-jobs/demo")
# The hourly close_lost_jobs run, counted back from the latest, that closed the job lost to memory.
LOST_CLOSED_BY = 14

ENCODE = "gahkflix.tasks.process_video_file"
DERIVATIVES = "photo_album.tasks.build_media_derivatives"

SMTP_TRACEBACK = """Traceback (most recent call last):
  File "/app/.venv/lib/python3.13/site-packages/celery/app/trace.py", line 453, in trace_task
    R = retval = fun(*args, **kwargs)
  File "/app/residents/tasks.py", line 35, in notify_admin_access_request
    return send_mail(
  File "/app/.venv/lib/python3.13/site-packages/django/core/mail/backends/smtp.py", line 81, in open
    self.connection = self.connection_class(self.host, self.port, **connection_params)
  File "/usr/local/lib/python3.13/socket.py", line 849, in create_connection
    sock.connect(sa)
ConnectionRefusedError: [Errno 111] Connection refused
"""


class _Line(NamedTuple):
    after: timedelta  # since the job started
    level: str
    message: str
    logger: str = ""  # the job's own, unless given


@dataclass
class _Job:
    key: str  # its task id is made from this
    task: str
    queue: str
    started: datetime
    ran: timedelta | None = None  # None: it is running
    state: str = "SUCCESS"
    arguments: str = "()"
    sent: datetime | None = None  # a moment before it started, unless given
    result: Any = None
    traceback: str = ""
    lines: tuple[_Line, ...] = ()
    logger: str = ""  # its task's module, where its lines come from
    progress: float | None = None
    step: str = ""
    progressed: datetime | None = None
    parent: str = ""  # the key of the job that sent it
    memory: int | None = None  # its peak, in bytes
    program: tuple[str, int] | None = None  # the largest program it ran, and that one's peak

    @property
    def task_id(self) -> str:
        return task_id(self.key)

    @property
    def finished(self) -> datetime | None:
        return None if self.ran is None else self.started + self.ran


def task_id(key: str) -> str:
    return str(uuid5(_NAMESPACE, key))


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    if not residents:
        return 0
    jobs = [
        *_beat_jobs(now, rng),
        *_film_jobs(now),
        *_photo_jobs(residents, now, rng),
        *_other_jobs(now),
    ]
    _ensure_result_table()
    ids = [job.task_id for job in jobs]
    WorkerJobLog.objects.filter(task_id__in=ids).delete()
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM celery_taskmeta WHERE task_id = ANY(%s)", [ids])
    for job in jobs:
        _store(job, now)
    return len(jobs)


def wipe() -> None:
    """Celery's result rows, for --fresh: they go with the worker's records (WorkerJobLog), as
    either without the other is half a job on the page."""
    if "celery_taskmeta" in connection.introspection.table_names():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM celery_taskmeta")


# ---- the jobs ----


def _hourly(now: datetime, minute: int, count: int) -> list[datetime]:
    """The last `count` runs of a Beat entry at `minute` past every hour, newest first."""
    last = now.replace(minute=minute, second=0, microsecond=0)
    if last > now:
        last -= timedelta(hours=1)
    return [last - timedelta(hours=hours) for hours in range(count)]


def _daily(now: datetime, hour: int, minute: int, count: int) -> list[datetime]:
    """The last `count` runs of a Beat entry at hour:minute, Danish time, newest first."""
    local = timezone.localtime(now)
    last = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if last > local:
        last -= timedelta(days=1)
    return [last - timedelta(days=days) for days in range(count)]


def _quarterly(after: datetime, minute: int) -> datetime:
    """The first run, at or after `after`, of a Beat entry every quarter hour from `minute`."""
    run = after.replace(minute=minute, second=0, microsecond=0)
    while run < after:
        run += timedelta(minutes=15)
    return run


def _beat_jobs(now: datetime, rng: random.Random) -> list[_Job]:
    """The routine jobs Beat sends, for the activity chart, the tiles and the task types. Hourly
    ones over the last day and nightly ones over three, at their real times (config/settings.py)."""
    jobs = []
    for index, at in enumerate(_hourly(now, 35, 24)):
        closed = index == LOST_CLOSED_BY  # the one that closed the encode lost to memory (_film_jobs)
        jobs.append(
            _Job(
                f"close-lost-jobs/{index}",
                "core.tasks.close_lost_jobs",
                "celery",
                at + timedelta(seconds=1),
                timedelta(milliseconds=rng.randint(30, 90)),
                result=int(closed),
                lines=(
                    _Line(
                        timedelta(milliseconds=20),
                        "WARNING",
                        "Closed 1 job(s) whose worker was lost while they ran.",
                    ),
                )
                if closed
                else (),
                logger="core.tasks",
                memory=rng.randint(118, 131) * MiB,
            )
        )
    nightly = [
        ("cms.tasks.rebuild_gahk_web", 0, 5, "celery", timedelta(seconds=4), "ok"),
        ("photo_album.tasks.process_pending_media", 2, 0, "celery", timedelta(seconds=1), 0),
        ("notifications.tasks.purge_expired_notifications", 3, 30, "celery", timedelta(milliseconds=210), 14),
        ("core.tasks.purge_worker_job_logs", 4, 40, "celery", timedelta(milliseconds=340), 61),
        ("core.tasks.purge_worker_job_results", 4, 45, "celery", timedelta(milliseconds=280), 61),
        ("photo_album.tasks.measure_photo_album_storage", 5, 0, "media", timedelta(minutes=3, seconds=12), 3),
        ("events.tasks.remind_rsvp_deadlines", 17, 0, "celery", timedelta(milliseconds=520), 2),
    ]
    for task, hour, minute, queue, ran, result in nightly:
        for index, at in enumerate(_daily(now, hour, minute, 3)):
            jobs.append(
                _Job(
                    f"{task}/{index}",
                    task,
                    queue,
                    at + timedelta(seconds=1),
                    ran * rng.uniform(0.8, 1.25),
                    result=result,
                    memory=rng.randint(121, 164) * MiB if queue == "celery" else rng.randint(210, 260) * MiB,
                )
            )
    return jobs


def _film_jobs(now: datetime) -> list[_Job]:
    """GAHKFlix encodes, the media worker's longest jobs and its biggest users of memory: ffmpeg
    the largest program."""
    from gahkflix.models import VideoFile
    from gahkflix.services import NotProcessed

    files = {f.video.title: f for f in VideoFile.objects.select_related("video").order_by("pk")}
    jobs: list[_Job | None] = []

    def encode(key: str, title: str, started: datetime, attempt: int = 1, **fields: Any) -> _Job | None:  # noqa: ANN401
        if (video_file := files.get(title)) is None:
            return None  # the GAHKFlix demo made no such film (no ffmpeg)
        message = f"Processing {video_file.name_and_size}, attempt {attempt}, for video {video_file.video_id} ({title})"
        fields.setdefault("lines", (_Line(timedelta(milliseconds=40), "INFO", message),))
        return _Job(
            key, ENCODE, "media", started, arguments=f"({video_file.pk},)", logger="gahkflix.tasks", **fields
        )

    # Running now, two and a half hours in and about half done: the live progress, the watchdog's
    # note, and the memory "so far", ffmpeg's far above the worker's own.
    jobs.append(
        encode(
            "encode-running",
            "Revy",
            now - timedelta(hours=2, minutes=40),
            state="STARTED",
            progress=0.46,
            step="Behandler filmen",
            progressed=now - timedelta(seconds=7),
            memory=268 * MiB,
            program=("ffmpeg", 1043 * MiB),
        )
    )

    # Ran the worker out of memory (as on 2026-11-15): killed with nothing recorded, so it stayed
    # "running" until close_lost_jobs closed it, past its 12-hour limit. Its peak, written as it ran,
    # is what the job page is for: the worker's own near worker-media's 1.5 GB limit.
    closed_at = _hourly(now, 35, LOST_CLOSED_BY + 1)[-1] + timedelta(seconds=1)
    lost_start = closed_at - timedelta(hours=12, minutes=20)
    killed_at = lost_start + timedelta(minutes=52)
    jobs.append(
        encode(
            "encode-lost",
            "Julefrokost",
            lost_start,
            ran=timedelta(hours=12),  # close_lost_jobs: the latest it can have ended
            state="FAILURE",
            traceback=LOST_JOB_TRACEBACK,
            progress=0.71,
            step="Behandler filmen",
            progressed=killed_at,
            memory=1423 * MiB,
            program=("ffmpeg", 702 * MiB),
        )
    )
    # recover_stalled saw its heartbeat go stale and sent it again: a sub-job of that Beat run.
    recovered_at = _quarterly(killed_at + timedelta(minutes=31), 13)
    jobs.append(
        _Job(
            "recover-stalled-after-kill",
            "gahkflix.tasks.recover_stalled",
            "celery",
            recovered_at + timedelta(seconds=1),
            timedelta(milliseconds=180),
            result=1,
            memory=141 * MiB,
        )
    )
    jobs.append(
        encode(
            "encode-recovered",
            "Julefrokost",
            recovered_at + timedelta(seconds=2),
            attempt=2,
            ran=timedelta(hours=2, minutes=5, seconds=41),
            result=True,
            parent="recover-stalled-after-kill",
            memory=236 * MiB,
            program=("ffmpeg", 941 * MiB),
        )
    )

    # Stopped by the watchdog: it gained 5 points in an hour, and must gain 10. A failed job shows
    # how far it got and what it was doing.
    stuck_start = now - timedelta(hours=21)
    why = (
        "Stopped by the watchdog: 18% to 23% in the last 60 minutes, and a job must gain 10% an hour. "
        "Last step: Behandler filmen."
    )
    stuck = encode(
        "encode-stuck",
        "Pilot",
        stuck_start,
        ran=timedelta(hours=2, minutes=11),
        state="FAILURE",
        progress=0.23,
        step="Behandler filmen",
        progressed=stuck_start + timedelta(hours=2, minutes=10, seconds=50),
        memory=244 * MiB,
        program=("ffmpeg", 1131 * MiB),
    )
    if stuck is not None:
        message = f"{files['Pilot'].name_and_size}: {why} Gave up."
        stuck.result = celery_app.backend.prepare_exception(NotProcessed(message))
        stuck.traceback = (
            "Traceback (most recent call last):\n"
            '  File "/app/gahkflix/tasks.py", line 87, in process_video_file\n'
            "    return services.process_file(video_file)\n"
            "celery.exceptions.SoftTimeLimitExceeded: SoftTimeLimitExceeded()\n\n"
            "The above exception was the direct cause of the following exception:\n\n"
            "Traceback (most recent call last):\n"
            '  File "/app/gahkflix/tasks.py", line 93, in process_video_file\n'
            '    raise services.NotProcessed(f"{video_file.name_and_size}: {why} Gave up.") from exc\n'
            f"gahkflix.services.NotProcessed: {message}\n"
        )
        stuck.lines = (
            *stuck.lines,
            _Line(
                timedelta(hours=2, minutes=10, seconds=55),
                "ERROR",
                why,
                "core.job_progress",
            ),
        )
        jobs.append(stuck)

    jobs.append(
        encode(
            "encode-done",
            "Opvasken",
            now - timedelta(hours=9),
            ran=timedelta(hours=1, minutes=52, seconds=8),
            result=True,
            memory=231 * MiB,
            program=("ffmpeg", 884 * MiB),
        )
    )
    # Three days old: before memory was measured, so its page shows "–".
    jobs.append(
        encode(
            "encode-unmeasured",
            "Opvasken",
            now - timedelta(days=3, hours=2),
            ran=timedelta(hours=1, minutes=48, seconds=30),
            result=True,
        )
    )
    return [job for job in jobs if job is not None]


def _photo_jobs(residents: list[Resident], now: datetime, rng: random.Random) -> list[_Job]:
    """An album ZIP import with a thumbnail sub-job per picture, an upload's thumbnails, a ZIP for
    download, and a thumbnail being made right now."""
    from photo_album.models import Album, AlbumImport, AlbumImportState, Media

    jobs: list[_Job] = []

    def thumbnail(key: str, media: Media, started: datetime, **fields: Any) -> _Job:  # noqa: ANN401
        fields.setdefault("ran", timedelta(milliseconds=rng.randint(380, 900)))
        fields.setdefault("result", True)
        fields.setdefault("memory", rng.randint(112, 139) * MiB)  # no program: Pillow, in the worker
        line = _Line(
            timedelta(milliseconds=15), "INFO", f"Converting {media.display_name_and_size}, in {media.album}."
        )
        return _Job(
            key,
            DERIVATIVES,
            "thumbnails",
            started,
            arguments=f"({media.pk},)",
            lines=(line,),
            logger="photo_album.tasks",
            **fields,
        )

    # The import: an AlbumImport row like the one the upload makes, so the job names its ZIP and
    # albums (and the photo album's own dashboard lists it).
    root = Album.objects.filter(folder="2026", name="Sommerfest", parent=None).first()
    if root is not None:
        albums = list(Album.objects.filter(path__startswith=root.path).order_by("depth", "pk"))
        pictures = list(
            Media.objects.filter(album__in=albums).select_related("album").order_by("album__depth", "pk")
        )
        requester = residents[min(5, len(residents) - 1)]
        started = now - timedelta(hours=5, minutes=12)
        ran = timedelta(minutes=6, seconds=14)
        skipped = ["__MACOSX/._IMG_2231.jpg: macOS-metadata (ignoreret)"]
        album_import = AlbumImport.objects.create(
            requested_by=requester,
            folder="2026",
            archive_name="sommerfest-2026.zip",
            archive="",  # deleted once imported
            task_id=task_id("album-import"),
            state=AlbumImportState.READY,
            album_ids=[album.pk for album in albums],
            skipped=skipped,
            completed_at=started + ran,
        )
        AlbumImport.objects.filter(pk=album_import.pk).update(created_at=started - timedelta(seconds=3))
        jobs.append(
            _Job(
                "album-import",
                "photo_album.tasks.process_album_import",
                "media",
                started,
                ran,
                arguments=f"({album_import.pk},)",
                result=True,
                logger="photo_album.tasks",
                lines=(
                    _Line(
                        timedelta(milliseconds=30),
                        "INFO",
                        f"Importing sommerfest-2026.zip into folder 2026 for {requester}.",
                    ),
                    _Line(ran, "INFO", f"Left out: {skipped[0]}"),
                    _Line(
                        ran,
                        "INFO",
                        f"Imported {len(pictures)} media item(s) into {len(albums)} album(s): "
                        f"{', '.join(album.name for album in albums)}. 1 file(s) left out.",
                    ),
                ),
                progress=1.0,
                memory=498 * MiB,  # no program: an import is all Python
            )
        )
        # Queued as the import commits, so each waited for the ones before it.
        at = started + ran + timedelta(seconds=1)
        for index, media in enumerate(pictures):
            took = timedelta(milliseconds=rng.randint(380, 900))
            jobs.append(
                thumbnail(
                    f"album-import/{index}", media, at, ran=took, sent=started + ran, parent="album-import"
                )
            )
            at += took + timedelta(milliseconds=40)

    # A resident's upload from the album page, sent from the request: top-level jobs.
    hyttetur = list(
        Media.objects.filter(album__folder="2026", album__name="Hyttetur")
        .select_related("album")
        .order_by("pk")
    )
    at = now - timedelta(minutes=47)
    for index, media in enumerate(hyttetur[:-1]):
        jobs.append(thumbnail(f"upload/{index}", media, at))
        at += timedelta(seconds=2)
    # Running now, started a second ago: no memory figure yet, as the worker writes its first
    # reading ten seconds in.
    if hyttetur:
        jobs.append(
            thumbnail(
                "upload/running",
                hyttetur[-1],
                now - timedelta(seconds=1),
                ran=None,
                state="STARTED",
                memory=None,
            )
        )

    started = now - timedelta(hours=3, minutes=30)
    downloader = residents[min(1, len(residents) - 1)]
    jobs.append(
        _Job(
            "album-download",
            "photo_album.tasks.build_album_download",
            "media",
            started,
            timedelta(minutes=3, seconds=41),
            arguments="(12,)",
            result=True,
            logger="photo_album.tasks",
            lines=(
                _Line(
                    timedelta(milliseconds=25),
                    "INFO",
                    f"Building Sommerfest.zip (original) with 17 item(s) for {downloader}.",
                ),
            ),
            progress=1.0,
            memory=92 * MiB,
        )
    )
    return jobs


def _other_jobs(now: datetime) -> list[_Job]:
    """A job whose worker died under it, and one that failed with an error."""
    # Sent by Beat a quarter of an hour or more ago, with a 15-minute limit: past it, so its worker
    # died (a redeploy). Listed in red until close_lost_jobs closes it, ten minutes past the limit
    # at the earliest.
    died = _quarterly(now - timedelta(minutes=31), 13)
    stale = _Job(
        "recover-stalled-lost",
        "gahkflix.tasks.recover_stalled",
        "celery",
        died + timedelta(seconds=1),
        state="STARTED",
        memory=139 * MiB,
    )
    failed = _Job(
        "admin-access-mail",
        "residents.tasks.notify_admin_access_request",
        "celery",
        now - timedelta(hours=4, minutes=5),
        timedelta(milliseconds=62),
        state="FAILURE",
        arguments="(3,)",
        result=celery_app.backend.prepare_exception(ConnectionRefusedError(111, "Connection refused")),
        traceback=SMTP_TRACEBACK,
        memory=127 * MiB,
    )
    return [stale, failed]


# ---- storing them ----


def _ensure_result_table() -> None:
    """Celery's result table, as Celery makes it, where it is missing. Celery's backend makes it
    itself, on its own connection, the first time it is used (building the failures' results did),
    so in a dev database it is there by now; this is for a database Celery never writes to: the
    tests'."""
    if "celery_taskmeta" in connection.introspection.table_names():
        return
    from celery.backends.database.models import TaskExtended
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateSequence, CreateTable, ExecutableDDLElement

    table = TaskExtended.__table__
    dialect = postgresql.dialect()
    statements: list[ExecutableDDLElement] = [
        CreateSequence(table.c.id.default),
        CreateTable(table),
        *map(CreateIndex, table.indexes),
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(str(statement.compile(dialect=dialect)))


def _encoded(value: Any) -> bytes | None:  # noqa: ANN401
    """A result as Celery's database backend stores it: serialized, then pickled by its column."""
    from celery.backends.database.models import TaskExtended
    from sqlalchemy.dialects import postgresql

    store = TaskExtended.__table__.c.result.type.bind_processor(postgresql.dialect())
    return store(celery_app.backend.encode(value))


def _store(job: _Job, now: datetime) -> None:
    worker = MEDIA_WORKER if job.queue == "media" else DEFAULT_WORKER
    finished = job.finished
    measured = finished is None or now - finished < MEASURED_FOR
    if job.state == "STARTED":
        result = _encoded({"pid": 7, "hostname": worker})  # what Celery stores as a job starts
    else:
        result = None if job.traceback == LOST_JOB_TRACEBACK else _encoded(job.result)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO celery_taskmeta"
            " (id, task_id, status, result, date_done, traceback, name, worker, retries, queue)"
            " VALUES (nextval('task_id_sequence'), %s, %s, %s, %s, %s, %s, %s, 0, %s)",
            [
                job.task_id,
                job.state,
                result,
                # Naive UTC, as Celery writes it; empty while it runs, as Celery leaves it.
                None if finished is None else finished.astimezone(UTC).replace(tzinfo=None),
                job.traceback or None,
                job.task,
                worker,
                job.queue,
            ],
        )
    reports = job.progress is not None
    WorkerJobLog.objects.create(
        task_id=job.task_id,
        task_name=job.task,
        state=job.state,
        sent_at=job.sent or job.started - timedelta(milliseconds=350),
        started_at=job.started,
        finished_at=finished,
        parent_id=task_id(job.parent) if job.parent else "",
        arguments=job.arguments,
        keyword_arguments="{}",
        lines=[
            {
                "at": timezone.localtime(job.started + line.after).isoformat(),
                "level": line.level,
                "logger": line.logger or job.logger,
                "message": line.message,
            }
            for line in job.lines
        ],
        # As core.worker_logs ends a job: one that succeeded got all the way.
        progress=(1.0 if job.state == "SUCCESS" else job.progress) if reports else None,
        progress_step=job.step if reports and job.state != "SUCCESS" else "",
        progressed_at=(job.progressed or finished) if reports else None,
        peak_memory=job.memory if measured else None,
        peak_program=job.program[0] if job.program and measured else "",
        peak_program_memory=job.program[1] if job.program and measured else None,
    )
