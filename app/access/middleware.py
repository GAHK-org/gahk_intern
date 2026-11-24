"""The gate: every request needs its page's feature, at view for a request that changes nothing and
at use for one that may."""

from collections.abc import Callable

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.urls import reverse

from .api import FRAGMENT_ATTRIBUTE, audit_denied
from .features import ANONYMOUS, Feature, Grade, feature_of
from .permissions import SAFE_METHODS
from .roles import request_permissions

# Where an account without the dashboard lands: its first feature in this order.
HOMES = (
    (Feature.DEN_HURTIGE, "den_hurtige:feed"),
    (Feature.OPSLAGSTAVLE, "opslagstavle:board"),
    (Feature.EVENTS, "events:index"),
    (Feature.PHOTO_ALBUM, "photo_album:index"),
    (Feature.STAMTREE, "stamtree"),
)


class AccessGate:
    """Decided on the view a URL resolves to, never on the path: a path that is no page is the usual
    404, and a view in no feature is refused to everyone (a test keeps there from being one)."""

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
        feature = feature_of(view_func)
        if feature in ANONYMOUS:
            return None
        quiet = getattr(view_func, FRAGMENT_ATTRIBUTE, False)
        if not request.user.is_authenticated:
            # Django's admin has a login page of its own, and asks for it itself.
            if feature == Feature.DJANGO_ADMIN:
                return None
            # A page's own poll (htmx) would swap the login page into the middle of it: nothing
            # changes instead, and the next full page load asks for the login.
            if quiet or (request.headers.get("HX-Request") and request.method in SAFE_METHODS):
                return HttpResponse(status=204)
            return redirect_to_login(request.get_full_path())
        permissions = request_permissions(request)
        method = request.method or "GET"
        if permissions.allows_request(feature, method):
            return None
        if quiet:
            return HttpResponse(status=204)
        name = request.resolver_match.view_name if request.resolver_match else ""
        if name == "dashboard":
            home = next((url for f, url in HOMES if permissions.grade(f) >= Grade.VIEW), None)
            if home is None:
                return render(request, "no_access.html", status=403)
            return HttpResponseRedirect(reverse(home))
        audit_denied(request, f"{feature}.{'view' if method in SAFE_METHODS else 'use'}")
        raise PermissionDenied
