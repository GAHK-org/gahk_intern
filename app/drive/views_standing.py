"""Standing folders: defining them for a space, the combined view, and the naming rule's button. The
rules are in standing.py."""

from datetime import date
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.models import Workgroup
from residents.permissions import current_resident

from . import access, standing
from .access import Viewer
from .models import Folder, StandingFolder
from .views import _folder, _render, crumbs


def _space(request: HttpRequest, uuid: UUID) -> tuple[Folder, Viewer]:
    folder, viewer, _ = _folder(request, uuid)
    if folder.kind not in (Folder.Kind.COMMON_ROOT, Folder.Kind.GROUP):
        raise Http404("not a space")
    return folder, viewer


@access.access_required
def manage(request: HttpRequest, uuid: UUID) -> HttpResponse:
    space, viewer = _space(request, uuid)
    if request.method == "POST":
        try:
            standing.define(
                viewer,
                space,
                name=request.POST.get("name", ""),
                item_label=request.POST.get("item_label", ""),
                limited=bool(request.POST.get("limited")),
                writer_groups=[int(v) for v in request.POST.getlist("writer_groups") if v.isdigit()],
                actor=current_resident(request),
            )
        except PermissionDenied:
            messages.error(request, "Du kan ikke oprette stående mapper her.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Den stående mappe er oprettet.")
            return redirect("drive:standing_manage", space.uuid)
    return _render(
        request,
        "drive/standing_manage.html",
        {
            "folder": space,
            "ancestors": crumbs(viewer, space),
            "items": standing.defined(space),
            "can_define": standing.can_define(viewer, space),
            "common": space.kind == Folder.Kind.COMMON_ROOT,
            "groups": Workgroup.objects.order_by("name"),
            "starts_on": standing.next_start(space.space, viewer.today),
        },
    )


@access.access_required
@require_POST
def stop(request: HttpRequest, uuid: UUID, item: UUID) -> HttpResponse:
    space, viewer = _space(request, uuid)
    row = get_object_or_404(StandingFolder, uuid=item, space=space.space, workgroup_id=space.workgroup_id)
    try:
        standing.stop(viewer, row, space, current_resident(request))
    except PermissionDenied:
        messages.error(request, "Du kan ikke stoppe den.")
    else:
        messages.success(request, f"{row.name} oprettes ikke længere i nye perioder.")
    return redirect("drive:standing_manage", space.uuid)


def _readable(request: HttpRequest, uuid: UUID) -> tuple[StandingFolder, Viewer, list[Folder]]:
    item = get_object_or_404(StandingFolder, uuid=uuid)
    viewer = Viewer.for_request(request)
    readable = [copy for copy in standing.copies(item) if viewer.level(copy) >= access.Level.READ]
    if not readable:
        raise Http404("no such standing folder")
    return item, viewer, readable


@access.access_required
def combined(request: HttpRequest, uuid: UUID) -> HttpResponse:
    item, viewer, readable = _readable(request, uuid)
    current = standing.copy_for(item, viewer.today)
    return _render(
        request,
        "drive/standing.html",
        {
            "item": item,
            "entries": standing.combined(viewer, item),
            "ancestors": crumbs(viewer, readable[0], including=False),
            "can_create_item": bool(item.item_label)
            and current is not None
            and viewer.can_create_in(current),
            "today": viewer.today.isoformat(),
        },
    )


@access.access_required
@require_POST
def create_item(request: HttpRequest, uuid: UUID) -> HttpResponse:
    item, viewer, _ = _readable(request, uuid)
    try:
        day = date.fromisoformat(request.POST.get("day", ""))
    except ValueError:
        day = None
    try:
        created = standing.create_item(viewer, item, day, current_resident(request))
    except PermissionDenied:
        messages.error(request, "Du kan ikke oprette den her.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        return redirect("drive:folder", created.uuid)
    return redirect("drive:standing", item.uuid)
