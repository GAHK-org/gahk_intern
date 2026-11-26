"""The parts of the site, the grades of access to one, and which part each page is in.

A permission is named `<feature>.<action>`. The actions every feature has are its grades, in order:

  * view    open its pages, but change nothing (only GET, HEAD and OPTIONS get through the gate);
  * use     what a member of the house does there: post, book, add krydser;
  * manage  what the group that runs it does: moderate, edit, approve;
  * administer  what only the administrators do, inside a feature others manage: merge albums,
    delete a page of gahk.dk.

A higher grade includes the lower ones. A feature may also name extra actions (EXTRAS) for a power
between use and manage that one group needs alone, like Viceværterne moving a repair; manage includes
every extra of its feature.

Every page is in exactly one feature, marked in the URLconf with `in_feature`. The gate
(access.middleware) refuses a page in no feature to everyone, and a test fails while one exists, so
a new page is closed until somebody decides whose it is.
"""

from collections.abc import Callable, Iterable
from enum import IntEnum, StrEnum
from typing import Any, cast
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.urls import Resolver404, URLPattern, URLResolver, resolve


class Feature(StrEnum):
    """A part of the site."""

    # Logging in and out and the password pages: every account, and nobody logged in.
    ACCOUNT = "account"
    # No login involved: the service worker, /install, calendar feeds (the token is the credential).
    PUBLIC = "public"
    # Uploaded files (/media/), which carry their own checks.
    FILES = "files"
    DASHBOARD = "dashboard"
    DIRECTORY = "directory"  # Alumneliste; manage: the månedsliste and residents' details
    PROFILES = "profiles"
    MENTIONS = "mentions"  # the @-picker's lookup, used by every composer
    STAMTREE = "stamtree"
    STATS = "stats"
    NOTIFICATIONS = "notifications"
    DEN_HURTIGE = "den_hurtige"  # manage: moderate
    OPSLAGSTAVLE = "opslagstavle"  # manage: moderate and pin
    EVENTS = "events"  # Begivenheder
    REPARATIONER = "reparationer"  # manage: the whole board; extra "move": triage
    PHOTO_ALBUM = "photo_album"  # manage: Fotogruppen's curating
    GAHKFLIX = "gahkflix"  # manage: publishing films
    AK = "ak"  # use: one's own AK page; manage: the AK-gruppe's overview
    OELKAELDER = "oelkaelder"  # use: one's own balance; manage: the till and the accounts
    VAERELSESTJEK = "vaerelsestjek"  # manage: the AK-gruppe's overview
    SOEGVAERELSE = "soegvaerelse"  # manage: the Indstilling's offers
    # The wiki, a site of its own whose accounts follow the intranet's: no view here.
    WIKI = "wiki"
    # The tools of one group, which no one else opens at all.
    ADMISSIONS = "admissions"  # Optagelse: the Indstilling
    WEBSITE = "website"  # Hjemmeside, gahk.dk's editor
    ACCOUNTING = "accounting"  # Regnskab
    SITE_ADMIN = "site_admin"  # /admin/: worker jobs, roles, the audit trail, "Se side som"
    DJANGO_ADMIN = "django_admin"


class Grade(IntEnum):
    NONE = 0
    VIEW = 1
    USE = 2
    MANAGE = 3
    ADMINISTER = 4


GRADE_ACTIONS = {"view": Grade.VIEW, "use": Grade.USE, "manage": Grade.MANAGE, "administer": Grade.ADMINISTER}

# Powers between use and manage that one group has alone. Manage includes them.
EXTRAS: dict[Feature, frozenset[str]] = {
    # Viceværterne triage repairs: move a ticket on, or hand it to the Repperne.
    Feature.REPARATIONER: frozenset({"move"}),
}

# The features any account reaches, whatever it is: it must be able to log out.
ALWAYS = frozenset({Feature.ACCOUNT, Feature.PUBLIC})
# The features nobody logged in reaches. Uploaded files decide for themselves: the till's product
# photos are public (core.media.PUBLIC_PREFIXES).
ANONYMOUS = ALWAYS | {Feature.FILES}


def parse(permission: str) -> tuple[Feature, str]:
    """`"oelkaelder.manage"` → (Feature.OELKAELDER, "manage"). Raises ValueError on a name that does
    not exist, so a typo fails where it is written rather than closing a page."""
    feature_name, _, action = permission.partition(".")
    feature = Feature(feature_name)
    if action not in GRADE_ACTIONS and action not in EXTRAS.get(feature, frozenset()):
        raise ValueError(f"{feature} has no action {action!r}")
    return feature, action


# ---- marking the URLconf --------------------------------------------------------------------------

_FEATURE_ATTRIBUTE = "access_feature"


def _mark(view: Callable[..., Any], feature: Feature) -> None:
    current = feature_of(view)
    if current not in (None, feature):
        raise ImproperlyConfigured(f"{view.__qualname__} is in both {current} and {feature}")
    # A bound method (some of Django admin's views) takes no attribute: its function does.
    setattr(getattr(view, "__func__", view), _FEATURE_ATTRIBUTE, feature)


def _mark_patterns(patterns: Iterable[URLPattern | URLResolver], feature: Feature) -> None:
    """Every view not marked already: a page an included URLconf put in a feature of its own keeps it."""
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            _mark_patterns(pattern.url_patterns, feature)
        elif feature_of(pattern.callback) is None:
            _mark(pattern.callback, feature)


def in_feature[T](feature: Feature, target: T) -> T:
    """Put a view, every view of an `include(...)` or a list of path()s in `feature`. In the URLconf:

    path("den-hurtige/", in_feature(Feature.DEN_HURTIGE, include("den_hurtige.urls"))),
    path("stamtree/", in_feature(Feature.STAMTREE, views.stamtree), name="stamtree"),
    """
    if isinstance(target, list):  # a list of path()s
        _mark_patterns(target, feature)
    elif isinstance(target, tuple):  # what include() returns: (urlconf module, app_name, namespace)
        urlconf = target[0]
        patterns = cast("Iterable[URLPattern | URLResolver]", getattr(urlconf, "urlpatterns", urlconf))
        _mark_patterns(patterns, feature)
    else:
        _mark(target, feature)  # type: ignore[arg-type]
    return target


def feature_of(view: Callable[..., Any]) -> Feature | None:
    return getattr(view, _FEATURE_ATTRIBUTE, None)


def feature_of_url(url: str) -> Feature | None:
    """The feature of the page at a site path (a query or fragment is ignored); None for no page."""
    try:
        return feature_of(resolve(urlsplit(url).path).func)
    except Resolver404:
        return None
