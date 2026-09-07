"""Browsing the archive, and getting a file out of it.

Read-only for now: this slice replaces Dropbox for *reading*, which is what most of the kollegium
does with it. Uploading lands next, on the same access rules.

Every view starts from `access.visible_folders` / `access.visible_files`. Nothing here filters by id
first and checks permission afterwards - that ordering is how a 404 turns into a confirmation that
something exists.
"""

import json
import mimetypes
from typing import BinaryIO, cast

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import FileResponse, Http404, HttpRequest, HttpResponseRedirect, JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, uploads
from .models import ArchiveFile, ArchiveFolder, object_key, thumbnail_key
from .storage import LocalArchiveStore, get_store


@access.access_required
def browse(request: HttpRequest, pk: int | None = None) -> HttpResponseBase:
    """A folder's contents, or the roots when `pk` is None.

    Subfolders and files are fetched from the visible-* querysets rather than from `folder.children`
    and `folder.files`: the reverse accessors know nothing about embedsgrupper, and a private
    subfolder inside a public parent is exactly the case that has to not appear.
    """
    resident = current_resident(request)
    folders = access.visible_folders(resident)

    folder: ArchiveFolder | None = None
    if pk is not None:
        folder = folders.filter(pk=pk).select_related("parent", "effective_workgroup").first()
        if folder is None:
            # 404 rather than 403: whether a folder with this id exists is the thing being hidden.
            raise Http404("no such folder")

    subfolders = folders.filter(parent=folder).select_related("workgroup")
    files = (
        access.visible_files(resident).filter(folder=folder).select_related("uploaded_by")
        if folder is not None
        else ArchiveFile.objects.none()
    )

    return render(
        request,
        "arkiv/browse.html",
        {
            "folder": folder,
            "ancestors": folder.ancestors() if folder else [],
            "subfolders": subfolders,
            "files": files,
            "can_manage_roots": access.can_manage_roots(request),
            # Writing follows reading (access.can_write), so this is true for any folder the
            # resident can see - including the shared Billeder root, which is the point.
            "can_write": folder is not None and access.can_write(folder, request),
            "limited_rollout": access.is_limited(),
        },
    )


