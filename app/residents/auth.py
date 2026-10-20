"""Logging in, and what the log says when it does not work.

Residents only ever see "Forkert e-mail eller kodeord", which must not tell a stranger which
addresses have an account. The log says why, so a resident who "cannot log in on the phone" can be
helped from one attempt: no such account (with the address actually received, written with
ascii() so a keyboard's ø for o or a stray character shows), wrong password, or account inactive.
Never the password.

A stale login form (a PWA restoring a page from days ago, a second tab) used to answer a bare 403,
which reads exactly like "my password does not work". `csrf_failure` sends it back to a fresh form.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.signals import user_login_failed
from django.dispatch import receiver
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

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
        return
    # The account is known here, so its id is enough: no need to log the address as well.
    reason = "account inactive" if not user.is_active else "wrong password"
    logger.warning("Login failed for account %s: %s", user.pk, reason)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """settings.CSRF_FAILURE_VIEW: a form too old to accept, explained instead of a bare 403."""
    logger.warning("CSRF refused %s %s: %s", request.method, request.path, reason)
    login = reverse("login")
    if request.path == login:
        messages.info(request, "Siden var udløbet. Prøv at logge ind igen.")
        query = request.GET.urlencode()
        return redirect(f"{login}?{query}" if query else login)
    return render(request, "403_csrf.html", status=403)
