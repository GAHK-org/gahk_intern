"""The bytes a file's page shows: a PDF, video or sound served inline, and an image's viewer copy
and thumbnail (drive/preview.py). Access is checked on every request, as for a download, and what
the bucket is asked for lives for minutes.

Served inline only under a type chosen by the file's name (preview.INLINE_TYPES), never the one the
uploader's browser sent, and with nosniff: the local-disk path serves from the app's own origin.
"""

from pathlib import PurePosixPath

from django.http import FileResponse, Http404, HttpRequest, HttpResponseBase
from django.shortcuts import redirect
from django.views.decorators.clickjacking import xframe_options_sameorigin

from core.downloads import content_disposition
from core.media import REDIRECT_CACHE_CONTROL

from . import access, preview, services
from .views import live_file

# A preview's key never changes meaning: a new version is new bytes, and its URL carries the number.
CACHED_CACHE_CONTROL = "private, max-age=86400"


def _inline(key: str, name: str, content_type: str, *, cache_control: str = "") -> HttpResponseBase:
    store = services.storage()
    signed_url = getattr(store, "signed_url", None)
    if signed_url is not None:
        response: HttpResponseBase = redirect(
            signed_url(
                key,
                parameters={
                    "ResponseContentDisposition": content_disposition(name, inline=True),
                    "ResponseContentType": content_type,
                },
            )
        )
        if cache_control:
            response.headers["Cache-Control"] = REDIRECT_CACHE_CONTROL
    else:
        response = FileResponse(store.open(key, "rb"), content_type=content_type)
        response["Content-Disposition"] = content_disposition(name, inline=True)
        if cache_control:
            response.headers["Cache-Control"] = cache_control
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Vary"] = "Cookie"
    return response


@access.access_required
@xframe_options_sameorigin  # the PDF is shown in a frame on the file's page
def inline(request: HttpRequest, pk: int) -> HttpResponseBase:
    """A PDF, video or sound, for the browser's own viewer or player."""
    file, _ = live_file(request, pk)
    content_type = preview.inline_type(file.name)
    if content_type is None:
        raise Http404("not shown in the browser")
    return _inline(file.latest_version().key, file.name, content_type)


def _preview_key(request: HttpRequest, pk: int, *, thumbnail: bool) -> HttpResponseBase:
    file, _ = live_file(request, pk)
    if preview.kind_of(file.name) != preview.Kind.IMAGE:
        raise Http404("not an image")
    ready = preview.ready(file.latest_version())
    if ready is None:
        raise Http404("no preview yet")
    key = ready.thumbnail_key if thumbnail else ready.viewer_key
    content_type = "image/gif" if key.endswith(".gif") else "image/jpeg"
    local = preview.cached(key)
    if local is not None:
        try:
            handle = local.open("rb")
        except OSError:
            pass  # evicted meanwhile; the bucket still has it
        else:
            response = FileResponse(handle, content_type=content_type)
            response.headers["Cache-Control"] = CACHED_CACHE_CONTROL
            response.headers["Vary"] = "Cookie"
            return response
    name = f"{PurePosixPath(file.name).stem}{PurePosixPath(key).suffix}"  # a HEIC is shown as a JPEG
    return _inline(key, name, content_type, cache_control=CACHED_CACHE_CONTROL)


@access.access_required
def image(request: HttpRequest, pk: int) -> HttpResponseBase:
    """An image's viewer copy, at most 1600 px."""
    return _preview_key(request, pk, thumbnail=False)


@access.access_required
def thumbnail(request: HttpRequest, pk: int) -> HttpResponseBase:
    return _preview_key(request, pk, thumbnail=True)
