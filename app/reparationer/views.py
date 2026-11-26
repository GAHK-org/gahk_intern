"""Reparationer — a kanban board for tracking repairs on the kollegium.

Open to every logged-in resident for reporting, same openness as Opslagstavlen: reporting something
broken needs no role. Two crews then work the pipeline in sequence — see models.RepairTask for the
`responsible`/`status` split — and each has a different ceiling:

  * Viceværterne ("reparationer.move") triage: they may move a ticket to any status
    EXCEPT the two in MANAGER_ONLY_STATUSES, and hand `responsible` over to Reppergruppen (see
    set_responsible).
  * Reppergruppen, Inspektionen and administrator ("reparationer.manage") have no ceiling: every status,
    including MANAGER_ONLY_STATUSES and closing a ticket, plus delete.

set_status is one view for both crews rather than two, because the columns a Vicevært may use are a
strict subset of a manager's — splitting it would duplicate the "does this status exist" check for
no real separation of concerns.
"""

from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_POST

from access.api import permission_required
from core.clock import current_datetime
from core.uploads import EXTENSION_HELP, prepare_image_upload
from residents.permissions import current_resident

from . import live, services
from .forms import COMMON_AREAS, RepairCommentForm, RepairTaskForm
from .models import RepairComment, RepairImage, RepairTask

# The board's "sted" filter — same two-way split as the location dropdown itself (COMMON_AREAS vs.
# everything else, which is only ever a room or a floor's toilet — see forms.location_choices).
LOCATION_TYPES = [("faelles", "Fællesarealer"), ("vaerelser", "Værelser")]

# Statuses a Vicevært may never move a ticket INTO — the page never renders their buttons (see
# board.html/detail.html), so reaching set_status with one of these as a non-manager only happens
# via a replayed/crafted request. Færdig is closing the ticket; AK projekt is committing a slice of
# Reppergruppen's AK hours to it — both are calls only that crew should make.
MANAGER_ONLY_STATUSES = (RepairTask.Status.AK_PROJEKT, RepairTask.Status.FAERDIG)

# Photos per report or note. Enough to show a fault from a few angles; the cap is what keeps one
# submission from being a whole camera roll.
MAX_IMAGES = 6


def _attached_images(request: HttpRequest) -> list[Any]:
    """The request's photos that passed core.uploads, ready to store — refused ones dropped with a
    warning, never failing the submission. Same reaction as core.uploads.attached_image (which takes
    exactly one file, hence this loop): losing a written report because one picture was wrong is
    the worse outcome."""
    uploads = request.FILES.getlist("images")
    if len(uploads) > MAX_IMAGES:
        messages.warning(request, f"Højst {MAX_IMAGES} billeder ad gangen — resten blev ikke gemt.")
        uploads = uploads[:MAX_IMAGES]
    prepared = []
    for upload in uploads:
        image, error = prepare_image_upload(upload, settings.REPAIR_IMAGE_MAX_MB)
        if error is not None:
            messages.warning(request, f"{upload.name}: {error} Billedet blev ikke gemt.")
            continue
        prepared.append(image)
    return prepared


def _save_images(task: RepairTask, images: list[Any], comment: RepairComment | None = None) -> None:
    for image in images:
        RepairImage.objects.create(task=task, comment=comment, file=image)


def can_delete_comment(request: HttpRequest, comment: RepairComment) -> bool:
    """The comment's author, or a manager — mirrors opslagstavle.access.can_delete_comment."""
    if not request.user.is_authenticated:
        return False
    return comment.author_id == request.user.pk or request.user.has_perm("access.reparationer_manage")


def _redirect_after(request: HttpRequest, task_pk: int) -> HttpResponseRedirect:
    """Board and detail both carry the move/handoff forms; each stamps a hidden `next` so a tap
    returns to whichever page it came from instead of always bouncing to the board."""
    if request.POST.get("next") == "detail":
        return redirect("reparationer:detail", pk=task_pk)
    return redirect("reparationer:board")


