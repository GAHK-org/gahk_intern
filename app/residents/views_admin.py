"""Public-site admin (F-002) — administrator-gated. The legacy privilege-escalation and open
mass-mailer are structurally gone; this provides the legitimate admin screens, chiefly assigning the
monthly embedsgruppe roles."""

import ast
import datetime
import io
import json
import pickle  # nosec B403: _ResultUnpickler rejects every global/class lookup.
import zoneinfo
from typing import Never

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_celery_beat.models import PeriodicTask
from kombu.exceptions import DecodeError

from config.celery import app as celery_app
from core.models import WorkerJobLog

from . import admin_access
from .models import AdminAccessGrant, Resident, Role, RoleAssignment, active_period
from .permissions import PREVIEW_SESSION_KEY, current_resident, require_can_preview, role_required

JOB_PAGE_SIZE = 25
JOB_STATUSES = ("PENDING", "STARTED", "SUCCESS", "FAILURE", "RETRY", "REVOKED")
# Sort key -> ORDER BY clause. These strings are interpolated into SQL, so they must stay literals
# here and the request value must only ever select a key (the view validates it against this dict).
JOB_SORTS = {
    "started_desc": "sent_at DESC NULLS LAST, date_done DESC NULLS LAST, task_id",
    "started_asc": "sent_at ASC NULLS LAST, date_done DESC NULLS LAST, task_id",
    "finished_desc": "date_done DESC NULLS LAST, task_id",
    "finished_asc": "date_done ASC NULLS LAST, task_id",
    "status_asc": "status ASC, date_done DESC NULLS LAST, task_id",
    "status_desc": "status DESC, date_done DESC NULLS LAST, task_id",
}


@role_required("administrator")
def home(request: HttpRequest) -> HttpResponse:
    # The pending list rides on the front page as well as its own screen: the mail in
    # residents.tasks is best-effort, and this is the one page every administrator opens.
    return render(request, "siteadmin/home.html", {"pending_grants": admin_access.pending()})


def _queued_jobs() -> list[dict[str, object]]:
    """Return task metadata from Celery's SQLAlchemy PostgreSQL transport."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, q.name, m.visible, m.timestamp, m.payload
                FROM kombu_message AS m
                JOIN kombu_queue AS q ON q.id = m.queue_id
                WHERE m.visible = TRUE
                ORDER BY m.id DESC
                LIMIT 100
                """
            )
            rows = cursor.fetchall()
    except DatabaseError:
        return []

    jobs = []
    for message_id, queue, visible, timestamp, payload in rows:
        try:
            headers = json.loads(payload).get("headers", {})
        except (TypeError, json.JSONDecodeError):
            headers = {}
        jobs.append(
            {
                "id": headers.get("id"),
                "message_id": message_id,
                "task": headers.get("task", "Ukendt opgave"),
                "queue": queue,
                "visible": visible,
                "timestamp": timestamp,
                "eta": headers.get("eta"),
            }
        )
    return jobs


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


