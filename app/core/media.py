"""Serving /media/, whatever is actually holding the bytes.

/media/ is a permanent, app-owned URL space — see core/storage.py for why it can never move. This
module is the other half of that deal: the URL stays put, and this view resolves it against whatever
STORAGES["default"] happens to be.

Two branches, and both are load-bearing rather than a dev convenience:

  * object storage -> 302 to a short-lived presigned GET. The bytes come straight from Hetzner, so
    gunicorn never streams a file and a slow client cannot pin one of three sync workers.
  * anything else  -> stream it, which is what django.views.static.serve did before this existed.
    Dev and CI run with no S3 credentials at all, and prod ran this way until the migration; a
    single code path that only works when a bucket is configured would make the whole suite
    untestable and the rollback untested.

IT IS ALSO THE AUTHENTICATION BOUNDARY for uploads, which it did not used to be: /media/ was public
by URL, exactly as the legacy /public/ images were, so anyone who guessed
`/media/profile_pictures/IMG_1234.jpg` — a real, un-suffixed, un-dated name — could read a
resident's photograph. PUBLIC_PREFIXES is what that decision collapsed to; its derivation is
recorded there, because "which of these is the front page allowed to need" is the question a future
reader will actually have.
"""

import posixpath

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import SuspiciousOperation
from django.core.files.storage import storages
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponseNotModified,
    HttpResponseRedirect,
)
from django.http.response import HttpResponseBase
from django.utils.http import http_date
from django.views.static import was_modified_since

# How long the browser may reuse the redirect, and how long the signature it points at stays valid.
#
# THE ORDERING IS THE WHOLE POINT: REDIRECT_MAX_AGE must stay comfortably below PRESIGN_TTL, or a
# cached 302 outlives the URL it names and the image 403s from the bucket with nothing in our logs.
# The gap absorbs clock skew between us and Hetzner.
#
# Caching the redirect is not an optimisation, it is required. A 302 is not heuristically cacheable
# (RFC 9111 §4.2.2), so with no Cache-Control every image re-enters Django on every page load AND
# the signature differs each time, which also defeats the browser's cache of the bytes themselves —
# no caching at either layer.
PRESIGN_TTL = 3600
REDIRECT_MAX_AGE = 900

# `private`, never `public`: a shared cache must not hand one resident's presigned URL to another.
# Vary: Cookie is here for the day the auth gate lands — without it a 302-to-login cached for an
# anonymous visitor would be replayed to a logged-in resident.
REDIRECT_CACHE_CONTROL = f"private, max-age={REDIRECT_MAX_AGE}"


# The only upload prefix the logged-out public site needs.
#
# Derived from the templates rather than assumed, and re-derivable the same way: cms.CmsImage is the
# toolbar the CMS editors use, and its /media/cms/… URLs go into Page.body, NewsItem.body and
# Event.description, which cms/home.html, cms/page.html and cms/events_news.html render to anonymous
# visitors. `body_media` (cms.templatetags.cms_extras) rewrites only the LEGACY /public/… paths to
# /static/legacy/, so it never redirects these away from /media/.
#
# Everything else is reached from /intern/ only, and was checked one prefix at a time:
#   profile_pictures/  base.html avatar, residents' profile pages, alumneliste
#   roomimages/        værelsestjek
#   public/            relocate_media copies ONLY Product.image and RoomConditionScore.image legacy
#                      paths here — ølkælder and værelsestjek. Legacy CMS images are a different
#                      command (sync_cms_media) and land in static/legacy/, not here.
#   oel/               ølkælder, which lives under /intern/oelkaelder/
#   opslag/            opslagstavlen        quick_posts/, quick_comments/  Den Hurtige
#   begivenheder/      events
#
# Add a prefix here only after checking the same way. Adding one wrongly publishes it silently;
# omitting one wrongly breaks the front page loudly, which is the safer way round.
PUBLIC_PREFIXES = ("cms/",)


