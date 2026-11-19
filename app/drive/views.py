"""Drive's pages: the top level, a folder, a file, uploading and downloading. Renaming, moving,
copying, deleting, the bin and old versions are in views_items.py.

A folder a resident may not see is a 404, not a 403: a group's folders are not something a
non-member should be able to confirm the existence of by guessing ids. A folder they may see but
not open - admins and Inspektionen on a group's current period - is a 403.
"""

from datetime import date
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.downloads import content_disposition
from core.models import Workgroup
from photo_album import direct_upload
from photo_album.forms import DirectUploadSignForm
from residents.permissions import current_resident

from . import access, locks, office, operations, preview, services, shared, sharing
from .access import Level, Viewer
from .models import MAX_UNLOCK_DAYS, ApprovalRequest, File, FileVersion, Folder, SharedMember, Space
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
    # Not a group folder, nor Delte mapper: their totals would count what the reader cannot see.
    counted = [
        folder for folder, _ in visible if folder.kind not in (Folder.Kind.GROUP, Folder.Kind.SHARED_ROOT)
    ]
    totals = services.subtree_totals(counted)
    members = dict(
        SharedMember.objects.filter(
            shared__in=[f.shared_id for f, _ in visible if f.kind == Folder.Kind.SHARED]
        )
        .values("shared")
        .annotate(n=Count("pk"))
        .values_list("shared", "n")
    )
    return [
        {
            "folder": folder,
            # Admins and Inspektionen see a Delt mappe only by its number.
            "name": f"Delt mappe {folder.shared.number}"
            if folder.kind == Folder.Kind.SHARED and level == Level.META and folder.shared
            else folder.name,
            "members": members.get(folder.shared_id or 0) if folder.kind == Folder.Kind.SHARED else None,
            "can_open": level >= Level.READ,
            "count": totals.get(folder.pk, (None, None))[0],
            "size": totals.get(folder.pk, (None, None))[1],
            "is_current": _is_current(viewer, folder),
        }
        for folder, level in visible
    ]


def _sorted_children(folder: Folder | None) -> list[Folder]:
    """Timeless first, then periods and years newest first, then residents' folders by name."""
    children = list(
        Folder.objects.filter(parent=folder, deleted_at__isnull=True).select_related("workgroup", "shared")
    )
    dated = sorted((f for f in children if f.period_start), key=lambda f: f.period_start, reverse=True)  # type: ignore[arg-type, return-value]
    timeless = [f for f in children if f.kind == Folder.Kind.TIMELESS]
    rest = sorted(
        (f for f in children if not f.period_start and f.kind != Folder.Kind.TIMELESS),
        key=lambda f: f.name_key,
    )
    if folder is not None and folder.kind == Folder.Kind.SHARED_ROOT:
        return rest + dated  # the Delte mapper in use, then the archived ones by year
    return timeless + dated + rest


@access.access_required
def home(request: HttpRequest) -> HttpResponse:
    ensure_structure_today()
    viewer = Viewer.for_request(request)
    roots = [f for f in _sorted_children(None) if viewer.level(f) > Level.NONE]
    return _render(
        request,
        "drive/folder.html",
        {
            "folder": None,
            "rows": _rows(viewer, roots),
            "files": [],
            "decides": viewer.oversees or any(viewer.moderates(pk) for pk in viewer.shared_roles),
            "pending_requests": locks.pending_count(viewer),
        },
    )


# The file list's columns: what they sort by, and which way first - a name A to Z, the biggest and the
# newest first.
SORTS = {
    "name": ("Navn", lambda file: file.name_key, False),
    "size": ("Størrelse", lambda file: file.size, True),
    "changed": ("Ændret", lambda file: file.changed_at, True),
}


def _sorting(request: HttpRequest) -> tuple[str, bool]:
    sort = request.GET.get("sort", "")
    if sort not in SORTS:
        return "name", False
    order = request.GET.get("order", "")
    return sort, order == "desc" if order in ("asc", "desc") else SORTS[sort][2]


def _sort_links(sort: str, descending: bool) -> list[dict[str, object]]:
    """A link per column: the active one turns the order round, the others start their own way."""
    links = []
    for key, (label, _, first_descending) in SORTS.items():
        down = not descending if key == sort else first_descending
        links.append(
            {"label": label, "query": f"sort={key}&order={'desc' if down else 'asc'}", "active": key == sort}
        )
    return links