def _format_runtime(runtime: datetime.timedelta) -> str:
    total_milliseconds = max(0, runtime // datetime.timedelta(milliseconds=1))
    minutes, remainder = divmod(total_milliseconds, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes} min. {seconds} sek. {milliseconds} ms"


def _task_runtime(finished_at: datetime.datetime, submitted_at: datetime.datetime) -> str:
    """Calculate runtime across Celery's UTC result and the worker-local SQL broker timestamp.

    kombu writes `kombu_message.timestamp` with `datetime.now` in the worker process, which is
    Django-configured, and Django's settings export TZ=TIME_ZONE into that process. So the naive
    value is in settings.TIME_ZONE regardless of the container's own TZ (checked: TZ=UTC outside,
    `datetime.now()` is Copenhagen once settings load). Name the zone rather than use the request's
    active one, which is the wrong reference if anything ever calls `timezone.activate`.
    """
    if timezone.is_naive(finished_at):
        finished_at = finished_at.replace(tzinfo=datetime.UTC)
    if timezone.is_naive(submitted_at):
        submitted_at = submitted_at.replace(tzinfo=zoneinfo.ZoneInfo(settings.TIME_ZONE))
    return _format_runtime(finished_at - submitted_at)


def _task_argument_id(job: dict[str, object]) -> int | None:
    """Return the first positional task argument when it is a plain integer ID."""
    try:
        arguments = ast.literal_eval(str(job.get("arguments", "")))
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    if isinstance(arguments, (tuple, list)) and arguments:
        first_argument = arguments[0]
        if isinstance(first_argument, int) and not isinstance(first_argument, bool):
            return first_argument
    return None


def _add_photo_album_context(jobs: list[dict[str, object]]) -> None:
    """Attach concise resource names for photo-album tasks with two batched database queries."""
    media_task = "photo_album.tasks.build_media_derivatives"
    import_task = "photo_album.tasks.process_album_import"
    argument_ids = [_task_argument_id(job) for job in jobs]
    media_ids: set[int] = {
        argument_id
        for job, argument_id in zip(jobs, argument_ids, strict=True)
        if job["task"] == media_task and argument_id is not None
    }
    import_ids: set[int] = {
        argument_id
        for job, argument_id in zip(jobs, argument_ids, strict=True)
        if job["task"] == import_task and argument_id is not None
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
        if argument_id is not None and job["task"] == media_task and (media := media_by_id.get(argument_id)):
            job["task_context"] = f"Medie: {media.title} (album: {media.album})"
        elif (
            argument_id is not None
            and job["task"] == import_task
            and (album_import := imports_by_id.get(argument_id))
        ):
            imported_albums = [
                str(albums_by_id[album_id]) for album_id in album_import.album_ids if album_id in albums_by_id
            ]
            context = f"ZIP: {album_import.archive_name} (mappe: {album_import.folder})"
            if imported_albums:
                context += f"; albummer: {', '.join(imported_albums)}"
            job["task_context"] = context


def _child_jobs(parent_task_id: str) -> list[dict[str, object]]:
    """Return completed child jobs whose broker metadata names the given parent task.

    `position()` on the raw payload text discards almost every broker row before the payload is
    parsed as JSON; without it the cast would run for the whole table on every detail view. A retried
    task is republished under the same id and kombu keeps every delivery, so DISTINCT ON keeps the
    newest message per task.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task_id, status, date_done, name, sent_at, payload
                FROM (
                    SELECT DISTINCT ON (result.task_id)
                        result.task_id, result.status, result.date_done, result.name,
                        m.timestamp AS sent_at, m.payload
                    FROM kombu_message AS m
                    JOIN celery_taskmeta AS result
                      ON result.task_id = convert_from(m.payload::bytea, 'UTF8')::jsonb->'headers'->>'id'
                    WHERE position(%s in m.payload) > 0
                      AND convert_from(m.payload::bytea, 'UTF8')::jsonb->'headers'->>'parent_id' = %s
                    ORDER BY result.task_id, m.id DESC
                ) AS latest
                ORDER BY date_done DESC NULLS LAST, task_id
                """,
                [parent_task_id, parent_task_id],
            )
            rows = cursor.fetchall()
    except DatabaseError:
        return []

    children = []
    for task_id, status, finished_at, name, submitted_at, payload in rows:
        try:
            headers = json.loads(payload).get("headers", {}) if payload is not None else {}
        except (TypeError, json.JSONDecodeError):
            headers = {}
        runtime = (
            _task_runtime(finished_at, submitted_at)
            if isinstance(finished_at, datetime.datetime) and isinstance(submitted_at, datetime.datetime)
            else None
        )
        children.append(
            {
                "id": task_id,
                "status": status,
                "task": name or headers.get("task", "Ukendt opgave"),
                "arguments": headers.get("argsrepr", ""),
                "finished_at": finished_at,
                "runtime": runtime,
            }
        )
    _add_photo_album_context(children)
    return children


def _job_details(task_id: str) -> dict[str, object] | None:
    """Return the retained broker and result-backend metadata for one Celery task."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, date_done, result, traceback, name, worker, retries, queue
                FROM celery_taskmeta
                WHERE task_id = %s
                """,
                [task_id],
            )
            result_row = cursor.fetchone()
            cursor.execute(
                """
                SELECT q.name, m.visible, m.timestamp, m.payload
                FROM kombu_message AS m
                JOIN kombu_queue AS q ON q.id = m.queue_id
                WHERE convert_from(m.payload::bytea, 'UTF8')::jsonb->'headers'->>'id' = %s
                ORDER BY m.id DESC
                LIMIT 1
                """,
                [task_id],
            )
            message_row = cursor.fetchone()
    except DatabaseError:
        return None

    if result_row is None and message_row is None:
        return None

    headers: dict[str, object] = {}
    if message_row is not None:
        try:
            headers = json.loads(message_row[3]).get("headers", {})
        except (TypeError, json.JSONDecodeError):
            pass

    job: dict[str, object] = {
        "id": task_id,
        "task": headers.get("task", "Ukendt opgave"),
        "queue": message_row[0] if message_row else None,
        "visible": message_row[1] if message_row else False,
        "submitted_at": message_row[2] if message_row else None,
        "arguments": headers.get("argsrepr", ""),
        "keyword_arguments": headers.get("kwargsrepr", ""),
        "eta": headers.get("eta"),
        "expires": headers.get("expires"),
        "root_id": headers.get("root_id"),
        "parent_id": headers.get("parent_id"),
        "origin": headers.get("origin"),
    }
    if result_row is not None:
        status, finished_at, result, traceback, name, worker, retries, queue = result_row
        job.update(
            {
                "status": status,
                "finished_at": finished_at,
                "result": _display_task_result(result) if result is not None else None,
                "traceback": traceback,
                "task": name or job["task"],
                "worker": worker,
                "retries": retries,
                "queue": queue or job["queue"],
            }
        )
        submitted_at = job["submitted_at"]
        if isinstance(submitted_at, datetime.datetime) and isinstance(finished_at, datetime.datetime):
            job["runtime"] = _task_runtime(finished_at, submitted_at)
    _add_photo_album_context([job])
    job["children"] = _child_jobs(task_id)
    return job


