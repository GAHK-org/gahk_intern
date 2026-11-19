"""Search, and zips: asking for one, waiting for it, and fetching it. The rules are in search.py and
zips.py."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.downloads import content_disposition
from residents.permissions import current_resident

from . import access, operations, search, services, zips
from .access import Level, Viewer
from .models import ZipDownload
from .views import _folder, _render, mark_files
from .views_items import _selection


@access.access_required
def search_page(request: HttpRequest) -> HttpResponse:
    viewer = Viewer.for_request(request)
    text = request.GET.get("q", "")
    filters = {name: request.GET.get(name, "") for name in ("space", "kind", "period")}
    results = search.search(viewer, text, **filters)
    mark_files([hit.file for hit in results.hits if hit.file])
    return _render(
        request,
        "drive/search.html",
        {
            "q": text,
            **filters,
            "results": results,
            "too_short": bool(text.strip()) and len(text.strip()) < search.MIN_LENGTH,
            "spaces": search.SPACES,
            "kinds": search.KINDS,
            "periods": search.periods(),
            "result_limit": search.RESULTS,
        },
    )


@access.access_required
@require_POST
def request_zip(request: HttpRequest, pk: int) -> HttpResponse:
    """The ticked items of a folder, or with nothing ticked the folder itself."""
    folder, viewer, _ = _folder(request, pk)
    selection = _selection(request, folder)
    name = folder.name
    if not selection:
        selection = operations.Selection(folder, folders=[folder])
    elif len(selection.folders) == 1 and not selection.files:
        name = selection.folders[0].name
    try:
        download = zips.request(viewer, selection, name=name, actor=current_resident(request))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("drive:folder", folder.pk)
    return redirect("drive:zip", download.token)


def _own(request: HttpRequest, token: str) -> ZipDownload:
    download = get_object_or_404(ZipDownload, token=token)
    if download.requested_by_id != current_resident(request).pk:
        raise Http404("not yours")
    return download


@access.access_required
def zip_page(request: HttpRequest, token: str) -> HttpResponse:
    download = _own(request, token)
    viewer = Viewer.for_request(request)
    folder = download.folder if download.folder and viewer.level(download.folder) >= Level.READ else None
    return _render(
        request,
        "drive/zip.html",
        {
            "download": download,
            "back": folder,
            "stalled": zips.stalled(download),
            "file_count": sum(1 for entry in download.entries if "key" in entry),
            "keep_hours": zips.ZIP_KEEP_HOURS,
        },
    )


@access.access_required
def fetch_zip(request: HttpRequest, token: str) -> HttpResponseBase:
    download = _own(request, token)
    if download.state != ZipDownload.State.READY:
        raise Http404("not ready")
    if not zips.still_readable(Viewer.for_request(request), download):
        messages.error(request, "Du kan ikke længere læse alt i zip'en. Bed om en ny.")
        return redirect("drive:zip", download.token)
    store = services.storage()
    signed_url = getattr(store, "signed_url", None)
    if signed_url is not None:
        return redirect(
            signed_url(
                download.archive_key,
                expire=5 * 60,
                parameters={"ResponseContentDisposition": content_disposition(download.name)},
            )
        )
    response = FileResponse(store.open(download.archive_key, "rb"), content_type="application/zip")
    response["Content-Disposition"] = content_disposition(download.name)
    return response