@access.access_required
def folder(request: HttpRequest, pk: int) -> HttpResponse:
    ensure_structure_today()
    current, viewer, level = _folder(request, pk)
    sort, descending = _sorting(request)
    files = sorted(
        current.files.filter(deleted_at__isnull=True).select_related("changed_by"),
        key=lambda file: (SORTS[sort][1](file), file.name_key),
        reverse=descending,
    )
    rows = _rows(viewer, _sorted_children(current))
    can_change_files = viewer.can_change_file_in(current)
    shared_folder = current.shared if current.kind == Folder.Kind.SHARED else None
    instead = _instead(viewer, current)
    shared_files, shared_folders = sharing.shared_ids(files, [row["folder"] for row in rows])
    mark_files(files)
    for file in files:
        file.is_shared = file.pk in shared_files  # type: ignore[attr-defined]
    for row in rows:
        row["is_shared"] = row["folder"].pk in shared_folders
    return _render(
        request,
        "drive/folder.html",
        {
            "folder": current,
            "ancestors": crumbs(viewer, current, including=False),
            "rows": rows,
            "group_shared": bool(sharing.shared_with_group(viewer, current)),
            **_share_context(viewer, folder=current),
            "files": files,
            "can_write": level >= Level.WRITE and viewer.can_upload_to(current),
            "can_create_folder": viewer.can_create_in(current),
            "can_change_folder": viewer.can_change_folder(current),
            **_lock_context(viewer, current),
            "is_space": current.kind in (Folder.Kind.COMMON_ROOT, Folder.Kind.GROUP, Folder.Kind.SHARED),
            "shared": shared_folder,
            "shared_members": shared_folder.members.count() if shared_folder else 0,
            "can_create_shared": current.kind == Folder.Kind.SHARED_ROOT and viewer.living,
            # Ticking is for moving, copying and deleting: residents' own folders and the files.
            "can_select": bool(files) or any(row["can_open"] and not row["folder"].is_system for row in rows),
            "can_change_files": can_change_files,
            "in_bin": operations.bin_count(viewer, current),
            "upload_config": {**_upload_config(current), "instead": instead},
            "office_new": office.NEW
            if level >= Level.WRITE and viewer.can_upload_to(current) and office.enabled()
            else {},
            "instead": instead,
            "standing_item": current.standing if current.kind == Folder.Kind.STANDING else None,
            "space_root": current.kind in (Folder.Kind.COMMON_ROOT, Folder.Kind.GROUP),
            "selection_config": {
                "moveUrl": reverse("drive:move", args=[current.pk]),
                "deleteUrl": reverse("drive:delete", args=[current.pk]),
                "zipUrl": reverse("drive:request_zip", args=[current.pk]),
            },
            "sort_links": _sort_links(sort, descending),
            "descending": descending,
        },
    )


def _instead(viewer: Viewer, folder: Folder) -> dict[str, object] | None:
    """On `Uden periode` and `Uden årstal` themselves: the current period or year, offered before
    anything is put directly in the timeless folder - which outlives every period, and which
    admins and Inspektionen read."""
    if folder.kind != Folder.Kind.TIMELESS:
        return None
    if folder.space == Space.COMMON:
        start = date(viewer.today.year, 1, 1)
        kind = Folder.Kind.YEAR
    else:
        start = period_containing(viewer.today).start
        kind = Folder.Kind.PERIOD
    current = Folder.objects.filter(
        parent=folder.parent, kind=kind, period_start=start, deleted_at__isnull=True
    ).first()
    if current is None or not viewer.can_upload_to(current):
        return None
    return {
        "name": current.name,
        "here": folder.name,
        "url": reverse("drive:folder", args=[current.pk]),
        "beginUrl": reverse("drive:upload_begin", args=[current.pk]),
        "directUrl": reverse("drive:upload_direct", args=[current.pk]),
        "createUrl": reverse("drive:create_folder", args=[current.pk]),
        "officeUrl": reverse("drive:office_new", args=[current.pk]),
    }


