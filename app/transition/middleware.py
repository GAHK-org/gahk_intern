"""The two middlewares of the transition (see the package docstring). Both TEMPORARY.

PublicSiteHost, first of all: for a host in settings.TRANSITION_PUBLIC_HOSTS (gahk.dk, www.gahk.dk),
the old public site's URLconf, and gahk.dk's /health redirect (before core.health answers it).

CarryOverLegacySession, right after SessionMiddleware: on intern.gahk.dk, a browser with the old
code's session cookie and none of ours gets its session moved to a new key under our cookie; and
every session this code saves is marked, so `end_legacy_sessions` can tell the old ones apart.
"""

import logging
from collections.abc import Callable
from importlib import import_module
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.contrib.sessions.backends.base import SessionBase
from django.db import transaction
from django.http import HttpRequest, HttpResponsePermanentRedirect
from django.http.request import split_domain_port
from django.http.response import HttpResponseBase
from django.utils import timezone

from cms.middleware import EDITOR_HINT_COOKIE
from core.middleware import LEGACY_COOKIE_NAMES

logger = logging.getLogger(__name__)

PUBLIC_URLCONF = "transition.urls_public"

# The old code's session cookie: Django's default name, on Domain=.gahk.dk (so intern.gahk.dk gets
# it too). One of core.middleware.LEGACY_COOKIE_NAMES, which ExpireLegacyCookies deletes.
LEGACY_SESSION_COOKIE = "sessionid"

# In the data of every session the new code saves. A session without it was saved by the old code.
MARKER = "_intern_host"

# Absolute URLs in this module, and in the redirects of transition.views, keep these characters in a
# path as they are, as Apache does on gahk.dk; anything else is percent-encoded.
PATH_SAFE = "/:@!$&'()*+,;=~"


def is_public_host(request: HttpRequest) -> bool:
    """Whether the request is for one of TRANSITION_PUBLIC_HOSTS (by its Host header, port ignored).

    The raw header, not request.get_host(): this runs before anything checks ALLOWED_HOSTS, and it
    only compares the header with the configured names.
    """
    hosts = settings.TRANSITION_PUBLIC_HOSTS
    if not hosts:
        return False
    host, _port = split_domain_port(request.META.get("HTTP_HOST", ""))
    return host in hosts


def intern_url(path: str, query: str = "") -> str:
    """`path` (decoded) on intern.gahk.dk (settings.SITE_URL), with `query` (raw) kept."""
    target = settings.SITE_URL.rstrip("/") + "/" + quote(path.lstrip("/\\"), safe=PATH_SAFE)
    return f"{target}?{query}" if query else target


def permanent_redirect(url: str) -> HttpResponsePermanentRedirect:
    """A 308: the method and body are kept, as gahk.dk's redirects do (a POST stays a POST)."""
    response = HttpResponsePermanentRedirect(url)
    response.status_code = 308
    return response


class PublicSiteHost:
    """Serve the old public site for TRANSITION_PUBLIC_HOSTS; do nothing for any other host.

    For those hosts:
      * /health and /healthz go to intern.gahk.dk (308), as gahk.dk will send them. First in
        MIDDLEWARE for this, ahead of core.health, which answers them for every host.
      * The old ".gahk.dk" session and CSRF cookies and the editor hint are hidden from the rest of
        the request. Nothing on the public site reads them, and otherwise ExpireLegacyCookies would
        delete the old session cookie here — before the visitor reaches intern.gahk.dk, where
        CarryOverLegacySession needs it — and EditorHintCookie would delete an editor's hint.
      * Everything else is routed by transition.urls_public.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if not is_public_host(request):
            return self.get_response(request)
        path = request.path_info
        if path.rstrip("/") in ("/health", "/healthz"):
            return permanent_redirect(intern_url(path.rstrip("/"), request.META.get("QUERY_STRING", "")))
        for name in (*LEGACY_COOKIE_NAMES, EDITOR_HINT_COOKIE):
            request.COOKIES.pop(name, None)
        request.urlconf = PUBLIC_URLCONF
        return self.get_response(request)


# --------------------------------------------------------------------------------- sessions
def _session_store() -> type[SessionBase]:
    return import_module(settings.SESSION_ENGINE).SessionStore


def take_legacy_session(key: str) -> dict[str, Any] | None:
    """The data of the old code's session `key`, which is deleted; None when there is none to take.

    None for an unknown, expired or undecodable key, and for a session the NEW code saved (it has
    MARKER): under the old cookie name only an old session is accepted, never one of ours. That
    matters once gahk.dk is the webhost, which receives ".gahk.dk" cookies and could set one: a
    browser handed somebody's own new session key as `sessionid` must not be logged in with it. Such
    a session is left alone. The row is locked until it is gone, so two requests carrying the same
    old cookie cannot both take it.
    """
    store = _session_store()
    model = store.get_model_class()  # type: ignore[attr-defined]  # a database-backed engine
    with transaction.atomic():
        row = model.objects.select_for_update().filter(session_key=key).first()
        if row is None:
            return None
        data = store().decode(row.session_data)
        if data.get(MARKER):
            return None
        row.delete()
        if row.expire_date <= timezone.now() or not data:
            return None
    return data


def carry_over(key: str) -> SessionBase | None:
    """A new session (new key, saved, marked) holding what the old session `key` held, which is
    deleted; None when `key` names no old session to take (take_legacy_session)."""
    with transaction.atomic():
        data = take_legacy_session(key)
        if data is None:
            return None
        session = _session_store()()
        session.update(data)
        session[MARKER] = 1
        session.create()
    return session


class CarryOverLegacySession:
    """Keep a user of the old code logged in on intern.gahk.dk, and mark every session saved here.

    Right after SessionMiddleware. On a request without our session cookie but with the old code's
    `sessionid` (and not for a public host, whose requests PublicSiteHost strips of it), the old
    session is moved to a new key (carry_over) and becomes this request's session. It is left
    modified, so SessionMiddleware saves it and sets our cookie exactly as it would for any new
    session (name, Domain, Path, Secure, HttpOnly, SameSite, Max-Age per SESSION_* settings).
    ExpireLegacyCookies, further out, deletes the old cookie on the same response.

    On the way out, a session that is about to be saved (modified, not emptied) gets MARKER if it
    lacks it: new logins, anonymous sessions, carried-over ones. An emptied one (a logout) is left
    empty, so SessionMiddleware still deletes its cookie.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        old_key = request.COOKIES.get(LEGACY_SESSION_COOKIE)
        if (
            old_key
            and settings.SESSION_COOKIE_NAME != LEGACY_SESSION_COOKIE
            and settings.SESSION_COOKIE_NAME not in request.COOKIES
            and not is_public_host(request)
        ):
            session = carry_over(old_key)
            if session is not None:
                logger.info("Carried an old session over to a new key")
                request.session = session
        response = self.get_response(request)
        session = getattr(request, "session", None)
        if session is not None and session.modified and not session.is_empty() and MARKER not in session:
            session[MARKER] = 1
        return response
