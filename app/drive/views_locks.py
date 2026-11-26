"""Locking by hand, asking for an unlock or a permanent deletion, deciding those requests, and a
space's activity page. The rules are in locks.py."""

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, history, locks
from .access import Level, Viewer
from .models import ApprovalRequest, File, Folder
from .views import _folder, _render, as_uuid, live_file


def _fail(request: HttpRequest, exc: ValidationError) -> None:
    messages.error(request, " ".join(exc.messages))


def _days(request: HttpRequest) -> int:
    try:
        return int(request.POST.get("days", "0"))
    except ValueError:
        return 0


@access.access_required
@require_POST
def lock_file(request: HttpRequest, uuid: UUID) -> HttpResponse:
    file, viewer = live_file(request, uuid)
    locks.lock_file(viewer, file, current_resident(request))
    messages.success(request, f"{file.name} er låst.")
    return redirect("drive:file", file.uuid)


@access.access_required
@require_POST
def lock_folder(request: HttpRequest, uuid: UUID) -> HttpResponse:
    folder, viewer, _ = _folder(request, uuid)
    locks.lock_folder(viewer, folder, current_resident(request))
    messages.success(request, f"{folder.name} er låst.")
    return redirect("drive:folder", folder.uuid)


@access.access_required
@require_POST
def unlock_file(request: HttpRequest, uuid: UUID) -> HttpResponse:
    file, viewer = live_file(request, uuid)
    try:
        locks.request_unlock(
            viewer, file, _days(request), request.POST.get("reason", ""), current_resident(request)
        )
    except ValidationError as exc:
        _fail(request, exc)
    else:
        messages.success(request, "Anmodningen er sendt til admins og Inspektionen.")
    return redirect("drive:file", file.uuid)


@access.access_required
@require_POST
def unlock_folder(request: HttpRequest, uuid: UUID) -> HttpResponse:
    folder, viewer, _ = _folder(request, uuid)
    try:
        locks.request_unlock(
            viewer, folder, _days(request), request.POST.get("reason", ""), current_resident(request)
        )
    except ValidationError as exc:
        _fail(request, exc)
    else:
        messages.success(request, "Anmodningen er sendt til admins og Inspektionen.")
    return redirect("drive:folder", folder.uuid)


@access.access_required
@require_POST
def purge_request(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """A version of a file (`number`), or a file in the bin (no number)."""
    file = get_object_or_404(File.objects.select_related("folder"), uuid=uuid)
    viewer = Viewer.for_request(request)
    if viewer.level(file.folder) < Level.READ:
        raise Http404("no such file")
    number = request.POST.get("number", "")
    try:
        locks.request_purge(
            viewer,
            file,
            int(number) if number.isdigit() else None,
            request.POST.get("reason", ""),
            current_resident(request),
        )
    except ValidationError as exc:
        _fail(request, exc)
    else:
        messages.success(request, "Anmodningen er sendt til admins og Inspektionen.")
    if file.deleted_at is not None:
        return redirect("drive:bin", as_uuid(request.POST.get("bin")) or file.folder.uuid)
    return redirect("drive:file", file.uuid)


def _decider(request: HttpRequest) -> Viewer:
    """Admins, Inspektionen and the moderators of a Delt mappe."""
    viewer = Viewer.for_request(request)
    if not (viewer.oversees or any(viewer.moderates(pk) for pk in viewer.shared_roles)):
        raise PermissionDenied
    return viewer


@access.access_required
def requests_page(request: HttpRequest) -> HttpResponse:
    """Admins' and Inspektionen's: what waits for a decision, and what was decided. Names and paths
    only - nothing here opens a file."""
    rows = locks.decides(_decider(request)).select_related("requested_by", "decided_by")
    return _render(
        request,
        "drive/requests.html",
        {
            "pending": rows.filter(state=ApprovalRequest.State.PENDING).order_by("requested_at"),
            "decided": rows.exclude(state=ApprovalRequest.State.PENDING).order_by("-decided_at")[:100],
            "me": current_resident(request).pk,
        },
    )


@access.access_required
@require_POST
def decide(request: HttpRequest, uuid: UUID) -> HttpResponse:
    viewer = _decider(request)
    item = get_object_or_404(ApprovalRequest, uuid=uuid)
    approve = request.POST.get("decision") == "approve"
    try:
        locks.decide(
            viewer,
            item,
            approve=approve,
            reason=request.POST.get("reason", ""),
            actor=current_resident(request),
        )
    except ValidationError as exc:
        _fail(request, exc)
    else:
        messages.success(request, f"{item.name}: {'godkendt' if approve else 'afvist'}.")
    return redirect("drive:requests")


@access.access_required
def activity(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """A space's history: Fælles, or one group's folders across all periods. A line shows only to
    those who could read the item where it was."""
    space, viewer, _ = _folder(request, uuid)
    if space.kind not in (Folder.Kind.COMMON_ROOT, Folder.Kind.GROUP, Folder.Kind.SHARED):
        raise Http404("not a space")
    shown = history.for_space(viewer, space)
    return _render(
        request,
        "drive/history.html",
        {"folder": space, "ancestors": [*space.ancestors(), space], "events": shown, "title": "Aktivitet"},
    )