# One pass over kombu_message, newest delivery per task id (a retry republishes under the same id and
# kombu keeps every delivery). Headers are parsed once per row here and only the handful of fields the
# page shows are carried on, so neither the join nor the sort drags the payload around.
#
# `parent_id` comes from this row alone: celery_taskmeta has no parent column and the SQLAlchemy result
# backend does not store `children`, so there is nothing else to consult. A result whose broker row has
# been swept therefore reads as top-level.
_LATEST_MESSAGES_CTE = """
    WITH latest_messages AS MATERIALIZED (
        SELECT DISTINCT ON (parsed.headers->>'id')
            parsed.headers->>'id' AS task_id,
            q.name AS queue,
            m.timestamp AS sent_at,
            parsed.headers->>'task' AS task,
            parsed.headers->>'argsrepr' AS argsrepr,
            parsed.headers->>'kwargsrepr' AS kwargsrepr,
            parsed.headers->>'parent_id' AS parent_id
        FROM kombu_message AS m
        JOIN kombu_queue AS q ON q.id = m.queue_id
        CROSS JOIN LATERAL (
            SELECT convert_from(m.payload::bytea, 'UTF8')::jsonb->'headers' AS headers
        ) AS parsed
        ORDER BY parsed.headers->>'id', m.id DESC
    )
"""
_JOB_FILTER_SQL = """
    FROM celery_taskmeta AS result
    LEFT JOIN latest_messages AS message ON message.task_id = result.task_id
    WHERE (%s = '' OR result.status = %s)
      AND (%s::boolean OR message.parent_id IS NULL)
"""


