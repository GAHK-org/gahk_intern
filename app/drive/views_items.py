"""Changing what is in Drive: renaming, moving and copying (with a folder picker), deleting, the bin,
restoring old versions, and a folder's history. The rules are in operations.py; these pages only
gather the selection and say what happened."""

from typing import Any
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, history, operations
from .access import Level, Viewer
from .models import File, Folder
from .views import _folder, _render, _sorted_children, as_uuid, live_file

MOVE, COPY = "move", "copy"


def _uuids(request: HttpRequest, key: str) -> list[UUID]:
    data = request.POST if request.method == "POST" else request.GET
    return [uuid for value in data.getlist(key) if (uuid := as_uuid(value))]


def _selection(request: HttpRequest, source: Folder) -> operations.Selection:
    return operations.select(source, _uuids(request, "file"), _uuids(request, "folder"))


def _query(op: str, selection: operations.Selection) -> str:
    """The picker's query string: what is being moved or copied."""
    return "&".join(
        [f"op={op}"]
        + [f"file={file.uuid}" for file in selection.files]
        + [f"folder={folder.uuid}" for folder in selection.folders]
    )


def _fail(request: HttpRequest, exc: ValidationError) -> None:
    messages.error(request, " ".join(exc.messages))


@access.access_required
@require_POST
def rename_file(request: HttpRequest, uuid: UUID) -> HttpResponse:
    file, viewer = live_file(request, uuid)
    try:
        operations.rename_file(viewer, file, request.POST.get("name", ""), current_resident(request))
    except ValidationError as exc:
        _fail(request, exc)
    return redirect("drive:file", file.uuid)


@access.access_required
@require_POST
def rename_folder(request: HttpRequest, uuid: UUID) -> HttpResponse:
    folder, viewer, _ = _folder(request, uuid)
    try:
        operations.rename_folder(viewer, folder, request.POST.get("name", ""), current_resident(request))
    except ValidationError as exc:
        _fail(request, exc)
    return redirect("drive:folder", folder.uuid)


def _check(viewer: Viewer, op: str, selection: operations.Selection, destination: Folder) -> str:
    """Why the selection cannot go to `destination`, or "" if it can."""
    try:
        if op == MOVE:
            operations.check_move(viewer, selection, destination)
        else:
            operations.check_copy(viewer, selection, destination)
    except PermissionDenied:
        return "Du har ikke lov til det."
    except ValidationError as exc:
        return " ".join(exc.messages)
    return ""


@access.access_required
def move(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """GET: pick where the ticked items go, folder by folder. POST: move or copy them there."""
    source, viewer, _ = _folder(request, uuid)
    data = request.POST if request.method == "POST" else request.GET
    op = COPY if data.get("op") == COPY else MOVE
    selection = _selection(request, source)
    if not selection:
        messages.error(request, "Vælg noget først.")
        return redirect("drive:folder", source.uuid)

    if request.method == "POST":
        to = as_uuid(data.get("to"))
        if to is None:
            raise Http404("no folder to go to")
        destination, _, _ = _folder(request, to)
        if op == MOVE and operations.changes_access(selection, destination) and not data.get("confirm"):
            return redirect(f"{request.path}?{_query(op, selection)}&at={destination.uuid}")
        resident = current_resident(request)
        try:
            if op == MOVE:
                operations.move(viewer, selection, destination, resident)
                messages.success(request, f"Flyttet til {destination.name}.")
            else:
                count = operations.copy(viewer, selection, destination, resident)
                messages.success(
                    request, f"{count} fil{'er' if count != 1 else ''} kopieret til {destination.name}."
                )
        except PermissionDenied:
            messages.error(request, "Du har ikke lov til det.")
            return redirect("drive:folder", source.uuid)
        except ValidationError as exc:
            _fail(request, exc)
            return redirect("drive:folder", source.uuid)
        return redirect("drive:folder", destination.uuid)

    # Browsing starts in the source folder; `at=` (empty) is Drive's top level.
    at_value = data.get("at")
    if at_value is None:
        at: Folder | None = source
    elif at_uuid := as_uuid(at_value):
        at = _folder(request, at_uuid)[0]
    else:
        at = None
    moving = {folder.pk for folder in selection.folders}
    children = [
        folder
        for folder in _sorted_children(at)
        if folder.pk not in moving and viewer.level(folder) >= Level.READ
    ]
    context: dict[str, Any] = {
        "op": op,
        "source": source,
        "selection": selection,
        "at": at,
        "ancestors": at.ancestors() if at else [],
        "children": children,
        "query": _query(op, selection),
    }
    if at is not None:
        context["problem"] = _check(viewer, op, selection, at)
        if op == MOVE and not context["problem"] and operations.changes_access(selection, at):
            context["access_before"] = operations.access_note(source)
            context["access_after"] = operations.access_note(at)
            context["shares_ending"] = operations.ends_shares(selection, at)
    return _render(request, "drive/move.html", context)


@access.access_required
@require_POST
def delete(request: HttpRequest, uuid: UUID) -> HttpResponse:
    source, viewer, _ = _folder(request, uuid)
    selection = _selection(request, source)
    try:
        operations.delete(viewer, selection, current_resident(request))
    except PermissionDenied:
        messages.error(request, "Du har ikke lov til at slette det.")
    except ValidationError as exc:
        _fail(request, exc)
    else:
        count = len(selection.files) + len(selection.folders)
        messages.success(
            request,
            f"{count} lagt i papirkurven." if count != 1 else f"{selection.names[0]} er lagt i papirkurven.",
        )
    return redirect("drive:folder", source.uuid)


@access.access_required
def bin_page(request: HttpRequest, uuid: UUID) -> HttpResponse:
    folder, viewer, _ = _folder(request, uuid)
    return _render(
        request,
        "drive/bin.html",
        {
            "folder": folder,
            "ancestors": [*folder.ancestors(), folder],
            "binned": operations.binned(viewer, folder),
        },
    )


@access.access_required
@require_POST
def restore(request: HttpRequest, uuid: UUID) -> HttpResponse:
    file = get_object_or_404(File.objects.select_related("folder"), uuid=uuid, deleted_at__isnull=False)
    viewer = Viewer.for_request(request)
    if viewer.level(file.folder) == Level.NONE:
        raise Http404("no such file")
    restored = operations.restore(viewer, file, current_resident(request))
    messages.success(request, f"{restored.name} er gendannet.")
    return redirect("drive:folder", restored.folder.uuid)


@access.access_required
@require_POST
def restore_version(request: HttpRequest, uuid: UUID, number: int) -> HttpResponse:
    file, viewer = live_file(request, uuid)
    try:
        version = operations.restore_version(viewer, file, number, current_resident(request))
    except ValidationError as exc:
        _fail(request, exc)
    else:
        messages.success(request, f"Version {number} er gendannet som version {version.number}.")
    return redirect("drive:file", file.uuid)


@access.access_required
def folder_history(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """The folder's own lines, and those of the files and folders in it now."""
    folder, viewer, _ = _folder(request, uuid)
    events = history.for_folder(viewer, folder)
    return _render(
        request,
        "drive/history.html",
        {"folder": folder, "ancestors": [*folder.ancestors(), folder], "events": events},
    )