def _lock_context(viewer: Viewer, folder: Folder) -> dict[str, object]:
    """Whether the folder is locked (by itself or one above), and what the viewer can do about it."""
    holder = viewer.lock_holder(folder)
    own = holder is not None and holder.pk == folder.pk
    return {
        "lock_holder": holder,
        "can_lock_folder": not folder.is_system
        and holder is None
        and folder.deleted_at is None
        and viewer.may_write(folder),
        "can_request_folder_unlock": own
        and viewer.may_write(folder)
        and not ApprovalRequest.objects.filter(
            kind=ApprovalRequest.Kind.UNLOCK, state=ApprovalRequest.State.PENDING, folder=folder
        ).exists(),
        "max_unlock_days": MAX_UNLOCK_DAYS,
    }


def _share_context(
    viewer: Viewer, *, file: File | None = None, folder: Folder | None = None
) -> dict[str, object]:
    """Who the item is shared with, shown to everyone who can read it, and the form to share it."""
    where = file.folder if file is not None else folder
    can_share = (
        where is not None
        and (file is not None or (folder is not None and folder.kind == Folder.Kind.FOLDER))
        and viewer.can_share(where)
    )
    return {
        "shares": sharing.shares_on(file=file, folder=folder)
        if file or (folder and folder.kind == Folder.Kind.FOLDER)
        else [],
        "can_share": can_share,
        "share_residents": shared.residents_to_invite() if can_share else [],
        "share_groups": Workgroup.objects.exclude(pk=where.workgroup_id or 0).order_by("name")
        if can_share and where
        else [],
    }


def _version_config(file: File) -> dict[str, object]:
    """`Upload ny version` on a file's page: through the file, so an edit share is enough."""
    return {
        **_upload_config(file.folder, version_of=file.name),
        "beginUrl": reverse("drive:version_begin", args=[file.pk]),
        "directUrl": reverse("drive:version_direct", args=[file.pk]),
    }


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
    """A file outside the bin that the resident may read: in a folder they may open, or shared with
    them on its own."""
    file = get_object_or_404(File.objects.select_related("folder"), pk=pk, deleted_at__isnull=True)
    viewer = Viewer.for_request(request)
    level = viewer.file_level(file)
    if level < Level.READ:
        if level == Level.NONE:
            raise Http404("no such file")
        raise PermissionDenied
    if level < at_least:
        raise PermissionDenied
    return file, viewer


def mark_files(files: list[File]) -> None:
    """The version number of each file's thumbnail, where it has one: it goes in the URL."""
    thumbnails = preview.thumbnails(files)
    for file in files:
        file.thumbnail = thumbnails.get(file.pk)  # type: ignore[attr-defined]


def _preview_context(viewer: Viewer, file: File, latest: FileVersion) -> dict[str, object]:
    """What the file's page shows of it (drive/preview.py)."""
    kind = preview.kind_of(file.name)
    shown: dict[str, object] = {"kind": kind.value}
    if kind == preview.Kind.IMAGE:
        row = preview.ensure(latest, file.name)
        shown["state"] = row.state if row else "too_big"
        if viewer.level(file.folder) >= Level.READ:
            # Next and previous through the folder's images, by name as the list has them.
            images = [
                (pk, name)
                for pk, name in file.folder.files.filter(deleted_at__isnull=True)
                .order_by("name_key")
                .values_list("pk", "name")
                if preview.kind_of(name) == preview.Kind.IMAGE
            ]
            at = next(index for index, (pk, _) in enumerate(images) if pk == file.pk)
            shown["position"] = (at + 1, len(images))
            shown["previous"] = images[at - 1][0] if at > 0 else None
            shown["next"] = images[at + 1][0] if at + 1 < len(images) else None
    elif kind in (preview.Kind.PDF, preview.Kind.VIDEO, preview.Kind.AUDIO):
        shown["inline"] = preview.inline_type(file.name) is not None
    elif kind in (preview.Kind.TEXT, preview.Kind.CSV, preview.Kind.MARKDOWN):
        try:
            shown["text"] = preview.text(latest, file.name)
        except OSError:  # the bytes cannot be read just now; the file can still be downloaded
            shown["text"] = None
        shown["text_limit"] = preview.TEXT_BYTES
        shown["csv_rows"] = preview.CSV_ROWS
    return shown


def _office_context(viewer: Viewer, file: File) -> dict[str, bool] | None:
    """What live editing offers for the file, when it is on and opens the file at all."""
    if office.document_type(file.name) is None or not office.enabled():
        return None
    return {
        "edit": office.editable(file.name) and viewer.can_version(file),
        # An older format: a docx, xlsx or pptx copy beside it, for whoever may put one there.
        "convert": not office.editable(file.name) and viewer.can_upload_to(file.folder),
        "in_use": file.in_live_edit(),
    }


