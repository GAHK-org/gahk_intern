"""The logged-in resident of a request. Who may do what is the access app's (access.api)."""

from collections.abc import Callable

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from .models import Account

# A Django view: called with a request (plus captured URL kwargs) and returns a response.
#
# HttpResponseBase rather than HttpResponse, because FileResponse and StreamingHttpResponse are NOT
# HttpResponse subclasses - they descend from HttpResponseBase directly. Without this, any view that
# hands back a file (a download, or anything like core.media.serve_media that ever wants gating)
# cannot be decorated with a permission or a rollout Gate, which is the one place the decorator is
# most worth having. Widening a return type is safe for every existing caller.
View = Callable[..., HttpResponseBase]
# What request.user is statically typed as: django-stubs can't know it is always a Account here.
AnyUser = AbstractBaseUser | AnonymousUser


def current_resident(request: HttpRequest) -> Account:
    """The logged-in Account of a request the gate has let through.

    Centralizes the "an authenticated user is a Account" invariant that django-stubs can't express.
    Raises PermissionDenied if the request user is not an authenticated resident - which only happens
    if a page is open to nobody logged in by mistake, never in the normal flow.
    """
    user = request.user
    if not isinstance(user, Account):
        raise PermissionDenied
    return user
