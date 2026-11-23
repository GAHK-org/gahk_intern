"""The gate that keeps each access level to its features."""

from collections.abc import Callable

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.urls import reverse

from core.middleware import kitchen_home

from .levels import request_level
from .policy import AccessLevel, allows, feature_of, home_url_name


class AccessLevelGate:
    """Keep each access level to the features the table gives it (access.policy).

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
