"""The authentication backend: Django's own, behind the login limits of residents.throttle.

A backend rather than only the login form, so every way in is covered: the intranet's login page,
Django's admin login at /django-admin/, and any login view added later.
"""

from typing import Any

from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest

from . import throttle
from .models import Resident


class ThrottledModelBackend(ModelBackend):
    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,  # noqa: ANN401 - Django's signature
    ) -> Resident | None:
        # No request: a shell, a management command or a test's Client.login — nothing to count by.
        if request is None:
            return super().authenticate(request, username, password, **kwargs)
        if username is None:
            username = kwargs.get(Resident.USERNAME_FIELD)
        if throttle.login_blocked(request, username):
            request.login_throttled = True  # type: ignore[attr-defined]  # for residents.auth's log line
            # Stops the attempt here; django.contrib.auth.authenticate() answers it as a failed login.
            raise PermissionDenied
        user = super().authenticate(request, username, password, **kwargs)
        if user is None:
            throttle.login_failed(request, username)
        else:
            throttle.login_succeeded(request, username)
        return user
