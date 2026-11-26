"""Sharing a file or folder, stopping a share, `Delt med mig`, and `Delt med <gruppe>`. The rules are
in sharing.py and access.py."""

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, sharing
from .access import Level, Viewer
from .models import File, Folder, Share
from .views import _folder, _render, crumbs, live_file


def _recipient(request: HttpRequest) -> tuple[int | None, int | None]:
    """`resident:<id>` or `group:<id>`, from the one select that offers both."""
    kind, _, value = request.POST.get("recipient", "").partition(":")
    if not value.isdigit():
        return None, None
    return (
        (int(value), None) if kind == "resident" else (None, int(value)) if kind == "group" else (None, None)
    )


def _share(request: HttpRequest, viewer: Viewer, **item: File | Folder) -> None:
    resident_id, workgroup_id = _recipient(request)
    try:
        sharing.share(
            viewer,
            resident_id=resident_id,
            workgroup_id=workgroup_id,
            access=request.POST.get("access", ""),
            actor=current_resident(request),
            **item,  # type: ignore[arg-type]
        )
    except PermissionDenied:
        messages.error(request, "Du kan ikke dele det.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Delt.")


@access.access_required
@require_POST
def share_file(request: HttpRequest, uuid: UUID) -> HttpResponse:
    file, viewer = live_file(request, uuid)
    _share(request, viewer, file=file)
    return redirect("drive:file", file.uuid)


@access.access_required
@require_POST
def share_folder(request: HttpRequest, uuid: UUID) -> HttpResponse:
    folder, viewer, _ = _folder(request, uuid)
    _share(request, viewer, folder=folder)
    return redirect("drive:folder", folder.uuid)


@access.access_required
@require_POST
def stop(request: HttpRequest, uuid: UUID) -> HttpResponse:
    row = get_object_or_404(Share.objects.select_related("file__folder", "folder"), uuid=uuid)
    viewer = Viewer.for_request(request)
    item_folder = row.file.folder if row.file else row.folder
    if item_folder is None or viewer.level(item_folder) < Level.READ:
        raise Http404("no such share")
    try:
        sharing.stop(viewer, row, current_resident(request))
    except (PermissionDenied, ValidationError):
        messages.error(request, "Du kan ikke stoppe den deling.")
    else:
        messages.success(request, "Delingen er stoppet.")
    if row.file is not None:
        return redirect("drive:file", row.file.uuid)
    return redirect("drive:folder", item_folder.uuid)


@access.access_required
def shared_with_me(request: HttpRequest) -> HttpResponse:
    viewer = Viewer.for_request(request)
    return _render(
        request,
        "drive/shared_list.html",
        {"title": "Delt med mig", "items": sharing.shared_with_me(viewer), "ancestors": []},
    )


@access.access_required
def shared_with_group(request: HttpRequest, uuid: UUID) -> HttpResponse:
    folder, viewer, _ = _folder(request, uuid)
    items = sharing.shared_with_group(viewer, folder)
    if folder.kind not in (Folder.Kind.PERIOD, Folder.Kind.TIMELESS) or folder.workgroup is None:
        raise Http404("not a group's period")
    return _render(
        request,
        "drive/shared_list.html",
        {
            "title": f"Delt med {folder.workgroup.name}",
            "items": items,
            "ancestors": crumbs(viewer, folder),
            "folder": folder,
        },
    )