def _is_drag(request: HttpRequest) -> bool:
    """Whether this POST came from the board's drag-and-drop (frontend/src/reparationer.ts) rather
    than from a plain form. The drag has already moved the card in the DOM, so it wants a verdict it
    can act on — not a redirect whose body it would only throw away."""
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _move_response(request: HttpRequest, task: RepairTask, error: str = "") -> HttpResponse:
    """One answer for both callers: JSON for the drag, a redirect for a form POST.

    A refused drag still gets 200, with `ok: false` and a reason — the card has to be put back and
    the reason shown, which needs a body a fetch can read, and an error page is not one. Whether to
    refuse is the caller's decision; this only reports it.
    """
    if _is_drag(request):
        return JsonResponse(
            {
                "ok": not error,
                "error": error,
                "status": task.status,
                "status_label": task.get_status_display(),
                "responsible": task.responsible,
                "responsible_label": task.get_responsible_display(),
            }
        )
    if error:
        messages.error(request, error)
    return _redirect_after(request, task.pk)


def _search(tasks: "QuerySet[RepairTask]", query: str) -> "QuerySet[RepairTask]":
    """Title/location/description/reporter — everything a card actually shows, so a hit is never a
    surprise ("why did this match?"). A plain icontains rather than a search index: the whole table
    fits on a screen a few times over, so ranking would be over-engineering. Shared by the board and
    the archive, which search the same fields over different base querysets (active vs. archived)."""
    if not query:
        return tasks
    return tasks.filter(
        Q(title__icontains=query)
        | Q(location__icontains=query)
        | Q(description__icontains=query)
        | Q(reported_by__first_name__icontains=query)
        | Q(reported_by__last_name__icontains=query)
    )


@login_required
def board(request: HttpRequest) -> HttpResponse:
    query = (request.GET.get("q") or "").strip()
    location_type = request.GET.get("sted") or ""
    if location_type not in {value for value, _ in LOCATION_TYPES}:
        location_type = ""  # an unknown value shows everything rather than 404ing

    tasks = _search(
        RepairTask.objects.active()
        .select_related("reported_by")
        # distinct: two to-many joins in one query would otherwise multiply each other's counts.
        .annotate(comment_count=Count("comments", distinct=True), image_count=Count("images", distinct=True)),
        query,
    )
    if location_type == "faelles":
        tasks = tasks.filter(location__in=COMMON_AREAS)
    elif location_type == "vaerelser":
        tasks = tasks.exclude(location__in=[*COMMON_AREAS, ""])

    columns = [
        (status, label, [t for t in tasks if t.status == status])
        for status, label in RepairTask.Status.choices
    ]
    # A live signal re-reads the board through this same view, so its filters come along for free;
    # it gets the board alone (and no `messages`, which a background read must not consume).
    template = "reparationer/_kanban.html" if request.headers.get("HX-Request") else "reparationer/board.html"
    response = render(
        request,
        template,
        {
            "columns": columns,
            "query": query,
            "location_type": location_type,
            "location_types": LOCATION_TYPES,
            "result_count": sum(len(items) for _, _, items in columns) if query else None,
            # Dragging a card is the board's only move control (see board.html). Both flags are read
            # by frontend/src/reparationer.ts, which wires up nothing at all without can_move.
            "can_move": request.user.has_perm("access.reparationer_move"),
            "can_manage": request.user.has_perm("access.reparationer_manage"),
            "manager_only_statuses": MANAGER_ONLY_STATUSES,
        },
    )
    # One URL, two bodies. Without Vary the browser may keep the bare board under the page's URL
    # and serve it on Back — an unstyled kanban with no page around it. The known htmx trap.
    patch_vary_headers(response, ["HX-Request"])
    return response


@login_required
def archive_list(request: HttpRequest) -> HttpResponse:
    """Archived tickets: no columns (every one is Færdig by construction — see
    models.RepairTaskQuerySet.due_for_archive), just a searchable list, newest-archived first."""
    query = (request.GET.get("q") or "").strip()
    tasks = _search(
        RepairTask.objects.archived().select_related("reported_by").order_by("-archived_at"),
        query,
    )
    return render(
        request,
        "reparationer/archive.html",
        {
            "tasks": tasks,
            "query": query,
            "can_manage": request.user.has_perm("access.reparationer_manage"),
        },
    )


