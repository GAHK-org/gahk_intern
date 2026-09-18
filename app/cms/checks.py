"""Startup validation that cms.paths.RESERVED_TOP_SEGMENTS still covers the URLconf.

  cms.E001  a fixed URL prefix is mounted in config.urls but is not reserved

Same class of failure as den_hurtige.E008, and worth checking for the same reason: a matched pattern
wins over the CMS catch-all, so a page given that address would never open, while the admin happily
saved it and nothing anywhere reported a problem. The list in cms.paths is the only thing standing
between an editor and that dead end, and it is a hand-maintained copy of the URLconf — exactly the
kind of duplication that silently rots as urls.py grows.

Pure Python, no database access, deliberately: `manage.py check` runs during `migrate`, in CI before
any database exists, and inside `runserver`. Every other check in this project (core.checks,
den_hurtige.checks) holds that line, and a query here would turn an unmigrated database into a
startup failure. Reachability — which does need the database — is covered by a test and by an
in-flow warning when an editor saves, not by a check.
"""

from collections.abc import Sequence

from django.apps.config import AppConfig
from django.core.checks import CheckMessage, Error
from django.urls import get_resolver
from django.urls.resolvers import RegexPattern, RoutePattern

from .paths import RESERVED_TOP_SEGMENTS

# Characters that mean the leading text of a regex pattern is not a literal path segment.
_REGEX_METACHARACTERS = set(r".*+?[]()|{}\^$")


def _literal_first_segment(pattern: object) -> str | None:
    """The fixed first path segment a URL pattern owns, or None when it is not a literal.

    Both pattern flavours reach here: `path()` gives a RoutePattern whose text is already literal
    apart from `<converter:name>` parts, while `re_path()` gives a RegexPattern that has to be
    checked for metacharacters before any of it can be believed.
    """
    try:
        text = str(pattern)
    except Exception:  # an exotic custom pattern must not break `manage.py check`
        return None

    if isinstance(pattern, RoutePattern):
        head = text.split("/", maxsplit=1)[0]
        # A converter in the first segment means the segment is not fixed, so it reserves nothing.
        return None if "<" in head else head or None

    if isinstance(pattern, RegexPattern):
        if not text.startswith("^"):
            return None  # unanchored: matches anywhere, so it owns no particular prefix
        head = text[1:].split("/", maxsplit=1)[0]
        return None if not head or set(head) & _REGEX_METACHARACTERS else head

    return None


def check_reserved_top_segments(
    app_configs: Sequence[AppConfig] | None, **kwargs: object
) -> list[CheckMessage]:
    """Every fixed top-level URL prefix must be reserved, or a page could be given that address."""
    errors: list[CheckMessage] = []
    seen: set[str] = set()

    for entry in get_resolver(None).url_patterns:
        segment = _literal_first_segment(entry.pattern)
        if segment is None or segment in seen:
            continue
        seen.add(segment)
        if segment in RESERVED_TOP_SEGMENTS:
            continue
        errors.append(
            Error(
                f"“/{segment}/” is mounted in config/urls.py but is not listed in "
                f"cms.paths.RESERVED_TOP_SEGMENTS.",
                hint=(
                    f"An editor could give a CMS page the address “{segment}”. The fixed pattern is "
                    "matched first, so that page would never open and nothing would report it. Add "
                    f"“{segment}” to RESERVED_TOP_SEGMENTS."
                ),
                id="cms.E001",
            )
        )
    return errors