def _past_jobs(
    status: str = "",
    sort: str = "finished_desc",
    include_children: bool = False,
    limit: int = JOB_PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, object]]:
    """Return one page of completed task metadata, filtered and sorted by the database.

    Filtering, ordering and paging all happen in SQL so the sort options reach the whole result table
    and a fan-out of sub-jobs cannot crowd the parent jobs out of a fixed window.
    """
    order_by = JOB_SORTS[sort]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                {_LATEST_MESSAGES_CTE}
                SELECT
                    result.task_id,
                    result.status,
                    result.date_done,
                    result.name,
                    result.worker,
                    result.retries,
                    result.queue,
                    message.queue AS message_queue,
                    message.sent_at,
                    message.task,
                    message.argsrepr,
                    message.kwargsrepr,
                    message.parent_id
                {_JOB_FILTER_SQL}
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
                """,
                [status, status, include_children, limit, offset],
            )
            rows = cursor.fetchall()
    except DatabaseError:
        return []

    jobs = []
    for (
        task_id,
        job_status,
        date_done,
        name,
        worker,
        retries,
        queue,
        message_queue,
        sent_at,
        message_task,
        arguments,
        keyword_arguments,
        parent_id,
    ) in rows:
        jobs.append(
            {
                "id": task_id,
                "status": job_status,
                "finished_at": date_done,
                "task": name or message_task or "Ukendt opgave",
                "worker": worker,
                "retries": retries,
                "queue": queue or message_queue,
                "submitted_at": sent_at,
                "runtime": _task_runtime(date_done, sent_at) if date_done and sent_at else None,
                "parent_id": parent_id,
                "arguments": arguments or "",
                "keyword_arguments": keyword_arguments or "",
            }
        )
    _add_photo_album_context(jobs)
    return jobs


def _past_job_count(status: str = "", include_children: bool = False) -> int:
    """Return how many completed jobs match the same filters `_past_jobs` applies."""
    try:
        with connection.cursor() as cursor:
            if include_children:
                cursor.execute(
                    "SELECT COUNT(*) FROM celery_taskmeta WHERE (%s = '' OR status = %s)",
                    [status, status],
                )
            else:
                cursor.execute(
                    f"{_LATEST_MESSAGES_CTE} SELECT COUNT(*) {_JOB_FILTER_SQL}",
                    [status, status, include_children],
                )
            row = cursor.fetchone()
    except DatabaseError:
        return 0
    return int(row[0]) if row else 0


@role_required("administrator")
def worker_jobs(request: HttpRequest) -> HttpResponse:
    status = request.GET.get("status", "")
    if status not in JOB_STATUSES:
        status = ""
    sort = request.GET.get("sort", "finished_desc")
    if sort not in JOB_SORTS:
        sort = "finished_desc"
    include_children = request.GET.get("children") == "1"
    schedules = PeriodicTask.objects.filter(enabled=True).select_related(
        "interval", "crontab", "solar", "clocked"
    )
    page = Paginator(range(_past_job_count(status, include_children)), JOB_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    jobs = _past_jobs(
        status, sort, include_children, limit=JOB_PAGE_SIZE, offset=(page.number - 1) * JOB_PAGE_SIZE
    )
    return render(
        request,
        "siteadmin/worker_jobs.html",
        {
            "queued_jobs": _queued_jobs(),
            "past_jobs": page,
            "jobs": jobs,
            "schedules": schedules,
            "job_statuses": JOB_STATUSES,
            "selected_status": status,
            "selected_sort": sort,
            "include_children": include_children,
        },
    )


@role_required("administrator")
def worker_job_detail(request: HttpRequest, task_id: str) -> HttpResponse:
    job = _job_details(task_id)
    job_log = WorkerJobLog.objects.filter(task_id=task_id).first()
    if job is None:
        if job_log is None:
            raise Http404("worker job not found")
        # The result can be gone while its log is not — a result backend that expires results itself
        # does not know about the log — and the log is still worth showing on its own.
        job = {
            "id": task_id,
            "task": job_log.task_name,
            "status": job_log.state,
            "finished_at": job_log.finished_at,
        }
    return render(request, "siteadmin/worker_job_detail.html", {"job": job, "job_log": job_log})


@role_required("administrator")
def roles(request: HttpRequest) -> HttpResponse:
    year, month = active_period()
    if request.method == "POST":
        rid = request.POST.get("resident")
        role = request.POST.get("role")
        action = request.POST.get("action")
        if rid and role in Role.values:
            if action == "add":
                RoleAssignment.objects.get_or_create(resident_id=rid, role=role, year=year, month=month)
                Resident.objects.filter(id=rid).update(is_staff=True)
                # Stamp the granter so they cannot also be the approver (residents.admin_access).
                # The grant itself is opened by the signal, which covers the other three doors.
                if role == Role.ADMINISTRATOR:
                    AdminAccessGrant.objects.filter(
                        resident_id=rid, status=AdminAccessGrant.Status.PENDING, requested_by=None
                    ).update(requested_by=current_resident(request))
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


# ---- DEV-ONLY simulated clock: fast-forward the month locally to test round rollover (F-004).
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
