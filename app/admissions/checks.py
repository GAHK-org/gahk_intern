"""Startup check for the Turnstile captcha keys on the public application forms.

A half configuration has no server-side symptom: the app starts and the form renders. It is treated
as "Turnstile off" at runtime (admissions.views.turnstile_enabled), so the forms keep working — but
that also means the captcha someone meant to switch on is not there, which this warning says out loud.
"""

from collections.abc import Sequence

from django.apps.config import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Warning


def check_turnstile_keys(app_configs: Sequence[AppConfig] | None, **kwargs: object) -> list[CheckMessage]:
    site_key = bool(settings.TURNSTILE_SITE_KEY)
    secret = bool(settings.TURNSTILE_SECRET_KEY)
    if site_key == secret:
        return []  # both set (on) or neither (off, the dev default)
    present, missing = (
        ("TURNSTILE_SECRET_KEY", "TURNSTILE_SITE_KEY")
        if secret
        else ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY")
    )
    return [
        Warning(
            f"{present} is set but {missing} is empty, so Turnstile is OFF on the application forms.",
            hint="Set both keys to enable the captcha, or neither to disable it deliberately.",
            id="admissions.W001",
        )
    ]
