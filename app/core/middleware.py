"""Middleware of the core app."""

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import DisallowedHost, PermissionDenied
from django.http import HttpRequest, HttpResponseRedirect
from django.http.request import split_domain_port
from django.http.response import HttpResponseBase

from residents.permissions import is_kitchen

from .storage import request_host

# Where the kitchen account may go. Everything else in KITCHEN_GATED_PREFIXES answers 403; the
# public site stays reachable.
KITCHEN_ALLOWED_PREFIXES = (
    "/intern/den-hurtige/",
    "/intern/opslagstavle/",
    "/intern/reparationer/",
    "/intern/notifikationer/",
    "/intern/admin/",  # login, logout, password change/reset
    "/admin/preview",  # so an admin previewing as the kitchen can end it
)
KITCHEN_GATED_PREFIXES = ("/intern/", "/admin/", "/django-admin/", "/photo-album/")
KITCHEN_HOME = "/intern/den-hurtige/koekken/"


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
            if path in ("/intern/", "/intern/den-hurtige/"):
                return HttpResponseRedirect(KITCHEN_HOME)
            if path.startswith(KITCHEN_GATED_PREFIXES) and not path.startswith(KITCHEN_ALLOWED_PREFIXES):
                raise PermissionDenied
        return self.get_response(request)
