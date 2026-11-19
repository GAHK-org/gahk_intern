"""Drive's pages: the top level, a folder, a file, uploading and downloading. Renaming, moving,
copying, deleting, the bin and old versions are in views_items.py.

A folder a resident may not see is a 404, not a 403: a group's folders are not something a
non-member should be able to confirm the existence of by guessing ids. A folder they may see but
not open - admins and Inspektionen on a group's current period - is a 403.
"""

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.downloads import content_disposition
from photo_album import direct_upload
from photo_album.forms import DirectUploadSignForm
from residents.permissions import current_resident

from . import access, operations, services
from .access import Level, Viewer
from .models import File, FileVersion, Folder
from .periods import period_containing
from .structure import ensure_structure_today

UPLOAD_PURPOSE = "drive"


def _render(request: HttpRequest, template: str, context: dict[str, Any]) -> HttpResponse:
    return render(request, template, {"limited_rollout": access.gate.is_limited(), **context})


def _folder(request: HttpRequest, pk: int, *, at_least: Level = Level.READ) -> tuple[Folder, Viewer, Level]:
    folder = get_object_or_404(Folder, pk=pk, deleted_at__isnull=True)
    viewer = Viewer.for_request(request)
    level = viewer.level(folder)
    if level == Level.NONE:
        raise Http404("no such folder")
    if level < at_least:
        raise PermissionDenied
    return folder, viewer, level


def _is_current(viewer: Viewer, folder: Folder) -> bool:
    """The period or year now running, highlighted in the list as where things go by default."""
    if folder.kind == Folder.Kind.PERIOD:
        return folder.period_start == period_containing(viewer.today).start
    return (
        folder.kind == Folder.Kind.YEAR
        and folder.period_start is not None
        and folder.period_start.year == viewer.today.year
    )


def _rows(viewer: Viewer, folders: list[Folder]) -> list[dict[str, Any]]:
    """The subfolders the viewer may see, with what they may know about each."""
    visible = [(folder, viewer.level(folder)) for folder in folders]
    visible = [(folder, level) for folder, level in visible if level > Level.NONE]
    counted = [folder for folder, _ in visible if folder.kind != Folder.Kind.GROUP]
    totals = services.subtree_totals(counted)
    return [
        {
            "folder": folder,
            "can_open": level >= Level.READ,
            "count": totals.get(folder.pk, (None, None))[0],
            "size": totals.get(folder.pk, (None, None))[1],
            "is_current": _is_current(viewer, folder),
        }
        for folder, level in visible
    ]


def _sorted_children(folder: Folder | None) -> list[Folder]:
    """Timeless first, then periods and years newest first, then residents' folders by name."""
    children = list(Folder.objects.filter(parent=folder, deleted_at__isnull=True).select_related("workgroup"))
    dated = sorted((f for f in children if f.period_start), key=lambda f: f.period_start, reverse=True)  # type: ignore[arg-type, return-value]
    timeless = [f for f in children if f.kind == Folder.Kind.TIMELESS]
    rest = sorted(
        (f for f in children if not f.period_start and f.kind != Folder.Kind.TIMELESS),
        key=lambda f: f.name_key,
    )
    return timeless + dated + rest


@access.access_required
def home(request: HttpRequest) -> HttpResponse:
    ensure_structure_today()
    viewer = Viewer.for_request(request)
    roots = [f for f in _sorted_children(None) if viewer.level(f) > Level.NONE]
    return _render(request, "drive/folder.html", {"folder": None, "rows": _rows(viewer, roots), "files": []})


@access.access_required
def folder(request: HttpRequest, pk: int) -> HttpResponse:
    ensure_structure_today()
    current, viewer, level = _folder(request, pk)
    files = list(
        current.files.filter(deleted_at__isnull=True).select_related("changed_by").order_by("name_key")
    )
    rows = _rows(viewer, _sorted_children(current))
    can_change_files = viewer.can_change_file_in(current)
    return _render(
        request,
        "drive/folder.html",
        {
            "folder": current,
            "ancestors": current.ancestors(),
            "rows": rows,
            "files": files,
            "can_write": level >= Level.WRITE and viewer.can_upload_to(current),
            "can_create_folder": viewer.can_create_in(current),
            "can_change_folder": viewer.can_change_folder(current),
            # Ticking is for moving, copying and deleting: residents' own folders and the files.
            "can_select": bool(files) or any(row["can_open"] and not row["folder"].is_system for row in rows),
            "can_change_files": can_change_files,
            "in_bin": operations.bin_count(viewer, current),
            "upload_config": _upload_config(current),
            "selection_config": {
                "moveUrl": reverse("drive:move", args=[current.pk]),
                "deleteUrl": reverse("drive:delete", args=[current.pk]),
            },
        },
    )


