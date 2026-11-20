"""Logging in, and what the log says when it does not work.

Residents only ever see "Forkert e-mail eller kodeord", which must not tell a stranger which
addresses have an account. The log says why, so a resident who "cannot log in on the phone" can be
helped from one attempt: no such account (with the address actually received, written with
ascii() so a keyboard's ø for o or a stray character shows), wrong password, or account inactive.
Never the password.

A stale login form (a PWA restoring a page from days ago, a second tab) used to answer a bare 403,
which reads exactly like "my password does not work". `csrf_failure` sends it back to a fresh form.

The audit trail gets every login, failed login, logout, password change and reset. A login attempt
is one event from the form to the answer, so a stored password re-hashed on the way in (the old
system's hashes are upgraded at the first login) belongs to it; the signals name what it was.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from audit import trail

from .models import Resident

logger = logging.getLogger(__name__)


@receiver(user_login_failed)
def log_failed_login(
    sender: object, credentials: dict[str, Any], request: HttpRequest | None = None, **_: object
) -> None:
    email = str(credentials.get("username") or "")
    try:
        user = Resident._default_manager.get_by_natural_key(email)
    except (Resident.DoesNotExist, Resident.MultipleObjectsReturned):
        logger.warning("Login failed: no account for %s", ascii(email))
        _audit_failed_login(None, {"reason": "no account", "email": ascii(email)})
        return
    # The account is known here, so its id is enough: no need to log the address as well.
    reason = "account inactive" if not user.is_active else "wrong password"
    logger.warning("Login failed for account %s: %s", user.pk, reason)
    _audit_failed_login(user, {"reason": reason})


def _audit_failed_login(account: Resident | None, details: dict[str, str]) -> None:
    attempt = trail.current()
    if attempt is not None and attempt.action == LOGIN_ATTEMPT:
        attempt.action = "auth.login.failed"
        attempt.details.update(details)
        attempt.involve(account, "subject")
        return
    trail.record("auth.login.failed", actor=None, subject=account, details=details)


LOGIN_ATTEMPT = "auth.login.attempt"


@receiver(user_logged_in)
def audit_login(
    sender: object, request: HttpRequest | None = None, user: Resident | None = None, **_: object
) -> None:
    attempt = trail.current()
    if attempt is not None and attempt.action == LOGIN_ATTEMPT:
        attempt.action = "auth.login"
        attempt.actor = user
        attempt.involve(user, "subject")
        return
    trail.record("auth.login", actor=user, subject=user)  # the Django admin's own login form


@receiver(user_logged_out)
def audit_logout(
    sender: object, request: HttpRequest | None = None, user: Resident | None = None, **_: object
) -> None:
    if user is not None:
        trail.record("auth.logout", actor=user, subject=user)


class LoginView(auth_views.LoginView):
    """Django's, with each POST one audit event: a login, a failed login, or (a form missing a
    field, a stale token) nothing at all."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        with trail.event(LOGIN_ATTEMPT, actor=None) as attempt:
            response = super().post(request, *args, **kwargs)
            if attempt.action == LOGIN_ATTEMPT:
                attempt.cancel()
        return response


class PasswordChangeView(auth_views.PasswordChangeView):
    def form_valid(self, form: PasswordChangeForm) -> HttpResponse:
        with trail.event("auth.password.changed", subject=form.user):
            return super().form_valid(form)


class PasswordResetView(auth_views.PasswordResetView):
    """Asking for a reset link is recorded with the address asked for, and the account it reached if
    any: Django answers "sent" either way, so the trail is the only place that tells them apart."""

    def form_valid(self, form: PasswordResetForm) -> HttpResponse:
        email = form.cleaned_data["email"]
        accounts = list(form.get_users(email))
        with trail.event(
            "auth.password_reset.requested",
            details={"email": ascii(email), "accounts": len(accounts)},
            involve=[(account, "subject") for account in accounts],
        ):
            return super().form_valid(form)


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Setting a password from a reset link. The link proves the mailbox, so the account's owner is
    the actor even though nobody is logged in yet."""

    def form_valid(self, form: SetPasswordForm) -> HttpResponse:
        with trail.event("auth.password_reset.completed", actor=form.user, subject=form.user):
            return super().form_valid(form)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """settings.CSRF_FAILURE_VIEW: a form too old to accept, explained instead of a bare 403."""
    logger.warning("CSRF refused %s %s: %s", request.method, request.path, reason)
    login = reverse("login")
    if request.path == login:
        messages.info(request, "Siden var udløbet. Prøv at logge ind igen.")
        query = request.GET.urlencode()
        return redirect(f"{login}?{query}" if query else login)
    return render(request, "403_csrf.html", status=403)
