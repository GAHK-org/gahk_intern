"""Public-site admin (F-002) — administrator-gated. The legacy privilege-escalation and open
mass-mailer are structurally gone; this provides the legitimate admin screens, chiefly assigning the
monthly embedsgruppe roles."""

import ast
import datetime
import io
import json
import pickle  # nosec B403: _ResultUnpickler rejects every global/class lookup.
from typing import Never

from django.conf import settings
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

from .models import Resident, Role, RoleAssignment, active_period
from .permissions import PREVIEW_SESSION_KEY, require_can_preview, role_required

JOB_PAGE_SIZE = 25
JOB_STATUSES = ("PENDING", "STARTED", "SUCCESS", "FAILURE", "RETRY", "REVOKED")
JOB_SORTS = {
    "started_desc": ("submitted_at", True),
    "started_asc": ("submitted_at", False),
    "finished_desc": ("finished_at", True),
    "finished_asc": ("finished_at", False),
    "status_asc": ("status", False),
    "status_desc": ("status", True),
}


@role_required("administrator")
def home(request: HttpRequest) -> HttpResponse:
    return render(request, "siteadmin/home.html")


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
    """Calculate runtime across Celery's UTC result and local SQL broker timestamps."""
    if timezone.is_naive(finished_at):
        finished_at = finished_at.replace(tzinfo=datetime.UTC)
    if timezone.is_naive(submitted_at):
        submitted_at = timezone.make_aware(submitted_at, timezone.get_current_timezone())
    return _format_runtime(finished_at - submitted_at)


def _task_argument_id(job: dict[str, object]) -> int | None:
    """Return the first positional task argument when it is a plain integer ID."""
    try:
        arguments = ast.literal_eval(str(job.get("arguments", "")))
    except (SyntaxError, ValueError):
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
    media_ids: set[int] = {
        argument_id
        for job in jobs
        if job["task"] == media_task and (argument_id := _task_argument_id(job)) is not None
    }
    import_ids: set[int] = {
        argument_id
        for job in jobs
        if job["task"] == import_task and (argument_id := _task_argument_id(job)) is not None
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

    for job in jobs:
        argument_id = _task_argument_id(job)
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
    """Return completed child jobs whose broker metadata names the given parent task."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT result.task_id, result.status, result.date_done, result.name, m.timestamp, m.payload
                FROM kombu_message AS m
                JOIN celery_taskmeta AS result
                  ON result.task_id = convert_from(m.payload::bytea, 'UTF8')::jsonb->'headers'->>'id'
                WHERE convert_from(m.payload::bytea, 'UTF8')::jsonb->'headers'->>'parent_id' = %s
                ORDER BY result.date_done DESC
                """,
                [parent_task_id],
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
        "logs_available": False,
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


def _past_jobs(
    status: str = "", sort: str = "finished_desc", include_children: bool = False
) -> list[dict[str, object]]:
    """Return completed task metadata paired with its acknowledged broker message."""
    parameters = [status, status, sort, sort]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH recent_results AS MATERIALIZED (
                    SELECT task_id, status, date_done, name, worker, retries, queue
                    FROM celery_taskmeta
                    WHERE (%s = '' OR status = %s)
                    ORDER BY
                        CASE WHEN %s = 'finished_asc' THEN date_done END ASC,
                        CASE WHEN %s <> 'finished_asc' THEN date_done END DESC
                    LIMIT 100
                )
                SELECT
                    result.task_id,
                    result.status,
                    result.date_done,
                    result.name,
                    result.worker,
                    result.retries,
                    result.queue,
                    message.queue,
                    message.timestamp,
                    message.payload
                FROM recent_results AS result
                LEFT JOIN LATERAL (
                    SELECT q.name AS queue, m.timestamp, m.payload
                    FROM kombu_message AS m
                    JOIN kombu_queue AS q ON q.id = m.queue_id
                    WHERE convert_from(m.payload::bytea, 'UTF8')::jsonb->'headers'->>'id' = result.task_id
                    ORDER BY m.id DESC
                    LIMIT 1
                ) AS message ON TRUE
                ORDER BY result.date_done DESC
                """,
                parameters,
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
        timestamp,
        payload,
    ) in rows:
        try:
            headers = json.loads(payload).get("headers", {}) if payload is not None else {}
        except (TypeError, json.JSONDecodeError):
            headers = {}
        runtime = _task_runtime(date_done, timestamp) if date_done and timestamp else None
        jobs.append(
            {
                "id": task_id,
                "status": job_status,
                "finished_at": date_done,
                "task": name or headers.get("task", "Ukendt opgave"),
                "worker": worker,
                "retries": retries,
                "queue": queue or message_queue,
                "submitted_at": timestamp,
                "runtime": runtime,
                "parent_id": headers.get("parent_id"),
                "arguments": headers.get("argsrepr", ""),
                "keyword_arguments": headers.get("kwargsrepr", ""),
            }
        )
    _add_photo_album_context(jobs)
    if not include_children:
        jobs = [job for job in jobs if not job["parent_id"]]
    sort_field, reverse = JOB_SORTS[sort]
    present_jobs = [job for job in jobs if job[sort_field] is not None]
    missing_jobs = [job for job in jobs if job[sort_field] is None]
    present_jobs.sort(key=lambda job: job[sort_field], reverse=reverse)
    return [*present_jobs, *missing_jobs]


def _past_job_count(status: str = "") -> int:
    """Return the full number of completed jobs for the selected status."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM celery_taskmeta WHERE (%s = '' OR status = %s)",
                [status, status],
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
    past_jobs = Paginator(_past_jobs(status, sort, include_children), JOB_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "siteadmin/worker_jobs.html",
        {
            "queued_jobs": _queued_jobs(),
            "past_jobs": past_jobs,
            "total_past_jobs": _past_job_count(status),
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
    if job is None:
        raise Http404("worker job not found")
    return render(request, "siteadmin/worker_job_detail.html", {"job": job})


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
