import datetime
import functools
import logging
import zoneinfo
from collections.abc import Mapping
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import connection
from django.db.models import F, Subquery, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from config.celery import app as celery_app
from residents.admin_access import approvers

from . import admin_summary

# How long a delivered broker message is kept before being swept.
#
# WHY THERE IS A SWEEP AT ALL. The broker is kombu's SQLAlchemy transport on our own Postgres, and
# it never deletes anything on the read path: taking a message only sets `visible = false`, and the
# row stays. Nothing else prunes the table either, so it is pure accumulation — one row per message
# ever sent, each carrying its JSON payload. `process_pending_media` contributes one recovery
# message each night.
#
# The result backend has its own sweep below, purge_worker_job_results.
#
# A day is long enough to be no help to a worker (a message is acknowledged within seconds of being
# taken) and short enough to keep the table small; it exists only so an operator looking into a
# failure this morning still has the rows in front of them.
BROKER_MESSAGE_RETENTION = datetime.timedelta(days=1)

# How long a SUCCESSFUL job is kept on the worker-jobs page: its result (celery_taskmeta) and its
# log (core/worker_logs.py) alike. Longer than any month, so the monthly summary on the 1st
# (core.admin_summary) still counts the whole of a 31-day month, even sent a few days late.
WORKER_JOB_RETENTION = datetime.timedelta(days=35)

# Any other job — failed, revoked, or stuck as started by a worker that died — is one somebody may
# need to look into, so it is kept far longer, and only a pile of the same goes: per task and status,
# the newest UNSUCCESSFUL_JOBS_KEPT are always kept, and of those beyond them, the ones older than
# UNSUCCESSFUL_JOB_RETENTION are purged. 1500 revoked notify_admin_access_request jobs lose their
# oldest 500 once those are 60 days old; 300 failed ones of the same task are another pile, and stay.
UNSUCCESSFUL_JOB_RETENTION = datetime.timedelta(days=60)
UNSUCCESSFUL_JOBS_KEPT = 1000

logger = logging.getLogger(__name__)

# Celery's status for a job that succeeded, as stored in celery_taskmeta.status and copied into
# WorkerJobLog.state.
_SUCCESS = "SUCCESS"
_STARTED = "STARTED"
_FAILURE = "FAILURE"

# How far past its hard time limit a job Celery still has as running must be before it is taken
# for lost. At the limit the worker kills it and records the failure itself; this is its moment to.
LOST_JOB_GRACE = datetime.timedelta(minutes=10)
LOST_JOB_TRACEBACK = (
    "WorkerLostError: the job never finished. Its worker was killed outright (out of memory, a "
    "redeploy) and nothing recorded the end, so core.tasks.close_lost_jobs closed it."
)


@functools.cache
def _task_registry() -> Mapping[str, Any]:
    """Celery's registry with every app's tasks module imported, as a worker has it. A web process
    otherwise imports a tasks module only once something there sends one of its tasks, and a task
    it has not met would be measured against the global time limit instead of its own."""
    celery_app.autodiscover_tasks(force=True)
    return celery_app.tasks


def task_time_limit(task_name: str) -> datetime.timedelta:
    """How long a job of this task may run before the worker kills it: its own hard limit, or the
    global one."""
    task = _task_registry().get(task_name)
    return datetime.timedelta(seconds=getattr(task, "time_limit", None) or settings.CELERY_TASK_TIME_LIMIT)


@shared_task
def send_admin_monthly_summary() -> int:
    """Mail everyone with Django-admin access the summary of the month that just ended
    (core.admin_summary). Returns how many mails were sent: one, or none without recipients."""
    recipients = sorted(approvers().values_list("email", flat=True))
    if not recipients:
        logger.warning("Nobody has Django-admin access to send the monthly summary to.")
        return 0
    subject, body = admin_summary.compose()
    return send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients)


@shared_task
def purge_delivered_broker_messages() -> int:
    """Delete acknowledged broker messages, which the SQLAlchemy transport never removes itself.

    Raw SQL because `kombu_message` is kombu's table, not a Django model — there is no manager to
    ask, and inventing one would claim ownership of a schema another library migrates.

    A NAIVE cutoff on purpose, in settings.TIME_ZONE: the column is `timestamp without time zone`,
    which kombu fills with the worker's `datetime.now()` when the worker takes the message — local
    time, because Django's settings export TZ=TIME_ZONE into the process. (Celery's own `date_done`
    is the one holding UTC.) An aware cutoff would be compared in the session timezone instead, and
    a UTC one keeps every row an hour or two past its day.

    Guarded on the table existing rather than on the broker URL, so pointing CELERY_BROKER_URL at
    Redis or RabbitMQ makes this a no-op instead of an error — the table is simply not there.
    """
    if "kombu_message" not in connection.introspection.table_names():
        return 0
    cutoff = timezone.localtime(timezone=zoneinfo.ZoneInfo(settings.TIME_ZONE)).replace(tzinfo=None)
    cutoff -= BROKER_MESSAGE_RETENTION
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM kombu_message WHERE visible = false AND timestamp < %s",
            [cutoff],
        )
        return cursor.rowcount


