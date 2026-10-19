"""`/healthz`, the container healthcheck: unauthenticated, cheap, touches the DB, no side effects.

A middleware first in MIDDLEWARE rather than a view, so it answers before Django validates the Host
header: a checker inside the container (Docker's, or Coolify's for a rolling update) sends whatever
host it likes, and an ALLOWED_HOSTS 400 would read as an unhealthy app.
"""

from collections.abc import Callable

from django.db import connection
from django.http import HttpRequest, HttpResponse

HEALTHZ_PATH = "/healthz"


def healthz() -> HttpResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return HttpResponse("db unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


class HealthzMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path == HEALTHZ_PATH and request.method in ("GET", "HEAD"):
            return healthz()
        return self.get_response(request)