@access.access_required
def download(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Hand over one file.

    Routed through Django rather than putting a presigned URL in the page, deliberately - see
    arkiv/storage.py. Access is re-checked here on every request, so a link forwarded to a resident
    outside the embedsgruppe gives them a 404 rather than the document.

    Two branches, matching core.media.serve_media: redirect to a short-lived presigned URL when the
    bytes are in the bucket, stream them when they are on local disk (dev, CI). A path that only
    works in production is a path no test covers.
    """
    resident = current_resident(request)
    file = access.visible_files(resident).filter(pk=pk).select_related("folder").first()
    if file is None:
        raise Http404("no such file")

    store = get_store()
    url = store.download_url(file.key, filename=file.name, content_type=file.content_type)
    if url is not None:
        return HttpResponseRedirect(url)

    if isinstance(store, LocalArchiveStore):
        path = store.path(file.key)
        if not path.is_file():
            # The row survives its object only through an operator mistake, but a 500 here would say
            # "broken app" when the truth is "missing object" - which is what audit_arkiv is for.
            raise Http404("the object behind this row is missing")
        return FileResponse(path.open("rb"), as_attachment=True, filename=file.name)

    raise Http404("no object store configured")


# --- upload -----------------------------------------------------------------------------------------
#
# Two steps, so the database never holds a row for bytes that never arrived. See arkiv/uploads.py for
# the whole argument; what lives here is the access checking, which must not be skipped on either
# half. `begin` proves the resident may write to the folder, and `commit` proves it again - a client
# can call commit directly, and the folder it names is the only thing establishing permission.


def _writable_folder(request: HttpRequest, pk: int) -> ArchiveFolder:
    """The folder, or the right refusal. Never returns one the caller may not write to."""
    resident = current_resident(request)
    folder = access.visible_folders(resident).filter(pk=pk).first()
    if folder is None:
        # 404, not 403: whether a folder with this id exists is the thing being hidden.
        raise Http404("no such folder")
    if not access.can_write(folder, request):
        raise PermissionDenied
    return folder


def _thumbnail_plan(sha256: str, content_type: str) -> dict[str, object] | None:
    """How the browser should send a preview, or None if this file should not have one."""
    if not content_type.startswith("image/"):
        return None
    policy = uploads.presigned_thumbnail_post(sha256)
    if policy is None:
        return {"mode": "direct"}
    return {"mode": "s3", "url": policy["url"], "fields": policy["fields"]}


@access.access_required
@require_POST
def upload_begin(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Check access, then hand back either a bucket policy or "post it here instead"."""
    folder = _writable_folder(request, pk)

    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Ugyldig foresp\u00f8rgsel."}, status=400)

    sha256 = str(payload.get("sha256", "")).lower()
    name = str(payload.get("name", "")).strip()
    size = int(payload.get("size", 0) or 0)
    content_type = str(payload.get("content_type", "") or "")

    if not uploads.valid_hash(sha256):
        return JsonResponse({"error": "Ugyldig kontrolsum."}, status=400)
    if not name:
        return JsonResponse({"error": "Filen mangler et navn."}, status=400)
    if size <= 0 or size > uploads.MAX_UPLOAD_BYTES:
        limit = uploads.MAX_UPLOAD_BYTES // (1024 * 1024 * 1024)
        return JsonResponse({"error": f"Filen er for stor (over {limit} GB)."}, status=400)
    if ArchiveFile.objects.alive().filter(folder=folder, name=name).exists():
        return JsonResponse({"error": "Der findes allerede en fil med det navn her."}, status=409)

    store = get_store()
    if store.exists(object_key(sha256)):
        # Somebody has already uploaded these exact bytes, here or in another folder. Nothing to
        # send: commit straight away. Content addressing paying for itself on the second copy of a
        # party photograph.
        # The bytes are there, but a preview might not be - an earlier upload from a browser that
        # could not decode the image, or an imported file. Offer the thumbnail slot regardless.
        thumb = None if uploads.stored_thumbnail(sha256) else _thumbnail_plan(sha256, content_type)
        return JsonResponse({"upload": None, "thumbnail": thumb, "already_stored": True})

    policy = uploads.presigned_post(sha256, content_type)
    thumb = _thumbnail_plan(sha256, content_type)
    if policy is None:
        # No bucket (dev, CI): the browser posts the file to upload_direct instead.
        return JsonResponse({"upload": {"mode": "direct"}, "thumbnail": thumb, "already_stored": False})
    return JsonResponse(
        {
            "upload": {"mode": "s3", "url": policy["url"], "fields": policy["fields"]},
            "thumbnail": thumb,
            "already_stored": False,
        }
    )


@access.access_required
@require_POST
def upload_direct(request: HttpRequest, pk: int) -> HttpResponseBase:
    """The no-bucket path: take the bytes through Django and put them in the local store.

    Only reachable when there is no object storage configured, which is dev and CI. Refusing
    outright when a bucket exists is deliberate - otherwise this becomes the quiet way a 2 GB video
    ends up going through gunicorn after all.
    """
    _writable_folder(request, pk)

    store = get_store()
    if not isinstance(store, LocalArchiveStore):
        raise PermissionDenied("direct upload is only for the local store")

    upload = request.FILES.get("file")
    sha256 = str(request.POST.get("sha256", "")).lower()
    is_thumb = request.POST.get("thumbnail") == "1"
    if upload is None or not uploads.valid_hash(sha256):
        return JsonResponse({"error": "Ugyldig upload."}, status=400)
    limit = uploads.MAX_THUMBNAIL_BYTES if is_thumb else uploads.MAX_UPLOAD_BYTES
    if (upload.size or 0) > limit:
        return JsonResponse({"error": "Filen er for stor."}, status=400)

    # .file is the underlying stream; UploadedFile itself is not a BinaryIO.
    key = thumbnail_key(sha256) if is_thumb else object_key(sha256)
    store.save(key, cast("BinaryIO", upload.file))
    return JsonResponse({"ok": True})


@access.access_required
@require_POST
def upload_commit(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Create the row, but only for bytes the store actually has.

    The HEAD is the real check. Everything the client said until now - the size, the content type,
    the hash - was its word; this is the store's.
    """
    folder = _writable_folder(request, pk)

    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Ugyldig foresp\u00f8rgsel."}, status=400)

    sha256 = str(payload.get("sha256", "")).lower()
    name = str(payload.get("name", "")).strip()
    if not uploads.valid_hash(sha256) or not name:
        return JsonResponse({"error": "Ugyldig upload."}, status=400)

    stored = uploads.stored_object(sha256)
    if stored is None:
        # The upload never landed. No row: a row pointing at nothing is a broken file in a listing
        # with no explanation, which is worse than an object nobody references.
        return JsonResponse({"error": "Filen n\u00e5ede ikke frem. Pr\u00f8v igen."}, status=409)

    size, stored_type = stored
    try:
        file = ArchiveFile.objects.create(
            folder=folder,
            name=name,
            sha256=sha256,
            size=size,
            content_type=stored_type or mimetypes.guess_type(name)[0] or "",
            uploaded_by=current_resident(request),
            # Asked of the store, not taken from the client: a flag set on the client's word would
            # render a broken <img> for every file whose preview silently failed to upload.
            has_thumbnail=uploads.stored_thumbnail(sha256),
        )
    except IntegrityError:
        # Two tabs, or a double-tap on a slow connection. The partial unique index caught it.
        return JsonResponse({"error": "Der findes allerede en fil med det navn her."}, status=409)

    return JsonResponse({"ok": True, "pk": file.pk, "name": file.name, "size": file.size})


# --- folders and deletion ---------------------------------------------------------------------------
#
# Plain POST forms and a redirect, not fetch: there is no partial to swap, the result is a page the
# resident wants in their history, and it works with JavaScript off. The upload path needs fetch
# because the bytes bypass Django; these two do not.

# Long enough for "Sommerfest 2026 - raw fra Jonas' telefon", short enough to fit the column.
MAX_FOLDER_NAME = 120

# A week, and `private` because a preview of a Regnskabsgruppen document is as confidential as the
# document. Safe to cache this long only because the URL is content-addressed: new bytes, new key.
THUMBNAIL_CACHE_CONTROL = "private, max-age=604800"


@access.access_required
@require_POST
def folder_create(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Make a subfolder inside `pk`.

    Only ever a SUBfolder: roots are the kollegium's filing system and belong to Inspektionen
    (access.can_manage_roots), while everything below one is free, because needing a ticket to make
    a folder for this year's fest is how an archive turns back into a chat thread full of
    attachments.

    The new folder inherits its parent's embedsgruppe through ArchiveFolder.save(), so a subfolder of
    a gated root is gated from the moment it exists - there is no window in which it is public.
    """
    parent = _writable_folder(request, pk)
    name = (request.POST.get("name") or "").strip()

    if not name:
        messages.error(request, "Mappen skal have et navn.")
    elif len(name) > MAX_FOLDER_NAME:
        messages.error(request, f"Navnet er for langt (over {MAX_FOLDER_NAME} tegn).")
    elif "/" in name or "\\" in name:
        # Not a security boundary - the name never reaches a key, which is a hash - but a slash in a
        # folder name reads as a path that is not one, in a breadcrumb built from real parents.
        messages.error(request, "Mappenavne må ikke indeholde skråstreg.")
    elif ArchiveFolder.objects.alive().filter(parent=parent, name=name).exists():
        messages.error(request, "Der findes allerede en mappe med det navn her.")
    else:
        try:
            ArchiveFolder.objects.create(parent=parent, name=name, created_by=current_resident(request))
        except IntegrityError:
            # Two people, same name, same second. The partial unique index is the real arbiter.
            messages.error(request, "Der findes allerede en mappe med det navn her.")
        else:
            messages.success(request, f"Mappen \u201e{name}\u201d er oprettet.")

    return redirect("arkiv:folder", pk=parent.pk)


@access.access_required
@require_POST
def file_delete(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Remove a file from the listing. The bytes stay.

    Soft, and attributed: `services.unreferenced_keys` counts a soft-deleted row as a reference, so
    the object is still there for an admin to restore by clearing `deleted_at`. That is what makes
    it reasonable to let anyone who can write to the folder do this.
    """
    resident = current_resident(request)
    file = access.visible_files(resident).filter(pk=pk).select_related("folder").first()
    if file is None:
        raise Http404("no such file")
    if not access.can_delete_file(file, request):
        raise PermissionDenied

    folder_pk = file.folder_id
    file.soft_delete(by=resident)
    messages.success(
        request, f"\u201e{file.name}\u201d er fjernet. Bed en administrator hvis den skal tilbage."
    )
    return redirect("arkiv:folder", pk=folder_pk)


@access.access_required
def thumbnail(request: HttpRequest, pk: int) -> HttpResponseBase:
    """The grid preview for a file. Same access rules as the file itself.

    CACHED HARD, and content addressing is what makes that safe: the key is the hash of the
    original, so different bytes are a different URL. A preview can therefore never go stale, and
    the browser is told so - which matters on a folder of two hundred photographs, where the
    alternative is two hundred revalidations every time somebody opens it.
    """
    resident = current_resident(request)
    file = access.visible_files(resident).filter(pk=pk, has_thumbnail=True).first()
    if file is None:
        raise Http404("no such preview")

    store = get_store()
    url = store.download_url(file.thumb_key, filename=file.name, content_type="image/jpeg")
    if url is not None:
        redirect_to = HttpResponseRedirect(url)
        redirect_to.headers["Cache-Control"] = THUMBNAIL_CACHE_CONTROL
        redirect_to.headers["Vary"] = "Cookie"
        return redirect_to

    if isinstance(store, LocalArchiveStore):
        path = store.path(file.thumb_key)
        if not path.is_file():
            raise Http404("the preview behind this row is missing")
        response = FileResponse(path.open("rb"), content_type="image/jpeg")
        response.headers["Cache-Control"] = THUMBNAIL_CACHE_CONTROL
        return response

    raise Http404("no object store configured")