@shared_task
def purge_worker_job_logs() -> int:
    """Delete the stored logs of successful jobs older than WORKER_JOB_RETENTION, and of other jobs
    as far as UNSUCCESSFUL_JOB_RETENTION and UNSUCCESSFUL_JOBS_KEPT allow.

    The excess is counted first and the age tested after, in the outer query: tested inside, it
    would decide which rows get counted, and an old pile could never reach the limit.
    """
    from .models import WorkerJobLog

    now = timezone.now()
    deleted, _ = WorkerJobLog.objects.filter(
        state=_SUCCESS, started_at__lt=now - WORKER_JOB_RETENTION
    ).delete()
    newer = Window(
        RowNumber(),
        partition_by=[F("task_name"), F("state")],
        order_by=[F("started_at").desc(), F("id").desc()],
    )
    excess = (
        WorkerJobLog.objects.exclude(state=_SUCCESS)
        .annotate(newer=newer)
        .filter(newer__gt=UNSUCCESSFUL_JOBS_KEPT)
        .values("pk")
    )
    unsuccessful, _ = WorkerJobLog.objects.filter(
        pk__in=Subquery(excess), started_at__lt=now - UNSUCCESSFUL_JOB_RETENTION
    ).delete()
    return deleted + unsuccessful


@shared_task
def close_lost_jobs() -> int:
    """Mark as failed the jobs Celery still has as running that cannot be running any more.

    A worker killed outright records nothing, so its job's result row keeps status STARTED for good:
    listed as running on the worker-jobs page, and never purged — purge_worker_job_results goes by
    `date_done`, which Celery leaves NULL on a STARTED row. A job is lost once it has run past its
    hard time limit (plus LOST_JOB_GRACE), since by then its worker would have killed it and said
    so. Its start is the worker's own record (core.worker_logs writes one as each job starts); a
    STARTED row without one is from before those existed, and long dead.

    `date_done` becomes the latest moment the job can have ended — start plus time limit — or now
    when the start is unknown, so the row ages out with the other failures. Should a job be closed
    wrongly after all, nothing is lost: its real end overwrites the row.
    """
    from .models import WorkerJobLog

    if "celery_taskmeta" not in connection.introspection.table_names():
        return 0
    with connection.cursor() as cursor:
        cursor.execute("SELECT task_id, name FROM celery_taskmeta WHERE status = %s", [_STARTED])
        running = dict(cursor.fetchall())
    if not running:
        return 0
    started = dict(WorkerJobLog.objects.filter(task_id__in=running).values_list("task_id", "started_at"))
    now = timezone.now()
    closed = 0
    for task_id, name in running.items():
        limit = task_time_limit(name or "")
        started_at = started.get(task_id)
        if started_at is not None and now - started_at <= limit + LOST_JOB_GRACE:
            continue
        ended_at = started_at + limit if started_at is not None else now
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE celery_taskmeta SET status = %s, date_done = %s, traceback = %s, result = NULL"
                " WHERE task_id = %s AND status = %s",
                [
                    _FAILURE,
                    ended_at.astimezone(datetime.UTC).replace(tzinfo=None),
                    LOST_JOB_TRACEBACK,
                    task_id,
                    _STARTED,
                ],
            )
            closed += cursor.rowcount
        WorkerJobLog.objects.filter(task_id=task_id, finished_at__isnull=True).update(
            state=_FAILURE, finished_at=ended_at
        )
    if closed:
        logger.warning("Closed %d job(s) whose worker was lost while they ran.", closed)
    return closed


@shared_task
def purge_worker_job_results() -> int:
    """Delete successful jobs older than WORKER_JOB_RETENTION from the result backend, and other
    jobs as far as UNSUCCESSFUL_JOB_RETENTION and UNSUCCESSFUL_JOBS_KEPT allow (counted before the
    age is tested, as in purge_worker_job_logs). A job is counted under its task `name`, which
    CELERY_RESULT_EXTENDED stores.

    In place of Celery's own `celery.backend_cleanup`, which deletes every result past
    `result_expires` whatever its status — failures included, which are the ones worth keeping.
    settings.CELERY_RESULT_EXPIRES is None so Beat no longer installs it, and core migration 0012
    removes the entry an earlier Beat already stored.

    Raw SQL, a naive UTC cutoff and the guard on the table, all for the reasons given on
    purge_delivered_broker_messages: the table is Celery's, `date_done` is `timestamp without time
    zone` holding UTC, and a result backend other than the database has no such table. (Such a
    backend now keeps results forever unless it expires them itself; Redis does not.)
    """
    if "celery_taskmeta" not in connection.introspection.table_names():
        return 0
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM celery_taskmeta WHERE status = %s AND date_done < %s",
            [_SUCCESS, now - WORKER_JOB_RETENTION],
        )
        deleted = cursor.rowcount
        cursor.execute(
            """
            DELETE FROM celery_taskmeta WHERE id IN (
                SELECT id FROM (
                    SELECT id, date_done, row_number() OVER (
                        PARTITION BY name, status ORDER BY date_done DESC NULLS FIRST, id DESC
                    ) AS newer
                    FROM celery_taskmeta WHERE status <> %s
                ) AS ranked
                WHERE newer > %s AND date_done < %s
            )
            """,
            [_SUCCESS, UNSUCCESSFUL_JOBS_KEPT, now - UNSUCCESSFUL_JOB_RETENTION],
        )
        return deleted + cursor.rowcount
