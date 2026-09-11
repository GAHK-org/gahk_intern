"""Browsing the archive, and getting a file out of it.

Read-only for now: this slice replaces Dropbox for *reading*, which is what most of the kollegium
does with it. Uploading lands next, on the same access rules.

Every view starts from `access.visible_folders` / `access.visible_files`. Nothing here filters by id
first and checks permission afterwards - that ordering is how a 404 turns into a confirmation that
something exists.
"""

import json
import mimetypes
import zipfile
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponseRedirect,
    JsonResponse,
    StreamingHttpResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.template.defaultfilters import filesizeformat
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, services, uploads
from .models import ArchiveFile, ArchiveFolder, object_key, selection_key
from .storage import ArchiveStore, LocalArchiveStore, content_disposition, get_store


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

    # Stage two of the delete lives here rather than on a page of its own: what has been removed
    # from THIS folder is only interesting while you are standing in it, and a house-wide list of
    # deleted files would be a second surface with its own access rules to get wrong.
    #
    # Only for writers, and only inside a folder. Queried unconditionally would cost every reader a
    # query to render nothing - `can_write` follows `can_read`, so this is most readers.
    can_write = folder is not None and access.can_write(folder, request)
    removed = (
        access.removed_files(resident)
        .filter(folder=folder)
        .select_related("deleted_by")
        .order_by("-deleted_at")
        if can_write
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
            "removed": removed,
            "can_manage_roots": access.can_manage_roots(request),
            # Writing follows reading (access.can_write), so this is true for any folder the
            # resident can see - including the shared Billeder root, which is the point.
            "can_write": can_write,
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


def _derived_plan(kind: str, sha256: str, content_type: str) -> dict[str, object] | None:
    """How the browser should send one derived size, or None if it should not send this one.

    None when the file is not an image, and also when the store already has that size - the bytes
    of a photograph somebody else uploaded last year are already there, and so are its thumbnail
    and preview, so there is nothing for this browser to make.
    """
    if not content_type.startswith("image/"):
        return None
    if uploads.stored_derived(kind, sha256):
        return None
    policy = uploads.presigned_derived_post(kind, sha256)
    if policy is None:
        return {"mode": "direct"}
    return {"mode": "s3", "url": policy["url"], "fields": policy["fields"]}


def _derived_plans(sha256: str, content_type: str) -> dict[str, object | None]:
    """Every derived size the browser should send for this file, by name."""
    return {kind: _derived_plan(kind, sha256, content_type) for kind in uploads.DERIVED_SIZES}


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
        # The bytes are there, but a derived size might not be - an earlier upload from a browser
        # that could not decode the image, or a file that arrived through the importer. Offer
        # whichever ones are genuinely missing; _derived_plan drops the rest.
        return JsonResponse(
            {
                "upload": None,
                "derived": _derived_plans(sha256, content_type),
                "already_stored": True,
            }
        )

    policy = uploads.presigned_post(sha256, content_type)
    derived = _derived_plans(sha256, content_type)
    if policy is None:
        # No bucket (dev, CI): the browser posts the file to upload_direct instead.
        return JsonResponse({"upload": {"mode": "direct"}, "derived": derived, "already_stored": False})
    return JsonResponse(
        {
            "upload": {"mode": "s3", "url": policy["url"], "fields": policy["fields"]},
            "derived": derived,
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
    # Which of the three things this is: the file itself, or one of the derived sizes. Named rather
    # than a boolean, because there are two derived sizes now and a third would be one more name.
    kind = str(request.POST.get("derived", ""))
    if upload is None or not uploads.valid_hash(sha256):
        return JsonResponse({"error": "Ugyldig upload."}, status=400)
    if kind and kind not in uploads.DERIVED_SIZES:
        return JsonResponse({"error": "Ukendt billedst\u00f8rrelse."}, status=400)

    limit = uploads.derived_limit(kind) if kind else uploads.MAX_UPLOAD_BYTES
    if (upload.size or 0) > limit:
        return JsonResponse({"error": "Filen er for stor."}, status=400)

    # .file is the underlying stream; UploadedFile itself is not a BinaryIO.
    key = uploads.derived_key(kind, sha256) if kind else object_key(sha256)
    store.save(cast("str", key), cast("BinaryIO", upload.file))
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
            # render a broken <img> for every file whose derived upload silently failed. Each size
            # is asked about separately, because a browser can perfectly well manage one and not
            # the other - and the viewer falls back to the original when the preview is missing.
            has_thumbnail=uploads.stored_derived("thumbnail", sha256),
            has_preview=uploads.stored_derived("preview", sha256),
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

# What one "Hent valgte" may carry. Both bound the BUILD, which since the zip became an object is
# the only part a worker waits for - reading the members out of the bucket and writing the archive
# back, inside fsn1. The recipient's connection is no longer in the picture at all, which is what
# these numbers used to be at the mercy of. The byte total also bounds the temporary file.
#
# Raising them is now a reasonable conversation, and a measurable one: the limit is our own internal
# bandwidth against `--timeout 60`, not a resident's hotel wifi. It was not measurable before.
MAX_SELECTED_FILES = 200
MAX_SELECTED_BYTES = 500 * 1024 * 1024

# Below this a zip is assembled in memory; above it SpooledTemporaryFile rolls over to disk. Sized
# so an ordinary selection of a few dozen photographs never touches the filesystem, while the cap
# above bounds what the largest one can occupy there.
ZIP_SPOOL_BYTES = 32 * 1024 * 1024


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
            # Wrapped for the same reason file_restore is: an IntegrityError caught outside a
            # savepoint poisons the transaction, and the messages.error() below is a query.
            with transaction.atomic():
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
    messages.success(request, f"\u201e{file.name}\u201d er fjernet. Den kan gendannes herunder.")
    return redirect("arkiv:folder", pk=folder_pk)


def _removed_file_or_404(request: HttpRequest, pk: int) -> ArchiveFile:
    """A soft-deleted file this resident may act on, or 404.

    Both stage-two views reach a row that `visible_files` deliberately cannot see, so they go
    through `access.removed_files` - the same `visible_folders` subquery, asked about the other side
    of `deleted_at`. 404 and never 403 for one outside it, because whether a file exists in a folder
    you cannot open is the thing Arkiv hides.
    """
    file = access.removed_files(current_resident(request)).filter(pk=pk).select_related("folder").first()
    if file is None:
        raise Http404("no such file")
    return file


@access.access_required
@require_POST
def file_restore(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Undo a removal. The other half of what makes stage two safe to hand to everybody."""
    file = _removed_file_or_404(request, pk)
    if not access.can_delete_file(file, request):
        raise PermissionDenied

    folder_pk = file.folder_id
    try:
        # A SAVEPOINT, not a bare try. Catching IntegrityError without one leaves the surrounding
        # transaction broken, so the very next query -- writing the message below -- raises
        # TransactionManagementError instead. That is invisible under this project's autocommit
        # settings and immediate under a test's transaction, which is exactly the way round that
        # gets a bug shipped: see test_restoring_over_a_reused_name_is_refused_not_silently_renamed.
        with transaction.atomic():
            file.restore()
    except IntegrityError:
        # `uniq_file_name_per_folder` covers live rows only, so a new file has taken the name since.
        # Reported rather than worked around: renaming somebody's upload to make room, or renaming
        # the restored file to "regnskab (1).pdf", are both worse than saying what happened.
        messages.error(
            request,
            f"Der findes allerede en fil med navnet \u201e{file.name}\u201d her. "
            "Omdøb eller fjern den først.",
        )
    else:
        messages.success(request, f"\u201e{file.name}\u201d er gendannet.")
    return redirect("arkiv:folder", pk=folder_pk)


@access.access_required
@require_POST
def file_purge(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Stage two: destroy the row and, if nothing else references them, the bytes.

    THIS IS THE ONE IRREVERSIBLE ACTION IN ARKIV. It is reachable only from the removed list, so a
    file has to be taken out of the listing first - see access.can_purge_file on why the order is
    what makes trusting every writer with this reasonable.

    The message distinguishes the two outcomes on purpose. `purge_file` returns False when another
    row still shares the hash, which happens whenever the same bytes were filed in two places, and
    "the file is gone but the bytes are still in use elsewhere" is not something a reader would
    otherwise guess from a listing that has one fewer row in it.
    """
    file = _removed_file_or_404(request, pk)
    if not access.can_purge_file(file, request):
        raise PermissionDenied

    folder_pk = file.folder_id
    name = file.name
    bytes_gone = services.purge_file(file)
    if bytes_gone:
        messages.success(request, f"\u201e{name}\u201d er slettet permanent.")
    else:
        messages.success(
            request,
            f"\u201e{name}\u201d er slettet permanent. Filens indhold ligger stadig i Arkivet, "
            "fordi den samme fil er gemt i en anden mappe.",
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


# --- the viewer, and taking several files at once ---------------------------------------------------


@access.access_required
def preview(request: HttpRequest, pk: int) -> HttpResponseBase:
    """The large image the viewer shows. Same access rules as the file itself.

    A THIRD SIZE, between the 40px row icon and a forty-megabyte original. Paging through a folder
    of originals is minutes of waiting and the one variable line on the Hetzner bill; paging through
    320px thumbnails blown up to fill a laptop is porridge. See `models.preview_key`.

    **Falls back to the original when there is no preview object**, which is not a detail: a photo
    uploaded through the browser gets a thumbnail made client-side and no preview, and stays that
    way until `make_arkiv_thumbnails` next sweeps. Without the fallback, every freshly uploaded
    photograph would be the one that breaks when you click it - the worst possible case to 404,
    because it is the one the uploader checks.

    Cached as hard as the thumbnail, and safe for the same reason: the key is the hash of the
    original, so different bytes are a different URL and a preview can never go stale.
    """
    resident = current_resident(request)
    file = access.visible_files(resident).filter(pk=pk).first()
    if file is None:
        raise Http404("no such file")

    key = file.preview_key if file.has_preview else file.key
    content_type = "image/jpeg" if file.has_preview else file.content_type

    store = get_store()
    url = store.download_url(key, filename=file.name, content_type=content_type)
    if url is not None:
        redirect_to = HttpResponseRedirect(url)
        redirect_to.headers["Cache-Control"] = THUMBNAIL_CACHE_CONTROL
        redirect_to.headers["Vary"] = "Cookie"
        return redirect_to

    if isinstance(store, LocalArchiveStore):
        path = store.path(key)
        if not path.is_file():
            raise Http404("the preview behind this row is missing")
        response = FileResponse(path.open("rb"), content_type=content_type or "image/jpeg")
        response.headers["Cache-Control"] = THUMBNAIL_CACHE_CONTROL
        return response

    raise Http404("no object store configured")


@access.access_required
@require_POST
def download_selected(request: HttpRequest, pk: int) -> HttpResponseBase:
    """Several files from one folder, as a zip BUILT INTO THE BUCKET and then redirected to.

    POST, not GET, and not because anything is modified: the selection is a list of ids that can
    run to a couple of hundred, which does not belong in a URL, and a GET would let a link in a chat
    thread start a multi-hundred-megabyte transfer for whoever clicked it.

    **The zip is an object, not a stream, and that is the whole design.** The first version streamed
    it straight to the browser, which quietly made this the only route in Arkiv that holds a gunicorn
    worker - and held it not for as long as the zip took to BUILD, but for as long as the recipient
    took to RECEIVE it. TCP backpressure: the server can only write as fast as the browser reads, so
    one resident on hotel wifi occupied one of three synchronous workers until they were done, and
    was killed at `--timeout 60` regardless, left holding a truncated archive.

    Building it into the bucket decouples all of that. The worker is busy only for the build, which
    is server-to-Hetzner traffic inside fsn1 - fast, free, and a number we can measure - and then it
    hands back a redirect, exactly like every other download here. Hetzner serves the bytes at
    whatever speed the resident has, and no worker is involved at all.

    **No job runner needed**, which is why this is worth doing now rather than "when we have Celery".
    The build is synchronous; all that changed is what the worker is waiting for.

    **Reused when it already exists.** `selection_key` names the object after the selection, so the
    second person to ask for the same files gets a redirect and no build. A changed selection is a
    changed key, so there is no stale cache and nothing to invalidate - see that docstring.

    Access is re-checked here rather than trusted from the page: the ids arrive from the client, so
    they are filtered through `visible_files` scoped to a folder the resident can already read. The
    key is then derived from THAT filtered list, so a selection is only ever named by files the
    asker may actually see.
    """
    resident = current_resident(request)
    folder = access.visible_folders(resident).filter(pk=pk).first()
    if folder is None:
        raise Http404("no such folder")

    try:
        ids = [int(raw) for raw in request.POST.getlist("ids")[:MAX_SELECTED_FILES]]
    except ValueError:
        raise Http404("bad selection") from None

    # Scoped to THIS folder as well as to the resident: the folder is what the access check above
    # established, so an id from somewhere else must not ride along on it.
    files = list(access.visible_files(resident).filter(folder=folder, pk__in=ids).order_by("name"))
    if not files:
        messages.error(request, "Vælg mindst én fil.")
        return redirect("arkiv:folder", pk=folder.pk)

    total = sum(file.size for file in files)
    if total > MAX_SELECTED_BYTES:
        messages.error(
            request,
            f"Det valgte fylder {filesizeformat(total)}. Grænsen for samlet download er "
            f"{filesizeformat(MAX_SELECTED_BYTES)} - vælg færre filer, eller hent de største "
            f"enkeltvis.",
        )
        return redirect("arkiv:folder", pk=folder.pk)

    store = get_store()
    key = selection_key((file.name, file.sha256) for file in files)
    if not store.exists(key):
        _build_zip(store, files, key)

    # The disposition lives on the URL, not on the object, so one cached zip can be handed to two
    # folders under two names.
    name = f"{folder.name}.zip"
    url = store.download_url(key, filename=name, content_type="application/zip")
    if url is not None:
        return HttpResponseRedirect(url)

    # No bucket (dev, CI): serve the object we just built. Same two-branch shape as every other
    # download here, and for the same reason - a path that only works in production is a path no
    # test covers.
    response = StreamingHttpResponse(store.chunks(key), content_type="application/zip")
    response.headers["Content-Disposition"] = content_disposition(name)
    return response


def _build_zip(store: ArchiveStore, files: list[ArchiveFile], key: str) -> None:
    """Assemble the zip and put it in the store under `key`.

    Written to a SpooledTemporaryFile rather than streamed, which is what let the hand-rolled
    non-seekable sink this used to need go away entirely: zipfile can seek back and patch its own
    local headers, so the result is an ordinary archive with no data descriptors. Small selections
    never touch the disk at all.

    `ZIP_STORED`, because the contents are JPEGs and video. Deflate would spend real CPU on the
    machine that is also serving the site to save a percent or two.

    A file whose object has vanished is skipped rather than fatal. The alternative is a truncated
    zip with no explanation, and a missing object is an operator problem (`audit_arkiv`), not
    something the resident downloading holiday photographs can act on.

    Half a build leaves NOTHING behind, which is what makes the `exists` check above trustworthy.
    The upload happens once the archive is complete, and boto3 either finishes it or leaves an
    incomplete multipart upload that the lifecycle rule aborts - there is no state in which this
    key holds a partial archive that the next request would then happily redirect somebody to.
    """
    with SpooledTemporaryFile(max_size=ZIP_SPOOL_BYTES) as tmp:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as archive:
            for file in files:
                if not store.exists(file.key):
                    continue
                with archive.open(file.name, "w") as entry:
                    for chunk in store.chunks(file.key):
                        entry.write(chunk)
        tmp.seek(0)
        store.save(key, cast("BinaryIO", tmp))
