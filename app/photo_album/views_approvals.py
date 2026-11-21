"""Fotogruppen's approval pages: what residents have waiting, and one request at a time
(photo_album.approvals)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from residents.permissions import current_resident

from . import access, approvals, merge
from .models import Album, normalize_name
from .views import album_folder_options

NOTE_MAX_LENGTH = 300


def _ids(request: HttpRequest, name: str) -> set[int]:
    return {int(value) for value in request.POST.getlist(name) if value.isdecimal()}


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


@login_required
def approvals_list(request: HttpRequest) -> HttpResponse:
    if not access.can_approve(request):
        raise PermissionDenied
    return render(request, "photo_album/approvals.html", {"requests": approvals.pending_requests()})


@login_required
def approval(request: HttpRequest, album_pk: int, uploader_pk: int) -> HttpResponse:
    """One request: every item shown, those picked rejected, the rest approved — and for each new
    album, its name (and folder), or the album it is merged into instead."""
    if not access.can_approve(request):
        raise PermissionDenied
    pending = approvals.find(album_pk, uploader_pk)
    if pending is None:
        messages.info(request, "Det er allerede afgjort.")
        return redirect("photo_album:approvals")
    posted = request.POST if request.method == "POST" else None
    if posted is not None:
        try:
            return _decide(request, pending)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
    groups: list[dict[str, object]] = []
    for item in pending.media:
        label = " › ".join(item.album_path)  # type: ignore[attr-defined]
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "items": []})
        groups[-1]["items"].append(item)  # type: ignore[attr-defined]
    return render(
        request,
        "photo_album/approval.html",
        {
            "pending": pending,
            "groups": groups,
            "several_albums": len(groups) > 1,
            "album_forms": [_album_form(pending, top, posted) for top in pending.tops],
            "rejected": _ids(request, "reject") if posted else set(),
            "note": posted.get("note", "") if posted else "",
            "folder_options": album_folder_options(),
        },
    )


def _album_form(pending: approvals.Request, top: Album, posted: "QueryDict | None") -> dict[str, object]:
    """What the page offers for one new album: keep it, under a name (a root album: in a folder), or
    merge it into an album of the folder chosen. An approved sibling of its name is the merge it
    starts on: the new album cannot keep that name."""
    siblings = (
        Album.objects.filter(parent_id=top.parent_id)
        if top.parent_id is not None
        else Album.objects.filter(parent__isnull=True, folder=top.folder)
    )
    clash = siblings.filter(pending=False, name__iexact=top.name).first()
    pk = top.pk
    if posted is not None:
        folder = normalize_name(posted.get(f"folder_{pk}", "")) or top.folder
        action = posted.get(f"album_action_{pk}", "keep")
        name = posted.get(f"name_{pk}", top.name)
        target = posted.get(f"merge_target_{pk}", "")
    else:
        folder, name = top.folder, top.name
        action = "merge" if clash is not None else "keep"
        target = str(clash.pk) if clash is not None else ""
    return {
        "album": top,
        "is_root": top.parent_id is None,
        "inside": [album for album in pending.new_albums if album.path.startswith(top.path) and album != top],
        "clash": clash,
        "folder": folder,
        "action": action,
        "name": name,
        "merge_target": target,
        "merge_options": merge.folder_albums(folder, excluding=top),
        "failed_merge": next((job for job in pending.failed_merges if job.source_id == pk), None),
    }


def _decide(request: HttpRequest, pending: approvals.Request) -> HttpResponse:
    by = current_resident(request)
    shown = _ids(request, "shown")
    shown_albums = _ids(request, "album")
    note = request.POST.get("note", "").strip()[:NOTE_MAX_LENGTH]
    action = request.POST.get("action", "")
    if action == "reject":
        decision = approvals.reject(pending, by=by, shown=shown, shown_albums=shown_albums, note=note)
        messages.success(
            request,
            f"{_count(decision.rejected, 'upload', 'uploads')} er afvist og ligger i papirkurven i 30 dage.",
        )
    elif action == "approve":
        choices: dict[int, approvals.AlbumChoice] = {}
        for top in pending.tops:
            if request.POST.get(f"album_action_{top.pk}") == "merge":
                target = request.POST.get(f"merge_target_{top.pk}", "")
                into = (
                    Album.objects.filter(pk=int(target), pending=False).first()
                    if target.isdecimal()
                    else None
                )
                if into is None:
                    raise ValidationError(f"Vælg det album, {top.name} skal flettes ind i.")
                choices[top.pk] = approvals.AlbumChoice(merge_into=into)
            else:
                choices[top.pk] = approvals.AlbumChoice(
                    name=request.POST.get(f"name_{top.pk}", ""),
                    folder=request.POST.get(f"folder_{top.pk}", ""),
                )
        decision = approvals.approve(
            pending,
            by=by,
            shown=shown,
            rejected=_ids(request, "reject"),
            shown_albums=shown_albums,
            choices=choices,
            note=note,
        )
        message = f"{_count(decision.approved, 'upload', 'uploads')} er godkendt"
        message += f" og {decision.rejected} afvist." if decision.rejected else "."
        for old_name, into, _ in decision.merges:
            message += (
                f" {old_name} flettes ind i {merge.label(into)} ({into.folder}); det, der er godkendt i "
                "det, bliver synligt, når fletningen er færdig."
            )
        messages.success(request, message)
    else:
        raise ValidationError("Vælg Godkend eller Afvis.")
    if decision.left:
        messages.info(
            request,
            f"{_count(decision.left, 'upload', 'uploads')} kom til, mens du kiggede, og venter stadig.",
        )
    return redirect("photo_album:approvals")


@login_required
@require_GET
def approval_albums(request: HttpRequest) -> JsonResponse:
    """The albums of a folder a new album can be merged into, for the approval page's choice."""
    if not access.can_approve(request):
        raise PermissionDenied
    excluding = None
    if (pk := request.GET.get("album", "")).isdecimal():
        excluding = Album.objects.filter(pk=int(pk)).first()
    candidates = merge.folder_albums(normalize_name(request.GET.get("folder", "")), excluding=excluding)
    return JsonResponse(
        {
            "albums": [
                {"id": candidate.album.pk, "label": " › ".join(candidate.path), "locked": candidate.locked}
                for candidate in candidates
            ]
        }
    )
