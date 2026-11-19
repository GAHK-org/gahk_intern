"""Creating a Delt mappe, and its page of members and expiry. The rules are in shared.py."""

from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, shared
from .access import Level, Viewer
from .models import MIN_SHARED_MEMBERS, SharedFolder, SharedMember
from .views import _render


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _ids(request: HttpRequest) -> list[int]:
    return [int(value) for value in request.POST.getlist("members") if value.isdigit()]


@access.access_required
def create(request: HttpRequest) -> HttpResponse:
    viewer = Viewer.for_request(request)
    me = current_resident(request)
    if request.method == "POST":
        try:
            folder = shared.create(
                viewer,
                name=request.POST.get("name", ""),
                members=_ids(request),
                expires_on=_date(request.POST.get("expires_on", "")),
                on_expiry=request.POST.get("on_expiry", ""),
                actor=me,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            return redirect("drive:folder", folder.pk)
    chosen = set(_ids(request))
    return _render(
        request,
        "drive/shared_new.html",
        {
            "residents": [r for r in shared.residents_to_invite() if r.pk != me.pk],
            "chosen": chosen,
            "name": request.POST.get("name", ""),
            "expires_on": request.POST.get("expires_on") or shared.default_expiry().isoformat(),
            "latest": shared.latest_new_expiry().isoformat(),
            "on_expiry": request.POST.get("on_expiry", SharedFolder.OnExpiry.ARCHIVE),
            "min_others": MIN_SHARED_MEMBERS - 1,
        },
    )


def _shared(request: HttpRequest, pk: int) -> tuple[SharedFolder, Viewer]:
    """A Delt mappe its members may see; to anyone else it does not exist."""
    item = get_object_or_404(SharedFolder, pk=pk)
    viewer = Viewer.for_request(request)
    top = item.top()
    if top.deleted_at is not None or viewer.level(top) < Level.READ:
        raise Http404("no such shared folder")
    return item, viewer


@access.access_required
def page(request: HttpRequest, pk: int) -> HttpResponse:
    item, viewer = _shared(request, pk)
    me = current_resident(request)
    members = list(
        item.members.select_related("resident").order_by("resident__first_name", "resident__last_name")
    )
    taken = {member.resident_id for member in members}
    top = item.top()
    return _render(
        request,
        "drive/shared.html",
        {
            "shared": item,
            "folder": top,
            "ancestors": [*top.ancestors(), top],
            "members": members,
            "moderates": viewer.moderates(item.pk),
            "is_member": item.pk in viewer.shared_roles,
            "me": me.pk,
            "roles": SharedMember.Role.choices,
            "others": [r for r in shared.residents_to_invite() if r.pk not in taken],
            "latest": item.latest_expiry().isoformat(),
            "can_archive": item.on_expiry == SharedFolder.OnExpiry.ARCHIVE,
        },
    )


def _act(request: HttpRequest, pk: int, action) -> HttpResponse:  # noqa: ANN001
    item, viewer = _shared(request, pk)
    try:
        done = action(viewer, item, current_resident(request))
    except PermissionDenied:
        messages.error(request, "Det kan kun mappens moderatorer.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if done:
            messages.success(request, done)
    return redirect("drive:shared", item.pk)


def _resident(request: HttpRequest) -> int:
    value = request.POST.get("resident", "")
    return int(value) if value.isdigit() else 0


@access.access_required
@require_POST
def add_member(request: HttpRequest, pk: int) -> HttpResponse:
    return _act(
        request,
        pk,
        lambda viewer, item, me: (
            shared.add_member(viewer, item, _resident(request), request.POST.get("role", ""), me)
            or "Medlemmet er tilføjet."
        ),
    )


@access.access_required
@require_POST
def set_role(request: HttpRequest, pk: int) -> HttpResponse:
    return _act(
        request,
        pk,
        lambda viewer, item, me: (
            shared.set_role(viewer, item, _resident(request), request.POST.get("role", ""), me)
            or "Rollen er ændret."
        ),
    )


@access.access_required
@require_POST
def remove_member(request: HttpRequest, pk: int) -> HttpResponse:
    resident = _resident(request)
    me = current_resident(request)
    item, viewer = _shared(request, pk)
    try:
        shared.remove_member(viewer, item, resident, me)
    except PermissionDenied:
        messages.error(request, "Det kan kun mappens moderatorer.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if resident == me.pk:
            messages.success(request, "Du har forladt mappen.")
            return redirect("drive:home")
        messages.success(request, "Medlemmet er fjernet.")
    return redirect("drive:shared", item.pk)


@access.access_required
@require_POST
def set_expiry(request: HttpRequest, pk: int) -> HttpResponse:
    return _act(
        request,
        pk,
        lambda viewer, item, me: (
            shared.set_expiry(
                viewer, item, _date(request.POST.get("expires_on", "")), request.POST.get("on_expiry", ""), me
            )
            or "Udløbet er gemt."
        ),
    )


@access.access_required
@require_POST
def rename(request: HttpRequest, pk: int) -> HttpResponse:
    return _act(
        request,
        pk,
        lambda viewer, item, me: shared.rename(viewer, item, request.POST.get("name", ""), me) or "",
    )


@access.access_required
@require_POST
def delete(request: HttpRequest, pk: int) -> HttpResponse:
    item, viewer = _shared(request, pk)
    try:
        shared.delete(viewer, item, current_resident(request))
    except PermissionDenied:
        messages.error(request, "Det kan kun mappens moderatorer.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("drive:shared", item.pk)
    messages.success(request, "Den delte mappe er lagt i papirkurven.")
    return redirect("drive:home")
