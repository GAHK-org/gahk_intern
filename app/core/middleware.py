"""Middleware of the core app."""

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import DisallowedHost, PermissionDenied
from django.http import HttpRequest, HttpResponseRedirect
from django.http.request import split_domain_port
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.urls import reverse

from residents.access_levels import AccessLevel, allows, feature_of, home_url_name, request_level

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


class AccessLevelGate:
    """Keep each access level to the features the table gives it (residents.access_levels).

    Decided on the view a URL resolves to, never on the path: a path that is no page is the usual
    404, and a view that is in no feature is for residents only, so a page nobody thought about is
    closed rather than open. The features narrow it further inside (Den Hurtige's channels for the
    kitchen, Opslagstavlen's own posts).
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
        level = request_level(request)
        if level == AccessLevel.RESIDENT:
            return None
        name = request.resolver_match.view_name if request.resolver_match else ""
        # The kitchen lands on its channel: from the dashboard, and from Den Hurtige's bare address,
        # which would otherwise show the default channel it may not read.
        if level == AccessLevel.KITCHEN and name in ("dashboard", "den_hurtige:feed"):
            return HttpResponseRedirect(kitchen_home())
        feature = feature_of(view_func)
        if allows(level, feature, request.method or "GET"):
            return None
        if name == "dashboard":
            home = home_url_name(level)
            if home is None:
                return render(request, "no_access.html", status=403)
            return HttpResponseRedirect(reverse(home))
        raise PermissionDenied
