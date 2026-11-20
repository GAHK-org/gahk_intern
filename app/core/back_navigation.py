"""What lets a page that comes back on the browser's Back button tell that it is out of date.

Back does not load a page again. The browser shows the copy it kept: the live page itself from its
back/forward cache, or the HTML from its HTTP cache (always after a form post, in Chrome). Either is
the page as it was BEFORE whatever you did next, so the comment you just wrote is missing, the
button you pressed is unpressed, and Chrome even types the text you sent back into the form.

Two numbers settle it, both from the server's clock so no device clock is involved:

  * every page carries the time its request started (`<meta name="gahk-rendered">`, base.html);
  * every successful write sets a cookie with the time it finished (LAST_WRITE_COOKIE_NAME).

A page shown again with a write newer than itself reloads (frontend/src/back-navigation.ts). A page
nothing has changed since keeps the instant Back.
"""

import time
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def not_a_write[View: Callable[..., Any]](view: View) -> View:
    """Mark a POST view that changes nothing any page shows: a preview, a status check.

    Only for requests the browser sends ON ITS OWN. Counting one of those as a write would reload the
    page you go back to every time, for nothing. A real write that is wrongly left unmarked costs one
    extra reload; one wrongly marked leaves a page out of date, so mark sparingly.
    """
    view.not_a_write = True  # type: ignore[attr-defined]
    return view


class LastWriteCookie:
    """Stamps each request's start, and sets the last-write cookie on each successful write."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        # The START of the request, not the moment the template renders: a write that lands while
        # this view is still reading would otherwise look older than the page that missed it.
        request.started_ms = now_ms()  # type: ignore[attr-defined]
        response = self.get_response(request)
        if (
            request.method not in SAFE_METHODS
            and response.status_code < 400
            and not getattr(request, "not_a_write", False)
        ):
            response.set_cookie(
                settings.LAST_WRITE_COOKIE_NAME,
                str(now_ms()),
                secure=not settings.DEBUG,
                httponly=False,  # read by frontend/src/back-navigation.ts; it says only "when"
                samesite="Lax",
            )
        return response

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., Any],
        view_args: tuple[object, ...],
        view_kwargs: dict[str, object],
    ) -> None:
        if getattr(view_func, "not_a_write", False):
            request.not_a_write = True  # type: ignore[attr-defined]


def rendered(request: HttpRequest) -> dict[str, int]:
    """Context processor: when this page's request started, for base.html's gahk-rendered meta."""
    return {"rendered_ms": getattr(request, "started_ms", 0) or now_ms()}
