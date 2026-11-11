"""Middleware of the core app."""

from collections.abc import Callable

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
KITCHEN_ALLOWED_PATHS = ("/sw.js", "/healthz", "/health", "/intern", "/nyintern")


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
