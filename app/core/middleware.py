"""Middleware of the core app."""

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import DisallowedHost, PermissionDenied
from django.http import HttpRequest, HttpResponseRedirect
from django.http.request import split_domain_port
from django.http.response import HttpResponseBase

from residents.permissions import AccountKind, account_kind, view_open_to

from .storage import request_host


def kitchen_home() -> str:
    """Den Hurtige's kitchen channel — where the kitchen account lands."""
    from den_hurtige import channels

    return channels.BY_SLUG[channels.KITCHEN_CHANNEL].url


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


class AccountKindGate:
    """Keep each kind of account (residents.permissions.AccountKind) to the views open to it.

    The rest of the site only checks login and roles, so without this the kitchen account would see
    everything a resident sees. A view is resident-only unless it is marked `open_to` another kind,
    so a new page is closed to them until somebody opens it on purpose. Decided on the view the URL
    resolves to, never on the path: a path that resolves to nothing is the usual 404.

    The features narrow it further inside (Den Hurtige's channels, Opslagstavlen's own posts).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., HttpResponseBase],
        view_args: tuple[object, ...],
        view_kwargs: dict[str, object],
    ) -> HttpResponseBase | None:
        if not request.user.is_authenticated:
            return None  # the view's own login check decides
        kind = account_kind(request)
        if kind == AccountKind.KITCHEN and request.resolver_match is not None:
            # The dashboard and Den Hurtige's bare URL (the default channel) both land on its home.
            if request.resolver_match.view_name in ("dashboard", "den_hurtige:feed"):
                return HttpResponseRedirect(kitchen_home())
        if not view_open_to(view_func, kind):
            raise PermissionDenied
        return None
