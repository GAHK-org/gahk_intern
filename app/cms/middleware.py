"""The `gahk_redaktoer` hint cookie: how gahk.dk knows to show its "Rediger siden" link.

gahk_web's protocol ("The edit link", docs/protocol.md there) fixes the cookie: `gahk_redaktoer=1`
on the parent domain (EDITOR_HINT_COOKIE_DOMAIN, ".gahk.dk" in production), Path=/, Secure,
SameSite=Lax, and NOT HttpOnly, because gahk.dk's static pages read it from a script. intern sets it
for a user who is a superuser or holds one of CMS_EDITOR_ROLES as a REAL role (a role preview neither
grants nor removes it), at login and on any later response that finds it missing, and deletes it
(same name, domain and path) at logout and on the first response after the user stops qualifying.

It is a hint, not a permission: the link leads to /hjemmeside/…, which checks the roles itself. So
the cookie is only touched when its state has to change. A response that leaves it alone carries
no Set-Cookie (which would keep it out of caches) and, for a request without a session, does not
even open the session (which would add Vary: Cookie). Static files and media never carry it.
"""

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from residents.permissions import CMS_EDITOR_ROLES, real_roles

EDITOR_HINT_COOKIE = "gahk_redaktoer"


def is_cms_editor(request: HttpRequest) -> bool:
    """Whether the signed-in user may edit gahk.dk, by real roles only (superuser: every role)."""
    return not real_roles(request.user).isdisjoint(CMS_EDITOR_ROLES)


def _path_prefix(url: str) -> str | None:
    return None if "://" in url else "/" + url.lstrip("/")


class EditorHintCookie:
    """Keep the browser's `gahk_redaktoer` cookie in step with whether its user edits gahk.dk."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response
        self.skipped = tuple(
            prefix
            for url in (settings.STATIC_URL, settings.MEDIA_URL)
            if url and (prefix := _path_prefix(url))
        )

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if self.skipped and request.path.startswith(self.skipped):
            return response
        held = request.COOKIES.get(EDITOR_HINT_COOKIE)
        if self._qualifies(request):
            if held != "1":
                self._set(response)
        elif held is not None:
            self._delete(response)
        return response

    @staticmethod
    def _qualifies(request: HttpRequest) -> bool:
        session = getattr(request, "session", None)
        # No session cookie and no login during this request (login() cycles the session key): the
        # request is anonymous, which is known without loading the session.
        if settings.SESSION_COOKIE_NAME not in request.COOKIES and not (
            session is not None and session.modified
        ):
            return False
        return request.user.is_authenticated and is_cms_editor(request)

    @staticmethod
    def _set(response: HttpResponseBase) -> None:
        response.set_cookie(
            EDITOR_HINT_COOKIE,
            "1",
            max_age=settings.SESSION_COOKIE_AGE,  # as long as a login can last
            path="/",
            domain=settings.EDITOR_HINT_COOKIE_DOMAIN,
            secure=not settings.DEBUG,
            httponly=False,  # gahk.dk's pages are static; their script reads it
            samesite="Lax",
        )

    @staticmethod
    def _delete(response: HttpResponseBase) -> None:
        response.delete_cookie(
            EDITOR_HINT_COOKIE, path="/", domain=settings.EDITOR_HINT_COOKIE_DOMAIN, samesite="Lax"
        )
        if not settings.DEBUG:
            response.cookies[EDITOR_HINT_COOKIE]["secure"] = True