def _upload_config(target: Folder, *, version_of: str = "") -> dict[str, object]:
    return {
        "beginUrl": reverse("drive:upload_begin", args=[target.pk]),
        "signUrl": reverse("drive:upload_sign"),
        "commitUrl": reverse("drive:upload_commit"),
        "directUrl": reverse("drive:upload_direct", args=[target.pk]),
        "maxBytes": services.max_upload_bytes(),
        # "Upload ny version" on a file's page: whatever file is picked becomes its next version.
        "versionOf": version_of,
    }


def live_file(request: HttpRequest, pk: int, *, at_least: Level = Level.READ) -> tuple[File, Viewer]:
    """A file outside the bin, in a folder the resident may open."""
    file = get_object_or_404(File.objects.select_related("folder"), pk=pk, deleted_at__isnull=True)
    _, viewer, _ = _folder(request, file.folder_id, at_least=at_least)
    return file, viewer


@access.access_required
def file_page(request: HttpRequest, pk: int) -> HttpResponse:
    """A file's versions, its history, and what can be done with it."""
    file, viewer = live_file(request, pk)
    versions = list(file.versions.select_related("created_by").order_by("-number"))
    can_change = viewer.can_change_file_in(file.folder)
    return _render(
        request,
        "drive/file.html",
        {
            "file": file,
            "folder": file.folder,
            "ancestors": [*file.folder.ancestors(), file.folder],
            "versions": versions,
            "latest": versions[0],
            "events": file.events.select_related("actor")[:200],
            "can_change": can_change,
            "upload_config": _upload_config(file.folder, version_of=file.name) if can_change else None,
            "move_query": f"file={file.pk}",
        },
    )