def crumbs(viewer: Viewer, folder: Folder, *, including: bool = True) -> list[Folder]:
    """The path to `folder`, each marked with whether the viewer may open it: a share lets somebody
    in partway down, and the folders above are then names, not links."""
    path = [*folder.ancestors(), folder] if including else folder.ancestors()
    for item in path:
        item.can_open = viewer.level(item) >= Level.READ  # type: ignore[attr-defined]
    return path


@access.access_required
def file_page(request: HttpRequest, pk: int) -> HttpResponse:
    """A file's versions, its history, and what can be done with it."""
    file, viewer = live_file(request, pk)
    versions = list(file.versions.select_related("created_by").order_by("-number"))
    can_change = viewer.can_change_file(file)
    locked = file.lock_active()
    return _render(
        request,
        "drive/file.html",
        {
            "file": file,
            "folder": file.folder,
            "ancestors": crumbs(viewer, file.folder),
            "versions": versions,
            "latest": versions[0],
            "preview": _preview_context(viewer, file, versions[0]),
            "events": file.events.select_related("actor")[:200],
            "can_change": can_change,
            "file_locked": locked,
            "folder_lock": viewer.lock_holder(file.folder),
            "can_lock": can_change,
            "can_request_unlock": locked
            and viewer.may_write(file.folder)
            and not ApprovalRequest.objects.filter(
                kind=ApprovalRequest.Kind.UNLOCK, state=ApprovalRequest.State.PENDING, file=file
            ).exists(),
            "purge_pending": set(
                ApprovalRequest.objects.filter(
                    kind=ApprovalRequest.Kind.PURGE, state=ApprovalRequest.State.PENDING, file=file
                ).values_list("version_number", flat=True)
            ),
            "max_unlock_days": MAX_UNLOCK_DAYS,
            "upload_config": _version_config(file) if viewer.can_version(file) else None,
            "can_version": viewer.can_version(file),
            "can_copy": viewer.level(file.folder) >= Level.READ,
            "office": _office_context(viewer, file),
            "move_query": f"file={file.pk}",
            **_share_context(viewer, file=file),
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
        if upload.get("file"):
            versioned = File.objects.filter(pk=upload["file"]).first()
            if versioned is None:
                raise PermissionDenied
            services.add_version(
                viewer,
                versioned,
                key=upload["key"],
                size=upload["size"],
                content_type=upload["contentType"],
                resident=resident,
            )
            return JsonResponse({"ok": True, "name": versioned.name, "replaced": True}, status=201)
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


@access.access_required
@require_POST
def version_begin(request: HttpRequest, pk: int) -> JsonResponse:
    """A new version of a file, from its page: a ticket for the bucket, or `{"direct": false}`."""
    file, viewer = live_file(request, pk)
    size = _size(request)
    try:
        services.check_version(viewer, file, size, ignore_stale=bool(request.POST.get("ignore_stale")))
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
        folder=file.folder_id,
        name=file.name,
        on_conflict=services.REPLACE,
        file=file.pk,
    )
    return JsonResponse({"direct": False} if upload is None else {"direct": True, **upload})


@access.access_required
@require_POST
def version_direct(request: HttpRequest, pk: int) -> JsonResponse:
    """The no-bucket path for a new version."""
    if direct_upload.available():
        raise PermissionDenied("direct upload is only for local storage")
    file, viewer = live_file(request, pk)
    upload = request.FILES.get("file")
    if upload is None:
        return _error("Der kom ingen fil med.")
    try:
        services.check_version(
            viewer, file, upload.size or 0, ignore_stale=bool(request.POST.get("ignore_stale"))
        )
    except services.Stale as exc:
        return _error(" ".join(exc.messages), 409, stale=True)
    except ValidationError as exc:
        return _error(" ".join(exc.messages))
    key = services.new_key()
    services.store_local(key, upload)
    services.add_version(
        viewer,
        file,
        key=key,
        size=upload.size or 0,
        content_type=upload.content_type or "application/octet-stream",
        resident=current_resident(request),
    )
    return JsonResponse({"ok": True, "name": file.name, "replaced": True}, status=201)