def _clean(path: str) -> str | None:
    """The storage name a request is really asking for, or None if it is not asking honestly.

    NORMALISING BEFORE THE PREFIX CHECK IS THE WHOLE POINT. `/media/cms/../profile_pictures/x.jpg`
    starts with "cms/" and resolves to a profile picture, and it does NOT trip the storage's
    safe_join guard, because collapsing those dots never escapes the media root — it just lands
    somewhere else inside it. Checking the raw path would be an authentication bypass, not a
    cosmetic issue.

    Backslashes are refused outright rather than normalised: posixpath does not treat them as
    separators, but a filesystem backend on Windows does, so they are a second spelling of the same
    trick and nothing legitimate produces one.
    """
    if "\\" in path:
        return None
    # Leading "/" so normpath can never produce a path above the root; "" and "." mean the root
    # itself, which is not a file.
    name = posixpath.normpath("/" + path).lstrip("/")
    if not name or name == "." or name.startswith("../"):
        return None
    return name


def serve_media(request: HttpRequest, path: str) -> HttpResponseBase:
    """Resolve `path` under MEDIA_ROOT / the media bucket, for someone allowed to see it."""
    name = _clean(path)
    if name is None:
        raise Http404("not a media path")

    if not name.startswith(PUBLIC_PREFIXES) and not request.user.is_authenticated:
        # redirect_to_login rather than 404: these URLs are opened directly often enough (a resident
        # following a link to a picture in an opslag on a machine where the session expired) that
        # landing on the login page and coming back is the useful answer. It carries no
        # Cache-Control, so the anonymous redirect is never stored and replayed to a logged-in
        # resident — the reason the authenticated branch below sets Vary: Cookie as well.
        return redirect_to_login(request.get_full_path())

    path = name
    storage = storages["default"]

    signer = getattr(storage, "signed_url", None)
    if signer is not None:
        try:
            target = signer(path, expire=PRESIGN_TTL)
        except SuspiciousOperation as exc:
            # `..` climbing out of the media prefix. django-storages raises the PARENT
            # SuspiciousOperation, not SuspiciousFileOperation, so catching only the subclass
            # would let it through as a 400 plus a security-log entry. 404 rather than 400:
            # whether a path is outside the media root is not something a caller needs
            # confirmed.
            raise Http404("media path outside the storage root") from exc
        redirect = HttpResponseRedirect(target)
        redirect.headers["Cache-Control"] = REDIRECT_CACHE_CONTROL
        redirect.headers["Vary"] = "Cookie"
        return redirect

    # CONDITIONAL GET, and it is not an optimisation. django.views.static.serve - what this replaced -
    # answered If-Modified-Since with a 304, and dropping that made every image on a page re-download
    # in full on every load. On alumnelisten that is ~124 profile pictures of up to a megabyte each,
    # through a single-threaded dev server, per reload. The S3 branch above never gets here: the
    # bucket does its own revalidation behind the redirect.
    try:
        modified = storage.get_modified_time(path)
    except SuspiciousOperation as exc:
        raise Http404("media path outside the storage root") from exc
    except (FileNotFoundError, IsADirectoryError, PermissionError, ValueError, NotImplementedError):
        # A backend that cannot report an mtime just does not get to answer 304.
        modified = None

    if modified is not None:
        stamp = modified.timestamp()
        if not was_modified_since(request.META.get("HTTP_IF_MODIFIED_SINCE"), stamp):
            return HttpResponseNotModified()

    try:
        handle = storage.open(path)
    except SuspiciousOperation as exc:
        raise Http404("media path outside the storage root") from exc
    except (FileNotFoundError, IsADirectoryError, PermissionError, ValueError) as exc:
        # ValueError covers the empty path — GET /media/ — which reaches the storage as "".
        raise Http404("media file not found") from exc

    response: HttpResponseBase = FileResponse(handle)
    if modified is not None:
        # Last-Modified without a max-age, exactly as static.serve did: the browser revalidates and
        # gets a 304, so an image replaced in place during development is never served stale.
        response.headers["Last-Modified"] = http_date(modified.timestamp())
    return response