@login_required
def create(request: HttpRequest) -> HttpResponse:
    form = RepairTaskForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        images = _attached_images(request)
        task = form.save(commit=False)
        task.reported_by = current_resident(request)
        task.save()
        _save_images(task, images)
        services.notify_new_task(task)
        live.task_changed(task.pk, "created")
        messages.success(request, "Reparationen er meldt ind.")
        return redirect("reparationer:board")
    return render(
        request,
        "reparationer/form.html",
        {"form": form, "max_images": MAX_IMAGES, "image_help": EXTENSION_HELP},
    )


@require_POST
@permission_required("access.reparationer_move")
def set_status(request: HttpRequest, pk: int) -> HttpResponse:
    task = get_object_or_404(RepairTask, pk=pk)
    status = request.POST.get("status")
    if status in MANAGER_ONLY_STATUSES and not request.user.has_perm("access.reparationer_manage"):
        # A Vicevært passed the view-level "reparationer.move" check but this status is manager-only — see
        # MANAGER_ONLY_STATUSES. Answered rather than ignored: the board lets them pick a card up
        # and carry it anywhere, so landing on a column that is not theirs is an ordinary slip, and
        # it deserves the card back plus a reason. The buttons on the detail page still never render
        # for them, so a form POST reaching here is a replayed request and gets the same refusal.
        return _move_response(request, task, "Kun Reppergruppen kan flytte en sag hertil.")
    if status in RepairTask.Status.values and status != task.status:
        # Only a real move restarts the card's colour clock (models.RepairTask.column_age) — a
        # replayed POST for the column it is already in must not quietly make it look fresh.
        task.status = status
        task.status_changed_at = current_datetime()
        task.save(update_fields=["status", "status_changed_at", "updated_at"])
        live.task_changed(task.pk)
    return _move_response(request, task)


@require_POST
@permission_required("access.reparationer_move")
def set_responsible(request: HttpRequest, pk: int) -> HttpResponse:
    """Hand a ticket between the two crews, in either direction.

    Vicevært -> Repper is the normal path: triage is done, this one needs the repair crew. Repper ->
    Vicevært is the same move backwards, for the ticket that turns out not to need Reppergruppen
    after all. Without it the only way back was the Django admin, and a handoff nobody can undo is
    one people hesitate to make in the first place.

    Both crews may move it both ways — "reparationer.move", not a gate per direction. Which crew is holding a
    ticket is a working arrangement between two crews who talk to each other, not a privilege one of
    them holds over the other, and either side of a handoff is equally entitled to say it was wrong.
    The ceiling that does matter — closing a ticket, or committing AK hours to it — sits on `status`
    (MANAGER_ONLY_STATUSES), and this view does not touch status.

    Each direction notifies the crew that just inherited the ticket, never the one letting it go:
    whoever acted already knows.
    """
    task = get_object_or_404(RepairTask, pk=pk)
    responsible = request.POST.get("responsible")
    if responsible not in RepairTask.Responsible.values:
        return _move_response(request, task, "Ukendt ansvarlig.")
    if responsible == task.responsible:
        return _move_response(request, task)  # already there: nothing to save, nobody to notify

    task.responsible = responsible
    task.save(update_fields=["responsible", "updated_at"])
    live.task_changed(task.pk)
    if responsible == RepairTask.Responsible.REPPER:
        services.notify_handed_to_repper(task)
        messages.success(request, "Reparationen er overdraget til Repper.")
    else:
        services.notify_handed_to_vicevaert(task)
        messages.success(request, "Reparationen er sendt tilbage til Viceværterne.")
    return _move_response(request, task)


