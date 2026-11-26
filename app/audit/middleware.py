"""Marks every request as one source of audit events: its resident, its address, one correlation id."""

from collections.abc import Callable

from django.http import HttpRequest
from django.http.response import HttpResponseBase

from . import trail


class AuditContext:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        with trail.context("web", request=request):
            return self.get_response(request)
