"""The administrators' monthly summary, mailed on the 1st by core.tasks.send_admin_monthly_summary.

Two halves: what the background jobs did in the month that just ended (read from Celery's result
table, which is why core.tasks.WORKER_JOB_RETENTION outlasts a month), and who holds access as the
mail goes out — superusers, administrators and whether Django admin is open to them, the other
roles, and the Django-admin grants decided during the month.

It replaced a test mail sent twice a day only to prove Beat and the worker were alive in
production. The summary arriving on the 1st proves the same, and has something in it worth reading.
"""

import datetime
from collections import Counter

from django.conf import settings
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format, number_format

from residents.models import AdminAccessGrant, Resident, Role, RoleAssignment, active_period

UNKNOWN_TASK = "(ukendt)"


def previous_month(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """The first day of the month before `today`'s, and the first day of `today`'s."""
    end = today.replace(day=1)
    return (end - datetime.timedelta(days=1)).replace(day=1), end


def compose(today: datetime.date | None = None) -> tuple[str, str]:
    """The subject and body of the summary for the month before `today`."""
    start, end = previous_month(today or timezone.localdate())
    month = date_format(start, "F Y")
    lines = [
        f"Her er månedsoversigten for {month}.",
        "",
        *_jobs(start, end),
        "",
        *_access(start, end, month),
        "",
        "Oversigten sendes den 1. i hver måned til alle med adgang til Django admin.",
    ]
    return f"Månedsoversigt for {month}", "\n".join(lines)


def _jobs(start: datetime.date, end: datetime.date) -> list[str]:
    lines = ["BAGGRUNDSJOB"]
    rows = _job_counts(start, end)
    if rows is None:
        return [*lines, "Ingen jobdata: Celerys resultattabel findes ikke."]
    statuses: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    for task, status, count in rows:
        statuses[status] += count
        if status == "FAILURE":
            failed[task or UNKNOWN_TASK] += count
    lines += [
        f"Kørt: {_number(statuses['SUCCESS'] + statuses['FAILURE'])}",
        f"  Gennemført: {_number(statuses['SUCCESS'])}",
        f"  Fejlet: {_number(statuses['FAILURE'])}",
    ]
    if statuses["REVOKED"]:
        lines.append(f"Annulleret: {_number(statuses['REVOKED'])}")
    if failed:
        lines += ["", "Fejlede job efter type:"]
        lines += [f"  {task}: {_number(count)}" for task, count in sorted(failed.items(), key=_most_first)]
        lines += ["", f"Se de fejlede job her: {_url('siteadmin:worker_jobs')}?status=FAILURE"]
    return lines


def _job_counts(start: datetime.date, end: datetime.date) -> list[tuple[str | None, str, int]] | None:
    """(task, status, count) of the jobs that ended between local midnight on `start` and on `end`;
    None when there is no result table (a result backend other than the database).

    Raw SQL against a naive UTC range for the reasons core.tasks.purge_worker_job_results gives: the
    table is Celery's, and `date_done` is `timestamp without time zone` holding UTC. A job still
    running has no `date_done`, so only ended jobs are counted, each in the month it ended.
    """
    if "celery_taskmeta" not in connection.introspection.table_names():
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, status, count(*) FROM celery_taskmeta"
            " WHERE date_done >= %s AND date_done < %s GROUP BY name, status",
            [_utc_midnight(start), _utc_midnight(end)],
        )
        return cursor.fetchall()


def _access(start: datetime.date, end: datetime.date, month: str) -> list[str]:
    year, period_month = active_period()
    period = date_format(datetime.date(year, period_month, 1), "F Y")
    holders = (
        RoleAssignment.objects.filter(year=year, month=period_month, resident__is_active=True)
        .select_related("resident")
        .order_by("resident__first_name", "resident__last_name", "resident__pk")
    )
    by_role: dict[str, list[Resident]] = {}
    for assignment in holders:
        by_role.setdefault(assignment.role, []).append(assignment.resident)
    grants = {
        grant.resident_id: grant.get_status_display().lower()
        for grant in AdminAccessGrant.objects.filter(resident__in=by_role.get(Role.ADMINISTRATOR, []))
    }
    superusers = Resident.objects.filter(is_active=True, is_superuser=True)

    lines = [f"ADGANG (roller for {period})"]
    lines += _people("Superbrugere", [str(resident) for resident in superusers])
    lines += [""]
    lines += _people(
        "Administratorer",
        [
            f"{resident} – Django admin: "
            + ("superbruger" if resident.is_superuser else grants.get(resident.pk, "ikke anmodet"))
            for resident in by_role.get(Role.ADMINISTRATOR, [])
        ],
    )
    others = [
        f"  {label}: " + ", ".join(resident.full_name for resident in by_role[role])
        for role, label in Role.choices
        if role != Role.ADMINISTRATOR and role in by_role
    ]
    if others:
        lines += ["", "Øvrige roller:", *others]

    decided = (
        AdminAccessGrant.objects.filter(decided_at__gte=_midnight(start), decided_at__lt=_midnight(end))
        .select_related("resident", "decided_by")
        .order_by("decided_at")
    )
    if decided:
        lines += ["", f"Adgang til Django admin afgjort i {month}:"]
        lines += [
            f"  {date_format(timezone.localtime(grant.decided_at), 'j. F')}: {grant.resident.full_name} "
            f"{grant.get_status_display().lower()} af {grant.decided_by.full_name if grant.decided_by else 'ukendt'}"
            for grant in decided
        ]
    lines += ["", f"Se og afgør adgang her: {_url('siteadmin:admin_access')}"]
    return lines


def _people(title: str, people: list[str]) -> list[str]:
    return [f"{title} ({len(people)}):", *(f"  {person}" for person in people or ["ingen"])]


def _most_first(item: tuple[str, int]) -> tuple[int, str]:
    return -item[1], item[0]


def _midnight(day: datetime.date) -> datetime.datetime:
    """Local midnight at the start of `day`, aware."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def _utc_midnight(day: datetime.date) -> datetime.datetime:
    """Local midnight at the start of `day` as Celery's `date_done` holds it: naive UTC."""
    return _midnight(day).astimezone(datetime.UTC).replace(tzinfo=None)


def _number(value: int) -> str:
    return number_format(value, force_grouping=True)


def _url(name: str) -> str:
    return settings.SITE_URL.rstrip("/") + reverse(name)