@require_POST
@permission_required("access.reparationer_manage")
def delete(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    task = get_object_or_404(RepairTask, pk=pk)
    task_pk = task.pk  # delete() clears it
    task.delete()
    live.task_changed(task_pk, "deleted")
    messages.success(request, "Reparationen er slettet.")
    return redirect("reparationer:board")


@require_POST
@permission_required("access.reparationer_manage")
def archive_now(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Archive a Færdig ticket immediately instead of waiting for the nightly sweep
    (archive_finished_repairs). Only meaningful for a Færdig, not-yet-archived ticket — the button
    the page renders already guarantees that, so anything else is a no-op rather than an error."""
    task = get_object_or_404(RepairTask, pk=pk)
    if task.status == RepairTask.Status.FAERDIG and task.archived_at is None:
        task.archived_at = timezone.now()
        task.save(update_fields=["archived_at"])
        live.task_changed(task.pk)
        messages.success(request, "Reparationen er arkiveret.")
    return redirect("reparationer:detail", pk=task.pk)


@require_POST
@permission_required("access.reparationer_manage")
def unarchive(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Bring an archived ticket back onto the board — it reappears in Færdig, exactly where the
    archive found it, since archiving never touched `status`."""
    task = get_object_or_404(RepairTask, pk=pk)
    if task.archived_at is not None:
        task.archived_at = None
        task.save(update_fields=["archived_at"])
        live.task_changed(task.pk)
        messages.success(request, "Reparationen er genåbnet fra arkivet.")
    return redirect("reparationer:detail", pk=task.pk)


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    """One repair with its note thread — the stable page a "→ status" or comment link lands on.
    Works for an archived ticket too (RepairTask.objects is the unfiltered manager), which is the
    whole point of archiving rather than deleting: still findable, still readable."""
    task = get_object_or_404(RepairTask.objects.select_related("reported_by"), pk=pk)
    comments = list(task.comments.select_related("author").prefetch_related("images"))
    for comment in comments:
        comment.can_delete = can_delete_comment(request, comment)  # type: ignore[attr-defined]
    can_manage = request.user.has_perm("access.reparationer_manage")
    return render(
        request,
        "reparationer/detail.html",
        {
            "task": task,
            "task_images": task.images.filter(comment__isnull=True),
            "comments": comments,
            "comment_form": RepairCommentForm(),
            "max_images": MAX_IMAGES,
            "statuses": RepairTask.Status.choices,
            "manager_only_statuses": MANAGER_ONLY_STATUSES,
            "can_manage": can_manage,
            "can_move": request.user.has_perm("access.reparationer_move"),
            "can_handoff": request.user.has_perm("access.reparationer_move"),
            "other_responsible": (
                RepairTask.Responsible.REPPER
                if task.responsible == RepairTask.Responsible.VICEVAERT
                else RepairTask.Responsible.VICEVAERT
            ),
            "can_archive": can_manage
            and task.status == RepairTask.Status.FAERDIG
            and task.archived_at is None,
            "can_unarchive": can_manage and task.archived_at is not None,
        },
    )


@require_POST
@login_required
def create_comment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    """Any resident may add a note — same openness as reporting. See can_delete_comment for removal.

    Photos alone are a whole note. One whose photos were all refused has nothing left in it, and
    lands in the empty branch below with the refusal warnings beside it (see _attached_images)."""
    task = get_object_or_404(RepairTask, pk=pk)
    form = RepairCommentForm(request.POST)
    images = _attached_images(request)
    if not form.is_valid():
        messages.error(request, "Noten kunne ikke gemmes.")
        return redirect("reparationer:detail", pk=task.pk)
    comment = form.save(commit=False)
    if not comment.body and not images:
        messages.error(request, "Skriv en note, eller vedhæft et billede.")
        return redirect("reparationer:detail", pk=task.pk)
    comment.task = task
    comment.author = current_resident(request)
    comment.save()
    _save_images(task, images, comment)
    services.notify_new_comment(comment)
    live.task_changed(task.pk)  # the card's note count
    return redirect("reparationer:detail", pk=task.pk)


@require_POST
@login_required
def delete_comment(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    comment = get_object_or_404(RepairComment, pk=pk)
    if not can_delete_comment(request, comment):
        raise PermissionDenied
    task_pk = comment.task_id
    comment.delete()
    live.task_changed(task_pk)
    messages.success(request, "Noten er slettet.")
    return redirect("reparationer:detail", pk=task_pk)
