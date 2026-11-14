"""Middleware of the core app."""

from collections.abc import Callable
from http.cookies import Morsel

from django.conf import settings
from django.core.exceptions import DisallowedHost, PermissionDenied
from django.http import HttpRequest, HttpResponseRedirect
from django.http.request import split_domain_port
from django.http.response import HttpResponseBase
from django.urls import reverse

from residents.permissions import is_kitchen

from .storage import request_host

# Where the kitchen account may go: an ALLOWLIST, because the intranet is the whole site — at the root
# of intern.gahk.dk there is no "/intern/" prefix left to gate by, and anything not named here
# (alumneliste, profiles, site-admin, Django admin, photo album, …) answers 403.
KITCHEN_ALLOWED_PREFIXES = (
    "/den-hurtige/",
    "/opslagstavle/",
    "/reparationer/",
    "/notifikationer/",
    # Auth: login, logout, password change/reset (residents.urls). NOT the rest of /admin/, which
    # is site-admin.
    "/admin/login",
    "/admin/logout",
    "/admin/password",
    "/admin/reset/",
    "/admin/preview",  # so an admin previewing as the kitchen can end it
    # Plumbing every allowed page needs: assets, Den Hurtige's images, the service worker, live
    # updates, health probes and calendar feeds (token-authenticated, no session involved).
    "/static/",
    "/media/",
    "/ws/",
    "/kalender/",
    # The old prefixes only redirect to the root, where the gate looks again.
    "/intern/",
    "/nyintern/",
)
# /install is public anyway (how to install the app), and the kitchen tablet may itself carry the old
# app from gahk.dk, whose notice links there: a 403 only for the kitchen account would make no sense.
KITCHEN_ALLOWED_PATHS = ("/sw.js", "/healthz", "/health", "/intern", "/nyintern", "/install")


def kitchen_home() -> str:
    """Den Hurtige's kitchen channel — where the kitchen account lands."""
    from den_hurtige import channels

    return channels.BY_SLUG[channels.KITCHEN_CHANNEL].url


def kitchen_may_open(path: str) -> bool:
    return path in KITCHEN_ALLOWED_PATHS or path.startswith(KITCHEN_ALLOWED_PREFIXES)


class PresignForRequestHost:
    """In DEBUG, let presigned URLs follow the host the page was opened on (core.storage.presigning_endpoint).

    So the site can be tried from a phone on the same network as the developer's machine. Does
    nothing with DEBUG off: production's public endpoint is the bucket's real host.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if not settings.DEBUG:
            return self.get_response(request)
        try:
            host = split_domain_port(request.get_host())[0]
        except DisallowedHost:
            return self.get_response(request)
        token = request_host.set(host)
        try:
            return self.get_response(request)
        finally:
            request_host.reset(token)


class KitchenAccountGate:
    """Keep the kitchen account (residents.permissions.is_kitchen) to its few pages.

    The rest of the site only checks login or roles, so without this it would see everything a
    resident sees. Den Hurtige and Opslagstavlen narrow it further inside the apps.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if is_kitchen(request):
            path = request.path
            # The dashboard and Den Hurtige's bare URL (the default channel) both land on its home.
            if path in (reverse("dashboard"), reverse("den_hurtige:feed")):
                return HttpResponseRedirect(kitchen_home())
            if not kitchen_may_open(path):
                raise PermissionDenied
        return self.get_response(request)


# The cookie names and domain this site used while it lived at gahk.dk/intern/ (see
# settings.SESSION_COOKIE_NAME). A browser that logged in back then still holds a ".gahk.dk"
# `sessionid`, sends it to intern.gahk.dk AND to the public site's webhost, and keeps it for up to two
# weeks. Nothing reads it any more; ExpireLegacyCookies makes the browser drop it.
LEGACY_COOKIE_NAMES = ("sessionid", "csrftoken")
LEGACY_COOKIE_DOMAIN = ".gahk.dk"
_EXPIRED = "Thu, 01 Jan 1970 00:00:00 GMT"


def _expired_morsel(name: str, domain: str | None, secure: bool) -> Morsel[str]:
    morsel: Morsel[str] = Morsel()
    morsel.set(name, "", '""')
    morsel["expires"] = _EXPIRED
    morsel["max-age"] = 0
    morsel["path"] = "/"
    morsel["samesite"] = "Lax"
    if domain:
        morsel["domain"] = domain
    if secure:
        morsel["secure"] = True
    return morsel


class ExpireLegacyCookies:
    """Delete the pre-move `sessionid`/`csrftoken` whenever a request still carries one.

    The browser alone knows which variant it holds — the ".gahk.dk" domain cookie of production, or a
    host-only one (development, or a stray) — and a deletion only matches the variant with the same
    Domain, so both are expired. Two Set-Cookie headers for one name do not fit HttpResponse.cookies,
    a SimpleCookie keyed by name, so each Morsel goes in under its own dict key; the handlers write
    every Morsel's own output, whose name is the real one.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        current = {settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME}
        for name in LEGACY_COOKIE_NAMES:
            if name in request.COOKIES and name not in current:
                for domain in (LEGACY_COOKIE_DOMAIN, None):
                    key = f"{name} (legacy {domain or 'host-only'})"
                    response.cookies[key] = _expired_morsel(name, domain, request.is_secure())
        return response
