import datetime

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import connection
from django.utils import timezone

from residents.models import Resident, active_period

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
# log (core/worker_logs.py) alike. Anything else — failed, revoked, or stuck as started by a worker
# that died — stays until somebody deletes it, since those are the jobs somebody needs to look into.
WORKER_JOB_RETENTION = datetime.timedelta(days=30)

# The one status that is purged — Celery's, as stored in celery_taskmeta.status and copied into
# WorkerJobLog.state.
_SUCCESS = "SUCCESS"


@shared_task
def send_admin_dummy_notification() -> int:
    """Send the temporary twice-daily worker notification to current administrators."""
    year, month = active_period()
    recipients = set(
        Resident.objects.filter(
            is_active=True,
            role_assignments__role="administrator",
            role_assignments__year=year,
            role_assignments__month=month,
        ).values_list("email", flat=True)
    )
    recipients.update(
        Resident.objects.filter(is_active=True, is_superuser=True).values_list("email", flat=True)
    )
    if not recipients:
        return 0
    return send_mail(
        "Celery-testnotifikation",
        "Dette er den planlagte testnotifikation fra Celery-worker'en.",
        settings.DEFAULT_FROM_EMAIL,
        sorted(recipients),
    )


@shared_task
def purge_delivered_broker_messages() -> int:
    """Delete acknowledged broker messages, which the SQLAlchemy transport never removes itself.

    Raw SQL because `kombu_message` is kombu's table, not a Django model — there is no manager to
    ask, and inventing one would claim ownership of a schema another library migrates.

    A NAIVE cutoff on purpose: the column is `timestamp without time zone` holding UTC (verified
    against the running database), so handing Postgres an aware datetime would compare against the
    session timezone and sweep the wrong hour's rows.

    Guarded on the table existing rather than on the broker URL, so pointing CELERY_BROKER_URL at
    Redis or RabbitMQ makes this a no-op instead of an error — the table is simply not there.
    """
    if "kombu_message" not in connection.introspection.table_names():
        return 0
    cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - BROKER_MESSAGE_RETENTION
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM kombu_message WHERE visible = false AND timestamp < %s",
            [cutoff],
        )
        return cursor.rowcount


@shared_task
def purge_worker_job_logs() -> int:
    """Delete the stored logs of successful jobs older than WORKER_JOB_RETENTION."""
    from .models import WorkerJobLog

    cutoff = timezone.now() - WORKER_JOB_RETENTION
    deleted, _ = WorkerJobLog.objects.filter(state=_SUCCESS, started_at__lt=cutoff).delete()
    return deleted


@shared_task
def purge_worker_job_results() -> int:
    """Delete successful jobs older than WORKER_JOB_RETENTION from the result backend.

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
    cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - WORKER_JOB_RETENTION
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM celery_taskmeta WHERE status = %s AND date_done < %s",
            [_SUCCESS, cutoff],
        )
        return cursor.rowcount