@access.access_required
@require_POST
def create_folder(request: HttpRequest, pk: int) -> HttpResponse:
    parent, viewer, _ = _folder(request, pk, at_least=Level.WRITE)
    try:
        created = services.create_folder(
            viewer, parent, request.POST.get("name", ""), current_resident(request)
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("drive:folder", parent.pk)
    return redirect("drive:folder", created.pk)


def _error(message: str, status: int = 400, **extra: object) -> JsonResponse:
    return JsonResponse({"error": message, **extra}, status=status)


def _size(request: HttpRequest) -> int:
    try:
        return int(request.POST.get("size", "0") or 0)
    except ValueError:
        return 0


@access.access_required
@require_POST
def upload_begin(request: HttpRequest, pk: int) -> JsonResponse:
    """Before a byte is sent: a ticket for the bucket, `{"direct": false}` when the file is to be
    posted with the request instead, or a 409 when a file of the name is there already."""
    target, viewer, _ = _folder(request, pk, at_least=Level.WRITE)
    on_conflict = request.POST.get("on_conflict", "")
    size = _size(request)
    try:
        name = services.check_upload(
            viewer,
            target,
            request.POST.get("name", ""),
            size,
            on_conflict,
            ignore_stale=bool(request.POST.get("ignore_stale")),
        )
    except services.NameTaken as exc:
        return _error(" ".join(exc.messages), 409, conflict=True)
    except services.Stale as exc:
        return _error(" ".join(exc.messages), 409, stale=True)
    except ValidationError as exc:
        return _error(" ".join(exc.messages))
    upload = direct_upload.ticket(
        services.new_key(),
        size,
        purpose=UPLOAD_PURPOSE,
        owner=current_resident(request).pk,
        content_type=request.POST.get("type", "") or "application/octet-stream",
        folder=target.pk,
        name=name,
        on_conflict=on_conflict,
    )
    return JsonResponse({"direct": False} if upload is None else {"direct": True, **upload})


@access.access_required
@require_POST
def upload_sign(request: HttpRequest) -> JsonResponse:
    """Uppy's signer, for a ticket upload_begin gave this resident (photo_album.direct_upload.sign)."""
    form = DirectUploadSignForm(request.POST)
    if not form.is_valid():
        return _error("Ugyldig forespørgsel.")
    try:
        signed = direct_upload.sign(
            form.cleaned_data["ticket"],
            owner=current_resident(request).pk,
            method=form.cleaned_data["method"],
            key=form.cleaned_data["key"],
            upload_id=form.cleaned_data["upload_id"],
            part_number=form.cleaned_data["part_number"],
        )
    except ValidationError as exc:
        return _error(" ".join(exc.messages))
    return JsonResponse(signed)


@access.access_required
@require_POST
def upload_commit(request: HttpRequest) -> JsonResponse:
    """The bytes are in the bucket: check them, and make the rows."""
    resident = current_resident(request)
    try:
        upload = direct_upload.finish(
            request.POST.get("ticket", ""), None, purpose=UPLOAD_PURPOSE, owner=resident.pk
        )
    except ValidationError as exc:
        return _error(" ".join(exc.messages))
    target = Folder.objects.filter(pk=upload["folder"]).first()
    viewer = Viewer.for_request(request)
    try:
        if target is None:
            raise PermissionDenied
        added = services.add_upload(
            viewer,
            target,
            name=upload["name"],
            key=upload["key"],
            size=upload["size"],
            content_type=upload["contentType"],
            on_conflict=upload["on_conflict"],
            resident=resident,
        )
    except (PermissionDenied, ValidationError):
        direct_upload.delete(upload["key"])
        return _error("Filen kunne ikke lægges i mappen. Du har måske ikke længere adgang til den.", 403)
    return JsonResponse({"ok": True, "name": added.file.name, "replaced": added.replaced}, status=201)


@access.access_required
@require_POST
def upload_direct(request: HttpRequest, pk: int) -> JsonResponse:
    """The no-bucket path (development, the tests): the file comes with the request. Refused when
    there is a bucket, or this would become the quiet way a 5 GB file goes through the app."""
    if direct_upload.available():
        raise PermissionDenied("direct upload is only for local storage")
    target, viewer, _ = _folder(request, pk, at_least=Level.WRITE)
    upload = request.FILES.get("file")
    if upload is None:
        return _error("Der kom ingen fil med.")
    on_conflict = request.POST.get("on_conflict", "")
    try:
        name = services.check_upload(
            viewer,
            target,
            request.POST.get("name") or upload.name or "",
            upload.size or 0,
            on_conflict,
            ignore_stale=bool(request.POST.get("ignore_stale")),
        )
    except services.NameTaken as exc:
        return _error(" ".join(exc.messages), 409, conflict=True)
    except services.Stale as exc:
        return _error(" ".join(exc.messages), 409, stale=True)
    except ValidationError as exc:
        return _error(" ".join(exc.messages))
    key = services.new_key()
    services.store_local(key, upload)
    added = services.add_upload(
        viewer,
        target,
        name=name,
        key=key,
        size=upload.size or 0,
        content_type=upload.content_type or "application/octet-stream",
        on_conflict=on_conflict,
        resident=current_resident(request),
    )
    return JsonResponse({"ok": True, "name": added.file.name, "replaced": added.replaced}, status=201)


@access.access_required
def download(request: HttpRequest, pk: int, number: int | None = None) -> HttpResponseBase:
    """The latest version, or an old one, under the file's name. Access is checked on every request,
    and the bucket URL it redirects to lives for minutes: a link that keeps working after somebody
    leaves the group would be a key to the group's folder."""
    file, viewer = live_file(request, pk)
    version = file.latest_version() if number is None else get_object_or_404(file.versions, number=number)
    services.remember_download(file, viewer.resident_id, version.number)
    return serve(version, file.name)


def serve(version: FileVersion, name: str) -> HttpResponseBase:
    store = services.storage()
    signed_url = getattr(store, "signed_url", None)
    if signed_url is not None:
        return redirect(
            signed_url(
                version.key,
                expire=5 * 60,
                parameters={"ResponseContentDisposition": content_disposition(name)},
            )
        )
    response = FileResponse(store.open(version.key, "rb"), content_type=version.content_type or None)
    response["Content-Disposition"] = content_disposition(name)
    return response
