"""Response headers that narrow what a page may do, on top of Django's SecurityMiddleware.

  Permissions-Policy       the browser features no page here uses (camera, microphone, location, …)
                           are switched off, so script injected into a page cannot ask for them.
  Content-Security-Policy  settings.CONTENT_SECURITY_POLICY, plus frame-ancestors mirroring the
                           X-Frame-Options header the response already carries.

A view that sets its own Content-Security-Policy keeps it (the Hjemmeside preview sandboxes gahk.dk's
HTML and gives it a <base> of gahk.dk, which this policy's base-uri would refuse).

frame-ancestors follows X-Frame-Options rather than being fixed, because a browser that understands
frame-ancestors ignores X-Frame-Options altogether: a fixed 'none' would break a view marked
@xframe_options_sameorigin, and a fixed 'self' would quietly loosen every DENY. So this runs after
XFrameOptionsMiddleware (it is listed BEFORE it in MIDDLEWARE) and translates whatever that decided.
"""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase

FRAME_ANCESTORS = {"DENY": "'none'", "SAMEORIGIN": "'self'"}


class SecurityHeaders:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if settings.PERMISSIONS_POLICY:
            response.headers.setdefault("Permissions-Policy", settings.PERMISSIONS_POLICY)
        if "Content-Security-Policy" not in response.headers:
            policy = list(settings.CONTENT_SECURITY_POLICY)
            ancestors = FRAME_ANCESTORS.get(response.headers.get("X-Frame-Options", "").upper())
            if ancestors:
                policy.append(f"frame-ancestors {ancestors}")
            if policy:
                response.headers["Content-Security-Policy"] = "; ".join(policy)
        return response
