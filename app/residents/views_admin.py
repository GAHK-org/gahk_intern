"""Public-site admin (F-002) — administrator-gated. The legacy privilege-escalation and open
mass-mailer are structurally gone; this provides the legitimate admin screens, chiefly assigning the
monthly embedsgruppe roles."""

import datetime
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_celery_beat.models import PeriodicTask

from .models import Resident, Role, RoleAssignment, active_period
from .permissions import PREVIEW_SESSION_KEY, require_can_preview, role_required


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
                "id": message_id,
                "task": headers.get("task", "Ukendt opgave"),
                "queue": queue,
                "visible": visible,
                "timestamp": timestamp,
                "eta": headers.get("eta"),
            }
        )
    return jobs


def _past_jobs() -> list[dict[str, object]]:
    """Return completed Celery task metadata without exposing arguments or results."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task_id, status, date_done, name, worker, retries, queue
                FROM celery_taskmeta
                ORDER BY date_done DESC
                LIMIT 100
                """
            )
            rows = cursor.fetchall()
    except DatabaseError:
        return []

    return [
        {
            "id": task_id,
            "status": status,
            "finished_at": date_done,
            "task": name or "Ukendt opgave",
            "worker": worker,
            "retries": retries,
            "queue": queue,
        }
        for task_id, status, date_done, name, worker, retries, queue in rows
    ]


@role_required("administrator")
def worker_jobs(request: HttpRequest) -> HttpResponse:
    schedules = PeriodicTask.objects.filter(enabled=True).select_related(
        "interval", "crontab", "solar", "clocked"
    )
    return render(
        request,
        "siteadmin/worker_jobs.html",
        {"queued_jobs": _queued_jobs(), "past_jobs": _past_jobs(), "schedules": schedules},
    )


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
